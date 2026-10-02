import hashlib
import json
import os
import re
import time
from google.auth.exceptions import RefreshError
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from PIL import Image, ImageOps
import pytesseract
from supabase import create_client

from drive_timetable import fetch_latest_timetable


BASE_DIR = Path(__file__).resolve().parent

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]

cipher = Fernet(
    os.environ["TOKEN_ENCRYPTION_KEY"].encode("utf-8")
)

supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_ROLE_KEY"],
)


MAX_RETRIES = 8
BASE_SLEEP = 1.0


def load_google_client_config():
    client_secrets_file = os.environ.get(
        "GOOGLE_CLIENT_SECRETS_FILE",
        "/etc/secrets/credentials_web.json",
    )
    with open(client_secrets_file, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    web_config = data.get("web")
    if not web_config:
        raise RuntimeError(
            "Google client secrets file must contain a top-level 'web' object."
        )

    client_id = web_config.get("client_id")
    client_secret = web_config.get("client_secret")

    if not client_id or not client_secret:
        raise RuntimeError(
            "Google client secrets file is missing client_id or client_secret."
        )

    return client_id, client_secret


def stable_event_id(calendar_id: str, current_date: date, prayer: str) -> str:
    event_key = (
        f"prayer-timings|{calendar_id}|{current_date.isoformat()}|{prayer}"
    )
    return hashlib.sha256(event_key.encode("utf-8")).hexdigest()


def build_prayer_times_and_dates():
    config = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    timezone_obj = ZoneInfo(config["timezone"])
    year = datetime.now(timezone_obj).year

    if os.name == "nt":
        pytesseract.pytesseract.tesseract_cmd = (
            r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        )
    else:
        pytesseract.pytesseract.tesseract_cmd = "tesseract"

    stream, metadata = fetch_latest_timetable()

    print(f"Using Drive timetable: {metadata.get('name')}")
    print(f"Created time: {metadata.get('createdTime')}")

    with stream:
        with Image.open(stream) as source:
            timetable_image = ImageOps.exif_transpose(source).convert("RGB")

    date_image = ImageOps.grayscale(timetable_image)
    date_image = date_image.resize(
        (date_image.width * 3, date_image.height * 3),
        Image.Resampling.LANCZOS,
    )
    date_image = ImageOps.autocontrast(date_image)

    try:
        date_text = pytesseract.image_to_string(
            date_image,
            lang="eng+deu",
        )
    finally:
        date_image.close()
    
    print(f"RAW DATE OCR TEXT: {date_text!r}", flush=True)
    
    months = {
        "jan": 1,
        "feb": 2,
        "mär": 3,
        "mar": 3,
        "maerz": 3,
        "apr": 4,
        "mai": 5,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "okt": 10,
        "oct": 10,
        "nov": 11,
        "dez": 12,
        "dec": 12,
    }

    weekdays = {
        "montag": 0,
        "dienstag": 1,
        "mittwoch": 2,
        "donnerstag": 3,
        "freitag": 4,
        "samstag": 5,
        "sonntag": 6,
    }

    weekday_pattern = (
        r"Montag|Dienstag|Mittwoch|Donnerstag|"
        r"Freitag|Samstag|Sonntag"
    )

    pattern = (
        r"\b(?:"
        r"(?P<marker>ab|bis\s+zum|zum)\s+"
        rf"(?:(?P<marked_weekday>{weekday_pattern})\s*,?\s*)?"
        r"|"
        rf"(?P<weekday>{weekday_pattern})\s*,?\s*"
        r")"
        r"(?:dem\s+)?"
        r"(?P<day>\d{1,2})\s*[.,]?\s*"
        r"(?P<month>[A-Za-zÄÖÜäöüß]+)\b"
    )

    matches = list(
        re.finditer(pattern, date_text, flags=re.IGNORECASE)
    )

    print(
        f"DATE MATCHES: {[match.group(0) for match in matches]!r}",
        flush=True,
    )

    if not matches:
        raise RuntimeError(
            "Could not identify any timetable date. "
            "Cannot safely infer the timetable week."
        )

    candidate_ranges = set()
    detected_roles = set()

    for match in matches:
        marker = " ".join(
            (match.group("marker") or "").lower().split()
        )

        weekday_text = (
            match.group("marked_weekday")
            or match.group("weekday")
        )

        month_text = match.group("month")
        month_name = month_text.lower()
        month = months.get(month_name) or months.get(month_name[:3])

        # If the existing mapping fails, normalize to English.
        if month is None:
            german_to_english = {
                "jan": "January",
                "feb": "February",
                "mär": "March",
                "mae": "March",
                "mrz": "March",
                "mar": "March",
                "apr": "April",
                "mai": "May",
                "may": "May",
                "jun": "June",
                "jul": "July",
                "aug": "August",
                "sep": "September",
                "okt": "October",
                "oct": "October",
                "nov": "November",
                "dez": "December",
                "dec": "December",
            }

            english_month_numbers = {
                "January": 1,
                "February": 2,
                "March": 3,
                "April": 4,
                "May": 5,
                "June": 6,
                "July": 7,
                "August": 8,
                "September": 9,
                "October": 10,
                "November": 11,
                "December": 12,
            }

            english_month = german_to_english.get(month_name[:3])
            month = english_month_numbers.get(english_month)

            if month is not None:
                print(
                    f"MONTH FALLBACK: {month_text!r} -> "
                    f"{english_month} ({month})",
                    flush=True,
                )


        if month is None:
            raise RuntimeError(
                f"Unrecognised month in date OCR: {month_text!r}"
            )

        try:
            parsed_date = date(
                year,
                month,
                int(match.group("day")),
            )
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid timetable date: {match.group(0)!r}"
            ) from exc

        # Validate a weekday whenever OCR captured one.
        if weekday_text:
            expected_weekday = weekdays[weekday_text.lower()]

            if parsed_date.weekday() != expected_weekday:
                raise RuntimeError(
                    f"Weekday mismatch: {match.group(0)!r} "
                    f"does not agree with {parsed_date}."
                )

        # Identify which endpoint this date represents.
        if marker == "ab":
            role = "start"
        elif marker in {"bis zum", "zum"}:
            role = "end"
        elif weekday_text and weekday_text.lower() == "samstag":
            role = "start"
        elif weekday_text and weekday_text.lower() == "freitag":
            role = "end"
        else:
            raise RuntimeError(
                f"Cannot identify a Saturday/Friday endpoint: "
                f"{match.group(0)!r}"
            )

        if role == "start":
            if parsed_date.weekday() != 5:
                raise RuntimeError(
                    f"Timetable start must be Saturday, "
                    f"but detected {parsed_date}."
                )

            candidate_start = parsed_date
            candidate_end = parsed_date + timedelta(days=6)

        else:
            if parsed_date.weekday() != 4:
                raise RuntimeError(
                    f"Timetable end must be Friday, "
                    f"but detected {parsed_date}."
                )

            candidate_end = parsed_date
            candidate_start = parsed_date - timedelta(days=6)

        detected_roles.add(role)
        candidate_ranges.add((candidate_start, candidate_end))

    # If multiple dates were read, they must identify the same week.
    if len(candidate_ranges) != 1:
        raise RuntimeError(
            "Detected timetable dates identify different weeks. "
            "Refusing to infer an inconsistent date range."
        )

    start_date, end_date = next(iter(candidate_ranges))
    day_count = (end_date - start_date).days + 1

    if (
        day_count != 7
        or start_date.weekday() != 5
        or end_date.weekday() != 4
    ):
        raise RuntimeError(
            f"Invalid Saturday–Friday timetable range: "
            f"{start_date} to {end_date}."
        )

    if detected_roles == {"start"}:
        print(
            f"DATE FALLBACK: Read Saturday {start_date}; "
            f"inferred Friday {end_date}.",
            flush=True,
        )
    elif detected_roles == {"end"}:
        print(
            f"DATE FALLBACK: Read Friday {end_date}; "
            f"inferred Saturday {start_date}.",
            flush=True,
        )
    else:
        print(
            f"DATE RANGE VERIFIED: {start_date} to {end_date}.",
            flush=True,
        )


    rows = [
        ("Fajr", 0.175, 0.255),
        ("Zohar", 0.275, 0.355),
        ("Assr", 0.375, 0.455),
        ("Maghrib", 0.480, 0.560),
        ("Ishaa", 0.580, 0.660),
        ("Juma", 0.685, 0.765),
    ]

    prayer_times = {}
    with timetable_image as image:
        width, height = image.size

        for prayer, top, bottom in rows:
            crop = image.crop(
                (
                    int(width * 0.36),
                    int(height * top),
                    int(width * 0.67),
                    int(height * bottom),
                )
            )

            crop = ImageOps.grayscale(crop)
            crop = crop.resize(
                (crop.width * 4, crop.height * 4),
                Image.Resampling.LANCZOS,
            )
            crop = ImageOps.autocontrast(crop)
            crop = ImageOps.expand(crop, border=20, fill="white")

            result = pytesseract.image_to_string(
                crop,
                lang="eng",
                config="--psm 7 -c tessedit_char_whitelist=0123456789:",
            ).strip()

            if not re.fullmatch(r"\d\d:\d\d", result):
                raise RuntimeError(
                    f"Invalid OCR time for {prayer}: {result!r}"
                )

            prayer_times[prayer] = datetime.strptime(result, "%H:%M").time()

    return config, timezone_obj, start_date, end_date, day_count, prayer_times


