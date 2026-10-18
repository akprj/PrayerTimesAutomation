import hashlib
import json
import os
import re
import time
from google.auth.exceptions import GoogleAuthError, RefreshError
from datetime import date, datetime, timedelta, timezone
from contextlib import ExitStack
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
from timetable_dates import (
    parse_timetable_endpoints,
    resolve_timetable_dates,
)


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


PRAYER_OCR_ALIASES = {
    "fajr": "Fajr",
    "fair": "Fajr",
    "zohar": "Zohar",
    "zuhr": "Zohar",
    "dhuhr": "Zohar",
    "assr": "Assr",
    "asr": "Assr",
    "maghrib": "Maghrib",
    "ishaa": "Ishaa",
    "isha": "Ishaa",
    "juma": "Juma",
    "jumma": "Juma",
    "jumuah": "Juma",
}


def normalize_prayer_label(text):
    return re.sub(r"[^a-z]", "", text.lower())


def parse_ocr_time(text):
    compact = re.sub(r"\s+", "", text)

    if not re.fullmatch(r"\d{1,2}:\d{2}", compact):
        return None

    try:
        return datetime.strptime(compact, "%H:%M").time()
    except ValueError:
        return None


def fetch_latest_prayer_event_date(service, calendar_id, timezone_obj):
    time_min = (
        datetime.now(timezone.utc) - timedelta(days=3650)
    ).isoformat().replace("+00:00", "Z")
    page_token = None
    latest_event_date = None
    prayer_names = {
        "Fajr",
        "Zohar",
        "Assr",
        "Maghrib",
        "Ishaa",
        "Juma",
    }

    try:
        while True:
            response = service.events().list(
                calendarId=calendar_id,
                timeMin=time_min,
                maxResults=2500,
                pageToken=page_token,
                singleEvents=True,
                fields=(
                    "items(summary,description,start),nextPageToken"
                ),
            ).execute()

            for event in response.get("items", []):
                if (
                    event.get("summary") not in prayer_names
                    or "Prayer Timings Automation:"
                    not in event.get("description", "")
                ):
                    continue

                event_start = event.get("start", {})
                if event_start.get("date"):
                    event_date = date.fromisoformat(
                        event_start["date"]
                    )
                elif event_start.get("dateTime"):
                    event_date = datetime.fromisoformat(
                        event_start["dateTime"].replace(
                            "Z", "+00:00"
                        )
                    ).astimezone(timezone_obj).date()
                else:
                    continue

                if (
                    latest_event_date is None
                    or event_date > latest_event_date
                ):
                    latest_event_date = event_date

            page_token = response.get("nextPageToken")
            if not page_token:
                break

    except (GoogleAuthError, HttpError, OSError, ValueError) as exc:
        print(
            f"DATE HISTORY LOOKUP FAILED for {calendar_id!r}: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        raise RuntimeError(
            "Could not read prior prayer events for the OCR date fallback."
        ) from exc

    if latest_event_date is None:
        print(
            f"DATE HISTORY LOOKUP: no prior prayer events found in "
            f"{calendar_id!r}.",
            flush=True,
        )
    else:
        print(
            f"DATE HISTORY LOOKUP: latest prayer event in "
            f"{calendar_id!r} is on {latest_event_date}.",
            flush=True,
        )

    return latest_event_date


def extract_original_whole_image_times(text):
    candidates = {}

    for line in text.splitlines():
        # Only accept an explicit prayer/time pair on the same text line.
        match = re.fullmatch(
            r"\s*([A-Za-z'’.-]+)\s*[:|–-]?\s*"
            r"(\d{1,2}\s*:\s*\d{2})\s*(?:Uhr)?\s*",
            line,
            flags=re.IGNORECASE,
        )

        if not match:
            continue

        prayer = PRAYER_OCR_ALIASES.get(
            normalize_prayer_label(match.group(1))
        )
        prayer_time = parse_ocr_time(match.group(2))

        if prayer and prayer_time is not None:
            candidates.setdefault(prayer, set()).add(prayer_time)

    return candidates

