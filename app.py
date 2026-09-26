import os
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from flask import Flask, redirect, request, session
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from supabase import create_client


# Never permit insecure OAuth transport on the hosted service.
os.environ.pop("OAUTHLIB_INSECURE_TRANSPORT", None)

BASE_DIR = Path(__file__).resolve().parent

SITE_URL = "https://akprj.github.io/PrayerTimesAutomation/"

CLIENT_SECRETS_FILE = os.environ.get(
    "GOOGLE_CLIENT_SECRETS_FILE",
    "/etc/secrets/credentials_web.json",
)

REDIRECT_URI = os.environ["REDIRECT_URI"]

if not REDIRECT_URI.startswith("https://"):
    raise RuntimeError("The hosted OAuth redirect URI must use HTTPS.")

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]

def get_google_client_config():
    blob = os.environ.get("GOOGLE_OAUTH_CLIENT_JSON")
    if not blob:
        return None
    return json.loads(blob)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ["FLASK_SECRET_KEY"],
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=15),
)

cipher = Fernet(
    os.environ["TOKEN_ENCRYPTION_KEY"].encode("utf-8")
)

supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_ROLE_KEY"],
)


def clear_oauth_session():
    session.pop("state", None)
    session.pop("code_verifier", None)


def failure_page(message, status=400):
    # Only pass fixed application messages here, not exception text.
    return (
        f"""
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <title>Google connection</title>
        </head>
        <body>
            <h1>Connection not completed</h1>
            <p>{message}</p>
            <p><a href="/connect_google">Try connecting again</a></p>
        </body>
        </html>
        """,
        status,
    )


@app.after_request
def prevent_sensitive_page_caching(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.route("/")
def index():
    return redirect(SITE_URL)


@app.route("/health")
def health():
    return {"status": "ok"}


@app.route("/connect_google")
def connect_google():
    session.clear()
    session.permanent = True

    config = get_google_client_config()

    if config:
        flow = Flow.from_client_config(
            config,
            scopes=SCOPES,
            autogenerate_code_verifier=True,
        )
    else:
        flow = Flow.from_client_secrets_file(
            CLIENT_SECRETS_FILE,
            scopes=SCOPES,
            autogenerate_code_verifier=True,
        )

        flow.redirect_uri = REDIRECT_URI


    authorization_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )

    session["state"] = state
    session["code_verifier"] = flow.code_verifier

    return redirect(authorization_url)


@app.route("/oauth_callback")
def oauth_callback():
    expected_state = session.get("state")
    received_state = request.args.get("state", "")
    code_verifier = session.get("code_verifier")

    if (
        not expected_state
        or not code_verifier
        or not secrets.compare_digest(expected_state, received_state)
    ):
        clear_oauth_session()
        return failure_page(
            "Your sign-in session expired or could not be verified."
        )

    # Consume this session's OAuth state before processing the callback.
    clear_oauth_session()

    if request.args.get("error"):
        return failure_page(
            "Google sign-in was cancelled or permission was not granted."
        )

    code = request.args.get("code")
    if not code:
        return failure_page(
            "Google did not return an authorization code."
        )

    try:
        config = get_google_client_config()

        if config:
            flow = Flow.from_client_config(
                config,
                scopes=SCOPES,
                state=expected_state,
                code_verifier=code_verifier,
                autogenerate_code_verifier=False,
            )
        else:
            flow = Flow.from_client_secrets_file(
                CLIENT_SECRETS_FILE,
                scopes=SCOPES,
                state=expected_state,
                code_verifier=code_verifier,
                autogenerate_code_verifier=False,
            )

         flow.redirect_uri = REDIRECT_URI

        # State was checked explicitly above. Using the code directly
        # avoids relying on the proxy-generated request URL.
        flow.fetch_token(code=code)

        credentials = flow.credentials

        if not credentials.has_scopes(
            ["https://www.googleapis.com/auth/calendar"]
        ):
            return failure_page(
                "Calendar permission is required. "
                "Please reconnect and grant calendar access."
            )

        if not credentials.refresh_token:
            return failure_page(
                "Google did not provide permission for background updates. "
                "Please reconnect and approve access."
            )

        user_service = build(
            "oauth2",
            "v2",
            credentials=credentials,
            cache_discovery=False,
        )
        user_info = user_service.userinfo().get().execute()

        # Google's user ID is stable; email addresses can change.
        google_sub = user_info.get("id")
        email = user_info.get("email")

        if (
            not google_sub
            or not email
            or not user_info.get("verified_email")
        ):
            return failure_page(
                "A verified Google account could not be identified."
            )

        encrypted_refresh_token = cipher.encrypt(
            credentials.refresh_token.encode("utf-8")
        ).decode("utf-8")

        member_data = {
            "google_sub": google_sub,
            "email": email,
            "refresh_token_encrypted": encrypted_refresh_token,
            "connection_status": "connected",
            "initial_sync_pending": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        # On reconnect, preserve calendar_id, created_at, and last_synced_at.
        # For new members, database defaults populate omitted columns.
        supabase.table("prayer_members").upsert(
            member_data,
            on_conflict="google_sub",
        ).execute()

        return """
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <title>Google Calendar connected</title>
        </head>
        <body>
            <h1>Google Calendar connected successfully</h1>
            <p>Your account has been registered for prayer-time updates.</p>
            <p>Your first calendar update is pending processing.</p>
            <p>
                <a href="https://akprj.github.io/PrayerTimesAutomation/">
                    Return to the website
                </a>
            </p>
        </body>
        </html>
        """

    except Exception as exc:
        app.logger.error(
            "Google onboarding failed; exception type: %s; message: %s",
            type(exc).__name__,
            str(exc),
        )
        return failure_page(
            "We could not finish connecting your account. "
            "Please try again later.",
            500,
        )


if __name__ == "__main__":
    # Render will use Gunicorn rather than this development server.
    app.run(
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
    )
