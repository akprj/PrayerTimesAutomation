import hashlib
import json
import os
import re
import time
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
            lang="eng",
        )
    finally:
        date_image.close()

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

    pattern = (
        r"\b(Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag)"
        r"\s*,?\s*dem\s+(\d{1,2})\s*\.\s*([A-Za-zÄÖÜäöü]+)"
    )

    matches = re.findall(pattern, date_text, flags=re.IGNORECASE)
    if len(matches) != 2:
        raise RuntimeError("Could not identify both timetable dates.")

    dates = []
    for weekday, day, month_text in matches:
        month_name = month_text.lower()
        month = months.get(month_name) or months.get(month_name[:3])
        if month is None:
            raise RuntimeError(f"Unrecognised month: {month_text}")

        parsed_date = date(year, month, int(day))

        if parsed_date.weekday() != weekdays[weekday.lower()]:
            raise RuntimeError(f"Weekday mismatch for {parsed_date}")

        dates.append(parsed_date)

    start_date, end_date = dates
    day_count = (end_date - start_date).days + 1

    if day_count != 7:
        raise RuntimeError(
            f"Expected 7 days in timetable, found {day_count}."
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
        calendars.extend(response.get("items", []))
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


def mark_member_reconnect_required(member_id: str):
    supabase.table("prayer_members").update(
        {
            "connection_status": "reconnect_required",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", member_id).execute()


def insert_event_handling_deleted(service, calendar_id, body):
    original_id = body["id"]
    candidate_id = original_id

    for generation in range(50):
        candidate_body = dict(body)
        candidate_body["id"] = candidate_id

        try:
            return service.events().insert(
                calendarId=calendar_id,
                body=candidate_body,
            ).execute()

        except HttpError as insert_error:
            insert_status = getattr(
                insert_error.resp, "status", None
            )

            if insert_status != 409:
                raise

            # A conflict does not necessarily mean an active event.
            # Inspect the record associated with this ID.
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
                    # Do not create a duplicate if the lookup
                    # failed for an unknown reason.
                    raise

            event_status = existing.get("status", "unknown")

            print(
                f"ID CONFLICT: {body['summary']} "
                f"on {body['start']['dateTime']} "
                f"-> Google status: {event_status}",
                flush=True,
            )

            if event_status != "cancelled":
                # Let the existing outer handler skip this event.
                raise insert_error

            # Deleted IDs may remain reserved by Google.
            # Use a repeatable replacement ID instead.
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

    raise RuntimeError(
        "Reached the replacement-ID limit for a deleted event."
    )


def sync_member(member, config, timezone_obj, start_date, day_count, prayer_times):
    email = member.get("email") or "(unknown)"
    member_id = member["id"]

    print(f"\n--- Syncing member: {email} ---")

    refresh_token = decrypt_refresh_token(member["refresh_token_encrypted"])
    credentials = build_member_credentials(refresh_token)

    service = build(
        "calendar",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )

    calendar_id = member.get("calendar_id")
    if not calendar_id:
        calendar_id = choose_target_calendar(service)

    print(f"Using calendar_id: {calendar_id}")
    print(f"Timezone: {timezone_obj.key}")
    print(f"Date range: {start_date}")

    reminder_minutes = config["reminder_minutes"]
    friday_replaces_zohar = config.get("friday_replaces_zohar", False)
    regular_prayers = ["Fajr", "Zohar", "Assr", "Maghrib", "Ishaa"]
    duration_minutes = 5

    total_attempted = 0
    total_created = 0
    total_skipped = 0

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
            end_at = starts_at + timedelta(minutes=duration_minutes)
            minutes = reminder_minutes[prayer]

            event_id = stable_event_id(calendar_id, current_date, prayer)

            body = {
                "id": event_id,
                "summary": prayer,
                "description": (
                    f"Prayer Timings Automation: start time {starts_at:%H:%M} "
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
                        {"method": "popup", "minutes": minutes}
                    ],
                },
            }

            total_attempted += 1

            try:
                last_err = None
                for attempt in range(MAX_RETRIES):
                    try:
                        insert_event_handling_deleted(
                            service,
                            calendar_id,
                            body,
                        )

                        total_created += 1
                        print(
                            f"CREATED: {current_date:%Y-%m-%d} "
                            f"{prayer:<8} at {starts_at:%H:%M}"
                        )
                        last_err = None
                        break

                    except HttpError as exc:
                        last_err = exc
                        status = getattr(exc.resp, "status", None)
                        message = str(exc)

                        if status == 403 and "rateLimitExceeded" in message:
                            sleep_seconds = BASE_SLEEP * (2 ** attempt)
                            print(
                                f"RATE LIMITED -> retrying in "
                                f"{sleep_seconds:.1f}s "
                                f"(attempt {attempt + 1}/{MAX_RETRIES})"
                            )
                            time.sleep(sleep_seconds)
                            continue

                        raise

                if last_err is not None:
                    raise last_err

            except HttpError as exc:
                if getattr(exc.resp, "status", None) == 409:
                    total_skipped += 1
                    print(
                        f"SKIPPED (exists): {current_date:%Y-%m-%d} "
                        f"{prayer:<8} at {starts_at:%H:%M}"
                    )
                else:
                    raise


    print("\nMember sync complete.")
    print(f"Total attempted: {total_attempted}")
    print(f"Created:         {total_created}")
    print(f"Skipped:         {total_skipped}")

    mark_member_synced(member_id, calendar_id)


def main():
    config, timezone_obj, start_date, end_date, day_count, prayer_times = (
        build_prayer_times_and_dates()
    )

    print(f"Timetable covers: {start_date} through {end_date}")

    members = fetch_pending_members()
    print(f"Pending members found: {len(members)}")

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
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            print(
                f"Google API error for {member.get('email')}: "
                f"status={status}, message={exc}"
            )
            if status in (400, 401):
                mark_member_reconnect_required(member["id"])
        except Exception as exc:
            print(
                f"Sync failed for {member.get('email')}: "
                f"{type(exc).__name__}: {exc}"
            )


if __name__ == "__main__":
    main()