def extract_prayer_band_times(image, words):
    # "words" contains coordinates from the 4x enhanced whole-image scan.
    scale = 4

    start_labels = {"fa", "fair", "fajr"}
    end_labels = {"juma", "ju", "jum", "jumma", "jumuah"}

    start_words = [
        word
        for word in words
        if normalize_prayer_label(word["text"]) in start_labels
        and word["confidence"] >= 30
    ]

    end_words = [
        word
        for word in words
        if normalize_prayer_label(word["text"]) in end_labels
        and word["confidence"] >= 30
    ]

    if not start_words or not end_words:
        print(
            "PRAYER BAND OCR SKIPPED: "
            "could not locate both a Fajr/Fair/Fa start anchor "
            "and a Juma/Ju/Jum end anchor.",
            flush=True,
        )
        return {}

    anchor_pairs = []

    for start in start_words:
        for end in end_words:
            if end["center_y"] <= start["center_y"]:
                continue

            # Prayer labels should be in approximately the same column.
            horizontal_tolerance = 3 * max(
                start["height"],
                end["height"],
            )

            if abs(start["left"] - end["left"]) > horizontal_tolerance:
                continue

            anchor_pairs.append((start, end))

    if len(anchor_pairs) != 1:
        print(
            "PRAYER BAND OCR SKIPPED: "
            f"expected one reliable anchor pair, found {len(anchor_pairs)}.",
            flush=True,
        )
        return {}

    (start, end), = anchor_pairs
    
    # Include padding so the first and last rows are not clipped.
    padding = max(start["height"], end["height"])

    top = max(
        0,
        int((start["top"] - padding) / scale),
    )

    bottom = min(
        image.height,
        int(
            (end["top"] + end["height"] + padding) / scale
        ) + 1,
    )

    if bottom <= top:
        print(
            "PRAYER BAND OCR SKIPPED: invalid crop boundaries.",
            flush=True,
        )
        return {}

    print(
        "PRAYER BAND OCR: "
        f"start={start['text']!r}, end={end['text']!r}, "
        f"full-width crop={(0, top, image.width, bottom)!r}",
        flush=True,
    )

    with image.crop((0, top, image.width, bottom)) as band:
        return extract_second_whole_image_times(
            band,
            allow_band_retry=False,
        )