def choose_target_calendar(service):
    preferred_prefixes = [
        "prayer times automation",
        "prayer times",
        "prayer timings",
        "prayer timetable",
        "prayer time",
        "prayers",
        "prayer",
    ]

    fallback_contains = [
        "prayer",
        "prayers",
        "salah",
        "namaz",
        "salat",
    ]

    page_token = None
    calendars = []

    while True:
        response = service.calendarList().list(pageToken=page_token).execute()
        calendars.extend(
            calendar
            for calendar in response.get("items", [])
            if calendar.get("accessRole") in ("owner", "writer")
            and not calendar.get("deleted", False)
        )

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    for calendar in calendars:
        summary = calendar.get("summary", "").strip().lower()
        if any(summary.startswith(prefix) for prefix in preferred_prefixes):
            print(
                f"Using prayer calendar (prefix match): "
                f"{calendar.get('summary')} ({calendar.get('id')})"
            )
            return calendar.get("id")

    for calendar in calendars:
        summary = calendar.get("summary", "").strip().lower()
        if any(word in summary for word in fallback_contains):
            print(
                f"Using prayer calendar (contains match): "
                f"{calendar.get('summary')} ({calendar.get('id')})"
            )
            return calendar.get("id")

    print("No prayer-specific calendar found. Using primary calendar.")
    return "primary"


