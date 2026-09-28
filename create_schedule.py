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

def normalize_ocr_text(text: str) -> str:
    text = text.lower()

    replacements = {
        "gültig": "gultig",
        "gültig": "gultig",
        "giltig": "gultig",
        "guitig": "gultig",
        "okt.": "okt",
        "sep.": "sep",
        "nov.": "nov",
        "dez.": "dez",
        "mär.": "mär",
        "maerz.": "maerz",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_german_date_fragment(fragment: str, year: int) -> date | None:
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

    fragment = normalize_ocr_text(fragment)

    match = re.search(
        r"(\d{1,2})\s*\.\s*([a-zA-ZäöüÄÖÜ]+)",
        fragment,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    day = int(match.group(1))
    month_text = match.group(2).lower().strip(". ")
    month = months.get(month_text) or months.get(month_text[:3])

    if month is None:
        return None

    return date(year, month, day)


def extract_labeled_date(text: str, year: int, kind: str) -> date | None:
    normalized = normalize_ocr_text(text)

    if kind == "start":
        patterns = [
            r"gultig\s*ab\s*.*?(\d{1,2}\s*\.\s*[a-zA-ZäöüÄÖÜ]+)",
            r"\bab\s*samstag.*?(\d{1,2}\s*\.\s*[a-zA-ZäöüÄÖÜ]+)",
            r"\bsamstag.*?(\d{1,2}\s*\.\s*[a-zA-ZäöüÄÖÜ]+)",
        ]
    else:
        patterns = [
            r"bis\s*zum\s*.*?(\d{1,2}\s*\.\s*[a-zA-ZäöüÄÖÜ]+)",
            r"\bbis\s*freitag.*?(\d{1,2}\s*\.\s*[a-zA-ZäöüÄÖÜ]+)",
            r"\bfreitag.*?(\d{1,2}\s*\.\s*[a-zA-ZäöüÄÖÜ]+)",
        ]

    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            parsed = parse_german_date_fragment(match.group(1), year)
            if parsed:
                return parsed

    return parse_german_date_fragment(normalized, year)


def ocr_prepared_text(image, label: str, psm: int = 6) -> str:
    prepared = ImageOps.grayscale(image)
    prepared = prepared.resize(
        (prepared.width * 4, prepared.height * 4),
        Image.Resampling.LANCZOS,
    )
    prepared = ImageOps.autocontrast(prepared)
    prepared = ImageOps.expand(prepared, border=20, fill="white")

    try:
        text = pytesseract.image_to_string(
            prepared,
            lang="deu+eng",
            config=f"--psm {psm}",
        )
        print(f"DEBUG {label} OCR TEXT:")
        print(repr(text))
        return text
    finally:
        prepared.close()


def resolve_timetable_dates(image, year: int):
    width, height = image.size

    candidate_boxes = [
        (
            "DATE BLOCK A",
            (
                int(width * 0.00),
                int(height * 0.875),
                int(width * 1.00),
                int(height * 0.995),
            ),
        ),
        (
            "DATE BLOCK B",
            (
                int(width * 0.02),
                int(height * 0.870),
                int(width * 0.98),
                int(height * 0.995),
            ),
        ),
        (
            "DATE BLOCK C",
            (
                int(width * 0.00),
                int(height * 0.865),
                int(width * 1.00),
                int(height * 0.990),
            ),
        ),
    ]

    best_start = None
    best_end = None
    seen_debug = []

    for block_label, box in candidate_boxes:
        date_block = image.crop(box)

        try:
            full_text = ocr_prepared_text(date_block, f"{block_label} FULL", psm=6)
            seen_debug.append(f"{block_label} FULL={full_text!r}")

            full_start = extract_labeled_date(full_text, year, "start")
            full_end = extract_labeled_date(full_text, year, "end")

            half_height = date_block.height // 2
            half_start = None
            half_end = None

            start_crop = date_block.crop((0, 0, date_block.width, half_height))
            end_crop = date_block.crop((0, half_height, date_block.width, date_block.height))

            try:
                start_text = ocr_prepared_text(
                    start_crop,
                    f"{block_label} START HALF",
                    psm=6,
                )
                seen_debug.append(f"{block_label} START={start_text!r}")
                half_start = extract_labeled_date(start_text, year, "start")
            finally:
                start_crop.close()

            try:
                end_text = ocr_prepared_text(
                    end_crop,
                    f"{block_label} END HALF",
                    psm=6,
                )
                seen_debug.append(f"{block_label} END={end_text!r}")
                half_end = extract_labeled_date(end_text, year, "end")
            finally:
                end_crop.close()

        finally:
            date_block.close()

        start_candidate = half_start or full_start
        end_candidate = half_end or full_end

        if start_candidate and start_candidate.weekday() == 5 and not best_start:
            best_start = start_candidate

        if end_candidate and end_candidate.weekday() == 4 and not best_end:
            best_end = end_candidate

        if best_start and best_end:
            break

    if best_start and best_end:
        if (best_end - best_start).days != 6:
            print(
                f"WARNING: OCR found both dates but span was not 7 days: "
                f"{best_start} -> {best_end}. Rebuilding end from start."
            )
            best_end = best_start + timedelta(days=6)

    elif best_start and not best_end:
        best_end = best_start + timedelta(days=6)

    elif best_end and not best_start:
        best_start = best_end - timedelta(days=6)

    if not best_start or not best_end:
        raise RuntimeError(
            "Could not resolve timetable dates from OCR crops. "
            + " | ".join(seen_debug)
        )

    return best_start, best_end


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

    start_date, end_date = resolve_timetable_dates(
        timetable_image,
        year,
    )

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


def fetch_connected_members():
    response = (
        supabase.table("prayer_members")
        .select("*")
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


def upsert_event_handling_deleted(service, calendar_id, body):
    original_id = body["id"]
    candidate_id = original_id

    for generation in range(50):
        candidate_body = dict(body)
        candidate_body["id"] = candidate_id

        try:
            return service.events().insert(
                calendarId=calendar_id,
                body=candidate_body,
            ).execute(), "created"

        except HttpError as insert_error:
            insert_status = getattr(insert_error.resp, "status", None)

            if insert_status != 409:
                raise

            try:
                existing = service.events().get(
                    calendarId=calendar_id,
                    eventId=candidate_id,
                ).execute()

                print(
                    f"EXISTS -> updating: {candidate_body['summary']} "
                    f"on {candidate_body['start']['dateTime']}",
                    flush=True,
                )

                updated_body = dict(candidate_body)
                updated_body["id"] = candidate_id

                return service.events().update(
                    calendarId=calendar_id,
                    eventId=candidate_id,
                    body=updated_body,
                ).execute(), "updated"

            except HttpError as lookup_error:
                lookup_status = getattr(lookup_error.resp, "status", None)

                if lookup_status == 410:
                    existing = {"status": "cancelled"}
                elif lookup_status == 404:
                    existing = None
                else:
                    raise

            event_status = (
                existing.get("status", "unknown")
                if existing else "not_found"
            )

            print(
                f"ID CONFLICT: {body['summary']} "
                f"on {body['start']['dateTime']} "
                f"-> Google status: {event_status}",
                flush=True,
            )

            if existing and event_status != "cancelled":
                updated_body = dict(candidate_body)
                updated_body["id"] = candidate_id

                print(
                    f"EXISTS -> updating after conflict check: "
                    f"{candidate_body['summary']} "
                    f"on {candidate_body['start']['dateTime']}",
                    flush=True,
                )

                return service.events().update(
                    calendarId=calendar_id,
                    eventId=candidate_id,
                    body=updated_body,
                ).execute(), "updated"

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
    total_updated = 0


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

            last_err = None
            for attempt in range(MAX_RETRIES):
                try:
                    _, action = upsert_event_handling_deleted(
                        service,
                        calendar_id,
                        body,
                    )

                    if action == "created":
                        total_created += 1
                        print(
                            f"CREATED: {current_date:%Y-%m-%d} "
                            f"{prayer:<8} at {starts_at:%H:%M}"
                        )
                    else:
                        total_updated += 1
                        print(
                            f"UPDATED: {current_date:%Y-%m-%d} "
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


    print("\nMember sync complete.")
    print(f"Total attempted: {total_attempted}")
    print(f"Created:         {total_created}")
    print(f"Updated:         {total_updated}")


    mark_member_synced(member_id, calendar_id)


def main():
    config, timezone_obj, start_date, end_date, day_count, prayer_times = (
        build_prayer_times_and_dates()
    )

    print(f"Timetable covers: {start_date} through {end_date}")

    members = fetch_connected_members()
    print(f"Connected members found: {len(members)}")


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