def extract_second_whole_image_times(image, *, allow_band_retry=True):
    aliases = dict(PRAYER_OCR_ALIASES)

    if not allow_band_retry:
        aliases.update(
            {
                "fa": "Fajr",
                "ju": "Juma",
                "jum": "Juma",
            }
        )

    # This timetable has dark text and a pale watermark.
    # Thresholding aims to suppress the watermark before sparse-text OCR.
    prepared = ImageOps.grayscale(image)

    try:
        resized = prepared.resize(
            (prepared.width * 4, prepared.height * 4),
            Image.Resampling.LANCZOS,
        )
    finally:
        prepared.close()


    try:
        contrasted = ImageOps.autocontrast(resized)
    finally:
        resized.close()

    try:
        binary = contrasted.point(
            lambda pixel: 0 if pixel < 145 else 255
        )
    finally:
        contrasted.close()

    try:
        # Whole image: sparse text.
        # Cropped prayer band: a single text block.
        psm = 11 if allow_band_retry else 6

        data = pytesseract.image_to_data(
            binary,
            lang="eng+deu",
            config=f"--oem 3 --psm {psm}",
            output_type=pytesseract.Output.DICT,
        )
        
    finally:
        binary.close()

    words = []

    for index, raw_text in enumerate(data["text"]):
        text = raw_text.strip()

        if not text:
            continue

        left = int(data["left"][index])
        top = int(data["top"][index])
        width = int(data["width"][index])
        height = int(data["height"][index])

        words.append(
            {
                "text": text,
                "left": left,
                "right": left + width,
                "top": top,
                "height": height,
                "center_y": top + height / 2,
                "confidence": float(data["conf"][index]),
            }
        )

    print(
        "SECOND WHOLE IMAGE OCR TOKENS: "
        f"{[word['text'] for word in words]!r}",
        flush=True,
    )
    
    candidates = {}

    for label in words:
        prayer = aliases.get(
            normalize_prayer_label(label["text"])
        )


        if prayer is None:
            continue

        if label["confidence"] < 30:
            print(
                f"SECOND WHOLE IMAGE OCR [{prayer}]: "
                "label confidence too low; ignoring.",
                flush=True,
            )
            continue

        right_hand_words = sorted(
            (
                word
                for word in words
                if word["left"] >= label["right"]
                and abs(word["center_y"] - label["center_y"])
                <= 0.45 * max(word["height"], label["height"])
            ),
            key=lambda word: word["left"],
        )

        row_times = set()

        for start in range(len(right_hand_words)):
            # Support "06:40", "06:" + "40", and "06" + ":" + "40".
            for length in (1, 2, 3):
                parts = right_hand_words[start:start + length]

                if len(parts) != length:
                    continue

                if any(part["confidence"] < 30 for part in parts):
                    continue

                # Do not join widely separated pieces of text.
                if any(
                    second["left"] - first["right"]
                    > 1.5 * max(
                        first["height"],
                        second["height"],
                        label["height"],
                    )
                    for first, second in zip(parts, parts[1:])
                ):
                    continue

                combined = "".join(part["text"] for part in parts)
                prayer_time = parse_ocr_time(combined)

                if prayer_time is not None:
                    row_times.add(prayer_time)

        if row_times:
            candidates.setdefault(prayer, set()).update(row_times)

        print(
            f"SECOND WHOLE IMAGE OCR [{prayer}] "
            f"RIGHT-HAND TEXT: "
            f"{[word['text'] for word in right_hand_words]!r}; "
            f"TIME CANDIDATES: "
            f"{sorted(value.strftime('%H:%M') for value in row_times)!r}",
            flush=True,
        )

    if allow_band_retry:
        expected_prayers = (
            "Fajr",
            "Zohar",
            "Assr",
            "Maghrib",
            "Ishaa",
            "Juma",
        )

        unresolved_prayers = [
            prayer
            for prayer in expected_prayers
            if len(candidates.get(prayer, set())) != 1
        ]

        if unresolved_prayers:
            print(
                "SECOND WHOLE IMAGE OCR: "
                "missing or ambiguous name/time pairs for "
                + ", ".join(unresolved_prayers)
                + "; attempting full-width prayer-band OCR.",
                flush=True,
            )

            try:
                band_candidates = extract_prayer_band_times(
                    image,
                    words,
                )
            except pytesseract.TesseractError as exc:
                print(
                    f"PRAYER BAND OCR FAILED: {exc}",
                    flush=True,
                )
                band_candidates = {}

            for prayer, times in band_candidates.items():
                candidates.setdefault(prayer, set()).update(times)

            formatted_candidates = {
                prayer: sorted(
                    value.strftime("%H:%M") for value in times
                )
                for prayer, times in candidates.items()
            }

            print(
                "SECOND WHOLE IMAGE OCR WITH BAND RETRY: "
                f"{formatted_candidates!r}",
                flush=True,
            )

    return candidates


def select_whole_image_fallback(prayer, original, second):
    original_times = original.get(prayer, set())
    second_times = second.get(prayer, set())
    combined = original_times | second_times

    # Reject ambiguous results, including disagreements between scans.
    if len(combined) > 1:
        print(
            f"WHOLE IMAGE FALLBACK [{prayer}] REJECTED: "
            f"conflicting times "
            f"{sorted(value.strftime('%H:%M') for value in combined)!r}",
            flush=True,
        )
        return None

    if not combined:
        return None

    prayer_time = next(iter(combined))
    source = (
        "original whole-image scan"
        if prayer_time in original_times
        else "second whole-image scan"
    )

    print(
        f"WHOLE IMAGE FALLBACK [{prayer}] VALIDATED TIME: "
        f"{prayer_time.strftime('%H:%M')} from {source}",
        flush=True,
    )

    return prayer_time