def decrypt_refresh_token(refresh_token_encrypted: str) -> str:
    return cipher.decrypt(
        refresh_token_encrypted.encode("utf-8")
    ).decode("utf-8")


def build_member_credentials(refresh_token: str) -> Credentials:
    client_id, client_secret = load_google_client_config()

    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )

    credentials.refresh(Request())
    return credentials


def fetch_pending_members():
    response = (
        supabase.table("prayer_members")
        .select("*")
        .eq("initial_sync_pending", True)
        .eq("connection_status", "connected")
        .execute()
    )
    return response.data or []


def mark_member_synced(member_id: str, calendar_id: str):
    supabase.table("prayer_members").update(
        {
            "calendar_id": calendar_id,
            "initial_sync_pending": False,
            "last_synced_at": datetime.now(timezone.utc).isoformat(),
            "connection_status": "connected",
        }
    ).eq("id", member_id).execute()

def fetch_connected_members():
    members = []
    page_size = 500
    offset = 0

    while True:
        response = (
            supabase.table("prayer_members")
            .select("*")
            .eq("connection_status", "connected")
            .order("id")
            .range(offset, offset + page_size - 1)
            .execute()
        )

        batch = response.data or []
        members.extend(batch)

        if len(batch) < page_size:
            break

        offset += page_size

    return members


