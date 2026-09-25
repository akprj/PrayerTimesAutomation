import hashlib
import json
import os
import re
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import pytesseract
from PIL import Image, ImageOps
from drive_timetable import fetch_latest_timetable

BASE_DIR = Path(__file__).resolve().parent
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]

def stable_event_id(calendar_id: str, current_date: date, prayer: str) -> str:
    # Stable ID prevents duplicates on reruns within the chosen calendar
    event_key = f"prayer-timings|{calendar_id}|{current_date.isoformat()}|{prayer}"
    return hashlib.sha256(event_key.encode("utf-8")).hexdigest()


def build_prayer_times_and_dates():
    config = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    timezone = ZoneInfo(config["timezone"])
    year = datetime.now(timezone).year

    if os.name == "nt":
        pytesseract.pytesseract.tesseract_cmd = (
            r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        )
    else:
        pytesseract.pytesseract.tesseract_cmd = "tesseract"


        # Fetch the newest timetable once, without saving it locally.
    stream, metadata = fetch_latest_timetable()

    print(f"Using Drive timetable: {metadata.get('name')}")
    print(f"Created time: {metadata.get('createdTime')}")

    # Create an independent image before closing the downloaded stream.
    with stream:
        with Image.open(stream) as source:
            timetable_image = ImageOps.exif_transpose(source).convert("RGB")

    # Read the date-range text from the same image used for prayer times.
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


    MONTHS = {
        "jan": 1, "feb": 2,
        "mär": 3, "mar": 3, "maerz": 3,
        "apr": 4, "mai": 5, "may": 5,
        "jun": 6, "jul": 7, "aug": 8,
        "sep": 9, "okt": 10, "oct": 10,
        "nov": 11, "dez": 12, "dec": 12,
    }

    WEEKDAYS = {
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
        raise SystemExit("Could not identify both dates. Review needed.")

    dates = []
    for weekday, day, month_text in matches:
        name = month_text.lower()
        month = MONTHS.get(name) or MONTHS.get(name[:3])
        if month is None:
            raise SystemExit(f"Unrecognised month: {month_text}")

        parsed_date = date(year, month, int(day))

        if parsed_date.weekday() != WEEKDAYS[weekday.lower()]:
            raise SystemExit(f"Weekday mismatch for {parsed_date}. Review needed.")

        dates.append(parsed_date)

    start_date, end_date = dates
    day_count = (end_date - start_date).days + 1

    if day_count != 7:
        raise SystemExit(
            f"Expected 7 days, found {day_count}. Review the date range before proceeding."
        )

    ROWS = [
        ("Fajr",    0.175, 0.255),
        ("Zohar",   0.275, 0.355),
        ("Assr",    0.375, 0.455),
        ("Maghrib", 0.480, 0.560),
        ("Ishaa",   0.580, 0.660),
        ("Juma",    0.685, 0.765),
    ]

    prayer_times = {}
    with timetable_image as image:
        width, height = image.size

        for prayer, top, bottom in ROWS:
            crop = image.crop((
                int(width * 0.36),
                int(height * top),
                int(width * 0.67),
                int(height * bottom),
            ))

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
                raise SystemExit(
                    f"Invalid OCR time for {prayer}: {result!r}. Review needed."
                )

            prayer_times[prayer] = datetime.strptime(result, "%H:%M").time()

    return config, timezone, start_date, end_date, day_count, prayer_times


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


MAX_RETRIES = 8
base_sleep = 1.0


def main(user_email):
    config, timezone, start_date, end_date, day_count, prayer_times = build_prayer_times_and_dates()

    reminder_minutes = config["reminder_minutes"]
    friday_replaces_zohar = config.get("friday_replaces_zohar", False)

    safe_email = user_email.replace("@", "_at_").replace(".", "_")
    token_path = BASE_DIR / "tokens" / f"{safe_email}.json"

    if not token_path.exists():
        raise SystemExit(f"Token file not found: {token_path}")

    credentials = Credentials.from_authorized_user_file(str(token_path))

    if not credentials.valid:
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            token_path.write_text(credentials.to_json(), encoding="utf-8")
        else:
            raise SystemExit(
                f"Authorization is not valid for {user_email}. "
                f"Please reconnect Google Calendar."
            )

    service = build("calendar", "v3", credentials=credentials)
    
    # Identify the primary calendar for the authenticated Google user.
    primary_calendar = service.calendars().get(
    calendarId="primary"
    ).execute()

    print(
    "Connected Google account (primary calendar):",
    primary_calendar.get("id", "Unknown")
    )

    calendar_id = choose_target_calendar(service)

    print(f"CREATE SCHEDULE | Timezone: {timezone.key}")
    print(f"Date range: {start_date} through {end_date}")

    regular_prayers = ["Fajr", "Zohar", "Assr", "Maghrib", "Ishaa"]
    duration_minutes = 5

    total_attempted = 0
    total_created = 0
    total_skipped = 0

    for offset in range(day_count):
        current_date = start_date + timedelta(days=offset)

        prayers = regular_prayers.copy()
        if current_date.weekday() == 4:  # Friday
            if friday_replaces_zohar:
                prayers[prayers.index("Zohar")] = "Juma"
            else:
                prayers.append("Juma")

        for prayer in prayers:
            starts_at = datetime.combine(
                current_date,
                prayer_times[prayer],
                tzinfo=timezone,
            )
            minutes = reminder_minutes[prayer]
            end_at = starts_at + timedelta(minutes=duration_minutes)

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
                    "timeZone": timezone.key,
                },
                "end": {
                    "dateTime": end_at.isoformat(),
                    "timeZone": timezone.key,
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
                        service.events().insert(
                            calendarId=calendar_id,
                            body=body,
                        ).execute()

                        total_created += 1
                        print(
                            f"CREATED: {current_date:%Y-%m-%d} "
                            f"{prayer:<8} at {starts_at:%H:%M}"
                        )
                        last_err = None
                        break

                    except HttpError as e:
                        last_err = e
                        status = getattr(e.resp, "status", None)
                        msg = str(e)

                        if status == 403 and "rateLimitExceeded" in msg:
                            sleep_s = base_sleep * (2 ** attempt)
                            print(
                                f"RATE LIMITED -> retrying in {sleep_s:.1f}s "
                                f"(attempt {attempt + 1}/{MAX_RETRIES})"
                            )
                            time.sleep(sleep_s)
                            continue

                        raise

                if last_err is not None:
                    raise last_err

            except Exception as e:
                msg = str(e)
                if "409" in msg:
                    total_skipped += 1
                    print(
                        f"SKIPPED (exists): {current_date:%Y-%m-%d} "
                        f"{prayer:<8} at {starts_at:%H:%M}"
                    )
                else:
                    raise

    print("\nDone.")
    print(f"Total attempted: {total_attempted}")
    print(f"Created:         {total_created}")
    print(f"Skipped:         {total_skipped}")


if __name__ == "__main__":
    main("noumanahmad1987@gmail.com")