def extract_flexible_prayer_row_time(image, prayer, top, bottom):
    height = image.height
    padding = max(1, int(height * 0.01))
    row_top = max(0, int(height * top) - padding)
    row_bottom = min(height, int(height * bottom) + padding)

    print(
        f"FLEXIBLE ROW OCR [{prayer}]: scanning full-width row "
        f"{(0, row_top, image.width, row_bottom)!r}.",
        flush=True,
    )

    with image.crop((0, row_top, image.width, row_bottom)) as row:
        grayscale = ImageOps.grayscale(row)

        try:
            enlarged = grayscale.resize(
                (grayscale.width * 5, grayscale.height * 5),
                Image.Resampling.LANCZOS,
            )
        finally:
            grayscale.close()

    try:
        contrasted = ImageOps.autocontrast(enlarged)
    finally:
        enlarged.close()

    variants = [("contrast", contrasted)]
    variants.extend(
        (
            f"threshold-{threshold}",
            contrasted.point(
                lambda pixel: 0 if pixel < threshold else 255
            ),
        )
        for threshold in (145, 180, 210)
    )
    votes = {}

    try:
        for variant_name, variant in variants:
            try:
                text = pytesseract.image_to_string(
                    variant,
                    lang="eng+deu",
                    config=(
                        "--oem 3 --psm 7 "
                        "-c tessedit_char_whitelist=0123456789:.,"
                    ),
                )
            except pytesseract.TesseractError as exc:
                print(
                    f"FLEXIBLE ROW OCR [{prayer}] "
                    f"{variant_name} FAILED: {exc}",
                    flush=True,
                )
                continue

            candidates = set()

            for match in re.finditer(
                r"(?<!\d)(\d{1,2})\s*[:.,]\s*(\d{2})(?!\d)",
                text,
            ):
                normalized = (
                    f"{match.group(1)}:{match.group(2)}"
                )
                prayer_time = parse_ocr_time(normalized)

                if prayer_time is not None:
                    candidates.add(prayer_time)

            print(
                f"FLEXIBLE ROW OCR [{prayer}] {variant_name}: "
                f"{sorted(value.strftime('%H:%M') for value in candidates)!r}",
                flush=True,
            )

            for candidate in candidates:
                votes[candidate] = votes.get(candidate, 0) + 1
    finally:
        for _, variant in variants:
            variant.close()

    if not votes:
        print(
            f"FLEXIBLE ROW OCR [{prayer}] found no valid time.",
            flush=True,
        )
        return None

    highest_vote_count = max(votes.values())
    winners = [
        candidate
        for candidate, count in votes.items()
        if count == highest_vote_count
    ]

    if highest_vote_count < 2 or len(winners) != 1:
        formatted_votes = {
            value.strftime("%H:%M"): count
            for value, count in votes.items()
        }
        print(
            f"FLEXIBLE ROW OCR [{prayer}] inconclusive: "
            f"{formatted_votes!r}",
            flush=True,
        )
        return None

    prayer_time = winners[0]
    print(
        f"FLEXIBLE ROW OCR [{prayer}] CONSENSUS TIME: "
        f"{prayer_time.strftime('%H:%M')} "
        f"({highest_vote_count}/{len(variants)} scans).",
        flush=True,
    )
    return prayer_time


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
    
    print(f"WHOLE IMAGE OCR SCAN TEXT FOR WEEKSPAN: {date_text!r}", flush=True)

    original_whole_image_times = extract_original_whole_image_times(
        date_text
    )


    date_endpoints = []
    start_date = end_date = day_count = None
    try:
        date_endpoints = parse_timetable_endpoints(date_text)
        start_date, end_date, day_count = resolve_timetable_dates(
            date_text,
            year,
            reference_date=datetime.now(timezone_obj).date(),
        )
    except RuntimeError as exc:
        if (
            any(
                endpoint.role == "start" and endpoint.day is not None
                for endpoint in date_endpoints
            )
            and any(
                endpoint.role == "end" and endpoint.day is not None
                for endpoint in date_endpoints
            )
        ):
            raise

        print(
            "DATE OCR COULD NOT RESOLVE A RANGE; will use the target "
            f"calendar's latest prayer event as a last resort: {exc}",
            flush=True,
        )

    print(
        f"DATE MATCHES: {[endpoint.text for endpoint in date_endpoints]!r}",
        flush=True,
    )

    has_start_date = any(
        endpoint.role == "start" and endpoint.day is not None
        for endpoint in date_endpoints
    )
    has_end_date = any(
        endpoint.role == "end" and endpoint.day is not None
        for endpoint in date_endpoints
    )

    if start_date is None:
        print(
            "DATE FALLBACK DEFERRED: each target calendar will be "
            "checked during synchronization.",
            flush=True,
        )
    elif has_start_date and has_end_date:
        print(
            f"DATE RANGE VERIFIED: {start_date} to {end_date} "
            f"({day_count} days).",
            flush=True,
        )
    else:
        print(
            f"DATE RANGE INFERRED: {start_date} to {end_date} "
            f"({day_count} days).",
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
    second_whole_image_times = None

    with timetable_image as image:
        width, height = image.size

        for prayer, top, bottom in rows:
            rectangle_time = None

            with ExitStack() as stack:
                crop = stack.enter_context(
                    image.crop(
                        (
                            int(width * 0.36),
                            int(height * top),
                            int(width * 0.67),
                            int(height * bottom),
                        )
                    )
                )

                crop = stack.enter_context(
                    ImageOps.grayscale(crop)
                )
                crop = stack.enter_context(
                    crop.resize(
                        (crop.width * 4, crop.height * 4),
                        Image.Resampling.LANCZOS,
                    )
                )
                crop = stack.enter_context(
                    ImageOps.autocontrast(crop)
                )
                crop = stack.enter_context(
                    ImageOps.expand(
                        crop,
                        border=20,
                        fill="white",
                    )
                )

                try:
                    result = pytesseract.image_to_string(
                        crop,
                        lang="eng+deu",
                        config=(
                            "--psm 7 "
                            "-c tessedit_char_whitelist=0123456789:"
                        ),
                    ).strip()

                    if not re.fullmatch(r"\d\d:\d\d", result):
                        raise ValueError(
                            f"Invalid OCR time format: {result!r}"
                        )

                    rectangle_time = datetime.strptime(
                        result,
                        "%H:%M",
                    ).time()

                except (pytesseract.TesseractError, ValueError) as exc:
                    print(
                        f"RECTANGLE OCR [{prayer}] FAILED: "
                        f"{type(exc).__name__}: {exc}. "
                        "Trying whole-image fallback.",
                        flush=True,
                    )

            if rectangle_time is not None:
                prayer_times[prayer] = rectangle_time

                print(
                    f"RECTANGLE OCR [{prayer}] VALIDATED TIME: "
                    f"{rectangle_time.strftime('%H:%M')}",
                    flush=True,
                )

                continue

            if second_whole_image_times is None:
                try:
                    second_whole_image_times = (
                        extract_second_whole_image_times(
                            timetable_image
                        )
                    )
                except pytesseract.TesseractError as exc:
                    print(
                        f"SECOND WHOLE IMAGE OCR FAILED: {exc}",
                        flush=True,
                    )
                    second_whole_image_times = {}

            fallback_time = select_whole_image_fallback(
                prayer,
                original_whole_image_times,
                second_whole_image_times,
            )

            if fallback_time is None:
                row_time = extract_flexible_prayer_row_time(
                    timetable_image,
                    prayer,
                    top,
                    bottom,
                )
                whole_image_times = (
                    original_whole_image_times.get(prayer, set())
                    | second_whole_image_times.get(prayer, set())
                )

                if row_time is not None and whole_image_times:
                    if whole_image_times != {row_time}:
                        formatted_times = sorted(
                            value.strftime("%H:%M")
                            for value in whole_image_times
                        )
                        print(
                            f"FLEXIBLE ROW OCR [{prayer}] REJECTED: "
                            "disagrees with whole-image candidates "
                            f"{formatted_times!r}.",
                            flush=True,
                        )
                        row_time = None

                fallback_time = row_time

            if fallback_time is None:
                raise RuntimeError(
                    f"Could not safely determine the time for {prayer}: "
                    "rectangle OCR and whole-image/flexible-row "
                    "fallbacks were missing, conflicting, or inconclusive. "
                    "Stopping before calendar synchronization."
                )

            prayer_times[prayer] = fallback_time

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

    print(f"\n--- Syncing target: {email} ---", flush=True)

    # 1. NEW INTERCEPT: Check if we are running the central Master Calendar
    if member.get("is_master_calendar"):
        # We reuse your main admin credentials securely saved in Render's environment
        admin_refresh_token = decrypt_refresh_token(
            os.environ["ADMIN_MANAGEMENT_REFRESH_TOKEN"]
        )
        credentials = build_member_credentials(admin_refresh_token)

        service = build(
            "calendar",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )
        # Directly target the public calendar string without running search matches
        calendar_id = member["calendar_id"]
    else:
        # 2. STANDARD LOGIC: Keep your existing code for individual OAuth users completely untouched
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

    if start_date is None or day_count is None:
        last_event_date = fetch_latest_prayer_event_date(
            service,
            calendar_id,
            timezone_obj,
        )
        if last_event_date is None:
            raise RuntimeError(
                "OCR could not resolve the timetable dates, and this "
                "calendar has no prior prayer event to anchor the "
                "date fallback."
            )

        start_date, end_date, day_count = resolve_timetable_dates(
            "",
            datetime.now(timezone_obj).year,
            last_event_date=last_event_date,
            reference_date=datetime.now(timezone_obj).date(),
        )
        print(
            f"DATE HISTORY FALLBACK: starting the next inferred "
            f"schedule block after {last_event_date}: "
            f"{start_date} to {end_date} ({day_count} days).",
            flush=True,
        )

    # =========================================================================
    # THE REST OF YOUR CORE CODE LOGIC REMAINS COMPLETELY UNTOUCHED BELOW HERE
    # =========================================================================
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
                    f"Dear member, Assalam-u-Alaikum, it's time to Prepare for {prayer} Prayer.\n\n"
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

    # ONLY update Supabase if it's a real individual user, not our Master profile
    if not member.get("is_master_calendar"):
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

    if start_date is None or end_date is None:
        print(
            "Timetable dates will be inferred separately from each "
            "target calendar's prayer-event history.",
            flush=True,
        )
    else:
        print(
            f"Timetable covers: {start_date} through {end_date}",
            flush=True,
        )

    if all_members and member_id is not None:
        raise ValueError(
            "Use either all_members or member_id, not both."
        )

    members = []
    include_master_calendar = False
    member_fetch_failed = False

    if all_members:
        try:
            members = fetch_connected_members()
        except Exception as exc:
            member_fetch_failed = True
            print(
                "SUPABASE MEMBER FETCH FAILED: "
                f"{type(exc).__name__}: {exc}. "
                "Continuing with the Master Calendar sync; "
                "individual member sync is unavailable for this run.",
                flush=True,
            )

        # Enable public master calendar sync during the weekly run
        include_master_calendar = True
        print(
            f"Weekly sync: connected members found: {len(members)}. Master Calendar sync enabled.",
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
    failed_members = int(member_fetch_failed)

    # =========================================================================
    # NEW STEP: SYNC THE CENTRAL MASTER CALENDAR FIRST
    # =========================================================================
    if include_master_calendar and config.get("public_calendar_id"):
        print("\n==============================================", flush=True)
        print("STARTING CENTRAL MASTER CALENDAR SYNCHRONIZATION", flush=True)
        print("==============================================", flush=True)
        
        master_member_mock = {
            "id": "MASTER_CALENDAR_SYSTEM_VIRTUAL_ID",
            "email": "Central Master Feed",
            "calendar_id": config["public_calendar_id"],
            "is_master_calendar": True
        }
        
        try:
            sync_member(
                master_member_mock,
                config,
                timezone_obj,
                start_date,
                day_count,
                prayer_times,
            )
            print("Central Master Calendar sync completed successfully.\n", flush=True)
        except Exception as exc:
            print(f"CRITICAL WARNING: Central Master Calendar sync failed: {exc}", flush=True)
            failed_members += 1

    # =========================================================================
    # EXISTING CODE: LOOP THROUGH INDIVIDUAL MEMBERS (UNTOUCHED LOGIC)
    # =========================================================================
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
                isinstance(arg, dict) and arg.get("error") == "invalid_grant"
                for arg in exc.args
            )
            if invalid_grant:
                try:
                    mark_member_reconnect_required(member["id"])
                except Exception as update_error:
                    print(
                        "Could not save reconnect-required status "
                        f"for {member.get('email')}: {update_error}",
                        flush=True,
                    )
                print(
                    f"RECONNECT REQUIRED: {member.get('email')} — "
                    "Google refresh token is no longer valid.",
                    flush=True,
                )
            else:
                print(
                    f"Token refresh failed for {member.get('email')}: "
                    "not a confirmed invalid_grant context.",
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
            f"Synchronization run completed with {failed_members} failure(s)."
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