def mark_member_reconnect_required(member_id: str):
    supabase.table("prayer_members").update(
        {
            "connection_status": "reconnect_required",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", member_id).execute()

def event_matches_desired(existing, desired):
    for field in ("summary", "description"):
        if existing.get(field, "") != desired.get(field, ""):
            return False

    for field in ("start", "end"):
        existing_part = existing.get(field, {})
        desired_part = desired[field]

        existing_datetime = existing_part.get("dateTime")
        desired_datetime = desired_part["dateTime"]

        if not existing_datetime:
            return False

        try:
            existing_value = datetime.fromisoformat(
                existing_datetime.replace("Z", "+00:00")
            )
            desired_value = datetime.fromisoformat(
                desired_datetime.replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            return False

        if existing_value != desired_value:
            return False

        if existing_part.get("timeZone") != desired_part.get("timeZone"):
            return False

    existing_reminders = existing.get("reminders", {})
    desired_reminders = desired["reminders"]

    if existing_reminders.get("useDefault", False) != (
        desired_reminders.get("useDefault", False)
    ):
        return False

    existing_overrides = sorted(
        (item["method"], item["minutes"])
        for item in existing_reminders.get("overrides", [])
    )
    desired_overrides = sorted(
        (item["method"], item["minutes"])
        for item in desired_reminders.get("overrides", [])
    )

    return existing_overrides == desired_overrides


def insert_event_handling_deleted(service, calendar_id, body):
    original_id = body["id"]
    candidate_id = original_id

    for generation in range(50):
        candidate_body = dict(body)
        candidate_body["id"] = candidate_id

        try:
            service.events().insert(
                calendarId=calendar_id,
                body=candidate_body,
            ).execute()

            return "created"

        except HttpError as insert_error:
            insert_status = getattr(
                insert_error.resp, "status", None
            )

            if insert_status != 409:
                raise

            try:
                existing = service.events().get(
                    calendarId=calendar_id,
                    eventId=candidate_id,
                ).execute()

            except HttpError as lookup_error:
                lookup_status = getattr(
                    lookup_error.resp, "status", None
                )

                if lookup_status == 410:
                    existing = {"status": "cancelled"}
                else:
                    # An uncertain lookup must not create a duplicate.
                    raise

            event_status = existing.get("status", "unknown")

            print(
                f"ID CONFLICT: {body['summary']} "
                f"on {body['start']['dateTime']} "
                f"-> Google status: {event_status}",
                flush=True,
            )

            if event_status == "cancelled":
                replacement_key = (
                    f"{original_id}:replacement:{generation + 1}"
                )
                candidate_id = hashlib.sha256(
                    replacement_key.encode("utf-8")
                ).hexdigest()

                print(
                    "Deleted event detected; trying replacement ID "
                    f"number {generation + 1}.",
                    flush=True,
                )

                continue

            if event_status not in ("confirmed", "tentative"):
                raise RuntimeError(
                    f"Unexpected event status {event_status!r} "
                    f"for event {candidate_id!r}."
                )

            if event_matches_desired(existing, candidate_body):
                return "skipped"

            # Patch only app-managed fields and preserve other fields.
            patch_body = {
                field: candidate_body[field]
                for field in (
                    "summary",
                    "description",
                    "start",
                    "end",
                    "reminders",
                )
            }

            service.events().patch(
                calendarId=calendar_id,
                eventId=candidate_id,
                body=patch_body,
            ).execute()

            return "updated"

    raise RuntimeError(
        "Reached the replacement-ID limit for a deleted event."
    )




def resolve_member_calendar(service, member):
    saved_calendar_id = member.get("calendar_id")

    if saved_calendar_id and saved_calendar_id != "primary":
        try:
            service.calendars().get(
                calendarId=saved_calendar_id
            ).execute()

            return saved_calendar_id

        except HttpError as exc:
            status = getattr(exc.resp, "status", None)

            if status not in (404, 410):
                raise

            print(
                f"Saved calendar is no longer available: "
                f"{saved_calendar_id!r}. Resolving another destination.",
                flush=True,
            )

    calendar_id = choose_target_calendar(service)

    if calendar_id != saved_calendar_id:
        print(
            f"Calendar destination changed: "
            f"{saved_calendar_id!r} -> {calendar_id!r}",
            flush=True,
        )

    return calendar_id


def sync_member(
    member,
    config,
    timezone_obj,
    start_date,
    day_count,
    prayer_times,
):
    email = member.get("email") or "(unknown)"
    member_id = member["id"]

    print(f"\n--- Syncing member: {email} ---", flush=True)

    refresh_token = decrypt_refresh_token(
        member["refresh_token_encrypted"]
    )
    credentials = build_member_credentials(refresh_token)

    service = build(
        "calendar",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )

    calendar_id = resolve_member_calendar(service, member)

    print(f"Using calendar_id: {calendar_id}", flush=True)
    print(f"Timezone: {timezone_obj.key}", flush=True)
    print(f"Date range starts: {start_date}", flush=True)

    reminder_minutes = config["reminder_minutes"]
    friday_replaces_zohar = config.get(
        "friday_replaces_zohar", False
    )
    regular_prayers = ["Fajr", "Zohar", "Assr", "Maghrib", "Ishaa"]
    duration_minutes = 5

    total_attempted = 0
    totals = {
        "created": 0,
        "updated": 0,
        "skipped": 0,
    }

    if MAX_RETRIES < 1:
        raise ValueError("MAX_RETRIES must be at least 1.")

    for offset in range(day_count):
        current_date = start_date + timedelta(days=offset)

        prayers = regular_prayers.copy()

        if current_date.weekday() == 4:
            if friday_replaces_zohar:
                prayers[prayers.index("Zohar")] = "Juma"
            else:
                prayers.append("Juma")

        for prayer in prayers:
            starts_at = datetime.combine(
                current_date,
                prayer_times[prayer],
                tzinfo=timezone_obj,
            )
            end_at = starts_at + timedelta(
                minutes=duration_minutes
            )
            minutes = reminder_minutes[prayer]

            event_id = stable_event_id(
                calendar_id,
                current_date,
                prayer,
            )

            body = {
                "id": event_id,
                "summary": prayer,
                "description": (
                    f"Prayer Timings Automation: start time "
                    f"{starts_at:%H:%M} "
                    f"on {current_date.isoformat()}."
                ),
                "start": {
                    "dateTime": starts_at.isoformat(),
                    "timeZone": timezone_obj.key,
                },
                "end": {
                    "dateTime": end_at.isoformat(),
                    "timeZone": timezone_obj.key,
                },
                "reminders": {
                    "useDefault": False,
                    "overrides": [
                        {
                            "method": "popup",
                            "minutes": minutes,
                        }
                    ],
                },
            }

            total_attempted += 1

            for attempt in range(MAX_RETRIES):
                try:
                    action = insert_event_handling_deleted(
                        service,
                        calendar_id,
                        body,
                    )
                    break

                except HttpError as exc:
                    status = getattr(exc.resp, "status", None)
                    message = str(exc).lower()

                    rate_limited = (
                        status == 403
                        and (
                            "ratelimitexceeded" in message
                            or "userratelimitexceeded" in message
                        )
                    )

                    retryable = rate_limited or status in (
                        429,
                        500,
                        502,
                        503,
                        504,
                    )

                    if not retryable or attempt == MAX_RETRIES - 1:
                        raise

                    sleep_seconds = BASE_SLEEP * (2 ** attempt)

                    print(
                        f"RETRYABLE GOOGLE ERROR: status={status}; "
                        f"retrying in {sleep_seconds:.1f}s "
                        f"(attempt {attempt + 1}/{MAX_RETRIES})",
                        flush=True,
                    )

                    time.sleep(sleep_seconds)

            totals[action] += 1

            label = (
                "SKIPPED (unchanged)"
                if action == "skipped"
                else action.upper()
            )

            print(
                f"{label}: {current_date:%Y-%m-%d} "
                f"{prayer:<8} at {starts_at:%H:%M}",
                flush=True,
            )

    print("\nMember sync complete.", flush=True)
    print(f"Total attempted: {total_attempted}", flush=True)
    print(f"Created:         {totals['created']}", flush=True)
    print(f"Updated:         {totals['updated']}", flush=True)
    print(f"Skipped:         {totals['skipped']}", flush=True)

    mark_member_synced(member_id, calendar_id)


def main(all_members=False, member_id=None):
    (
        config,
        timezone_obj,
        start_date,
        end_date,
        day_count,
        prayer_times,
    ) = build_prayer_times_and_dates()

    print(
        f"Timetable covers: {start_date} through {end_date}",
        flush=True,
    )

    if all_members and member_id is not None:
        raise ValueError(
            "Use either all_members or member_id, not both."
        )

    if all_members:
        members = fetch_connected_members()

        print(
            f"Weekly sync: connected members found: {len(members)}",
            flush=True,
        )

    elif member_id is not None:
        response = (
            supabase.table("prayer_members")
            .select("*")
            .eq("id", member_id)
            .eq("connection_status", "connected")
            .execute()
        )

        members = response.data or []

        print(
            f"Single-member sync: member_id={member_id}, "
            f"connected members found: {len(members)}",
            flush=True,
        )

    else:
        raise ValueError(
            "Provide member_id for login sync, "
            "or use --all-members for the weekly cron."
        )


    successful_members = 0
    failed_members = 0

    for member in members:
        try:
            sync_member(
                member,
                config,
                timezone_obj,
                start_date,
                day_count,
                prayer_times,
            )

            successful_members += 1

        except RefreshError as exc:
            failed_members += 1

            invalid_grant = any(
                isinstance(arg, dict)
                and arg.get("error") == "invalid_grant"
                for arg in exc.args
            )

            if invalid_grant:
                try:
                    mark_member_reconnect_required(member["id"])
                except Exception as update_error:
                    print(
                        "Could not save reconnect-required status "
                        f"for {member.get('email')}: "
                        f"{type(update_error).__name__}: "
                        f"{update_error}",
                        flush=True,
                    )

                print(
                    f"RECONNECT REQUIRED: {member.get('email')} — "
                    "Google refresh token is no longer valid. "
                    "Member must reconnect.",
                    flush=True,
                )
            else:
                print(
                    f"Token refresh failed for {member.get('email')}: "
                    "not a confirmed invalid_grant; "
                    "connection status unchanged.",
                    flush=True,
                )

        except HttpError as exc:
            failed_members += 1
            status = getattr(exc.resp, "status", None)

            print(
                f"Google API error for {member.get('email')}: "
                f"status={status}, message={exc}",
                flush=True,
            )

        except Exception as exc:
            failed_members += 1

            print(
                f"Sync failed for {member.get('email')}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

    print(
        "\nRun complete: "
        f"selected={len(members)}, "
        f"successful={successful_members}, "
        f"failed={failed_members}",
        flush=True,
    )

    if failed_members:
        raise RuntimeError(
            f"Synchronization failed for {failed_members} member(s). "
            "See the member-level errors above."
        )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Synchronize prayer timetable calendar events."
    )
    parser.add_argument(
        "--all-members",
        action="store_true",
        help=(
            "Synchronize all connected members, "
            "including those already initially synced."
        ),
    )
    args = parser.parse_args()

    main(all_members=args.all_members)
