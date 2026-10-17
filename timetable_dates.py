import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta


MONTHS = {
    "jan": 1,
    "feb": 2,
    "mär": 3,
    "mar": 3,
    "mae": 3,
    "maerz": 3,
    "mrz": 3,
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

WEEKDAYS = {
    "montag": 0,
    "dienstag": 1,
    "mittwoch": 2,
    "donnerstag": 3,
    "freitag": 4,
    "samstag": 5,
    "sonntag": 6,
}

WEEKDAY_PATTERN = (
    r"Montag|Dienstag|Mittwoch|Donnerstag|"
    r"Freitag|Samstag|Sonntag"
)
DATE_PATTERN = (
    r"(?P<day>\d{1,2})\s*[.,]?\s*"
    r"(?P<month>[A-Za-zÄÖÜäöüß]+)\b"
    r"(?:\s*[.,]?\s*(?P<year>\d{4})\b)?"
)


@dataclass(frozen=True)
class TimetableEndpoint:
    role: str
    weekday: int | None
    day: int | None
    month: int | None
    year: int | None
    text: str


def _role_for(marker: str, weekday: int | None, text: str) -> str:
    if marker == "ab":
        return "start"
    if marker in {"bis zum", "zum"}:
        return "end"
    if weekday in {5, 6}:
        return "start"
    if weekday == 4:
        return "end"
    raise RuntimeError(
        f"Cannot identify a timetable endpoint: {text!r}"
    )


def parse_timetable_endpoints(text: str) -> list[TimetableEndpoint]:
    date_regex = re.compile(
        rf"\b(?:(?P<marker>ab|bis\s+zum|zum)\s+)?"
        rf"(?:(?P<weekday>{WEEKDAY_PATTERN})\s*,?\s*)?"
        rf"(?:dem\s+)?{DATE_PATTERN}",
        flags=re.IGNORECASE,
    )
    endpoints = []
    date_spans = []

    for match in date_regex.finditer(text):
        marker = " ".join(
            (match.group("marker") or "").lower().split()
        )
        weekday_text = match.group("weekday")
        if not marker and not weekday_text:
            continue

        weekday = (
            WEEKDAYS[weekday_text.lower()]
            if weekday_text
            else None
        )
        month_text = match.group("month")
        month_name = month_text.lower()
        month = MONTHS.get(month_name) or MONTHS.get(month_name[:3])
        if month is None:
            raise RuntimeError(
                f"Unrecognised month in date OCR: {month_text!r}"
            )

        endpoints.append(
            TimetableEndpoint(
                role=_role_for(marker, weekday, match.group(0)),
                weekday=weekday,
                day=int(match.group("day")),
                month=month,
                year=(
                    int(match.group("year"))
                    if match.group("year")
                    else None
                ),
                text=match.group(0),
            )
        )
        date_spans.append(match.span())

    weekday_regex = re.compile(
        rf"\b(?P<marker>ab|bis\s+zum|zum)\s+"
        rf"(?P<weekday>{WEEKDAY_PATTERN})\b",
        flags=re.IGNORECASE,
    )

    for match in weekday_regex.finditer(text):
        if any(
            match.start() < end and match.end() > start
            for start, end in date_spans
        ):
            continue

        marker = " ".join(match.group("marker").lower().split())
        weekday_text = match.group("weekday")
        weekday = WEEKDAYS[weekday_text.lower()]
        endpoints.append(
            TimetableEndpoint(
                role=_role_for(marker, weekday, match.group(0)),
                weekday=weekday,
                day=None,
                month=None,
                year=None,
                text=match.group(0),
            )
        )

    return endpoints


def _summer_start_weekday(reference: date) -> int:
    march_last_day = calendar.monthrange(reference.year, 3)[1]
    october_last_day = calendar.monthrange(reference.year, 10)[1]
    summer_starts = date(reference.year, 3, march_last_day)
    summer_starts -= timedelta(days=(summer_starts.weekday() - 6) % 7)
    summer_ends = date(reference.year, 10, october_last_day)
    summer_ends -= timedelta(days=(summer_ends.weekday() - 6) % 7)
    return 5 if summer_starts <= reference < summer_ends else 6


def _weekday_only(
    endpoints: list[TimetableEndpoint],
    role: str,
) -> int | None:
    weekdays = {
        endpoint.weekday
        for endpoint in endpoints
        if endpoint.role == role and endpoint.weekday is not None
    }
    if len(weekdays) > 1:
        raise RuntimeError(
            f"Conflicting OCR weekdays for timetable {role}: "
            f"{sorted(weekdays)!r}"
        )
    return next(iter(weekdays), None)


def _endpoint_date_candidates(
    endpoints: list[TimetableEndpoint],
    current_year: int,
) -> dict[str, set[date]]:
    explicit_years = {
        endpoint.year
        for endpoint in endpoints
        if endpoint.year is not None
    }
    reference_years = explicit_years or {current_year}
    candidate_years = {
        reference_year + offset
        for reference_year in reference_years
        for offset in (-1, 0, 1)
        if 1 <= reference_year + offset <= 9999
    }
    candidates: dict[str, set[date]] = {}
    for endpoint in endpoints:
        if endpoint.day is None or endpoint.month is None:
            continue

        years_to_try = (
            [endpoint.year]
            if endpoint.year is not None
            else sorted(candidate_years)
        )
        record_dates = set()

        for candidate_year in years_to_try:
            try:
                parsed_date = date(
                    candidate_year,
                    endpoint.month,
                    endpoint.day,
                )
            except ValueError:
                continue

            if (
                endpoint.weekday is not None
                and parsed_date.weekday() != endpoint.weekday
            ):
                continue

            record_dates.add(parsed_date)

        if not record_dates:
            raise RuntimeError(
                f"Cannot resolve timetable date {endpoint.text!r} "
                f"for years {years_to_try!r}. Check the OCR date, "
                "weekday, and printed year."
            )

        if endpoint.role in candidates:
            candidates[endpoint.role].intersection_update(record_dates)
        else:
            candidates[endpoint.role] = record_dates

        if not candidates[endpoint.role]:
            raise RuntimeError(
                f"Conflicting OCR dates for timetable {endpoint.role}."
            )

    return candidates


def resolve_timetable_dates(
    text: str,
    current_year: int,
    last_event_date: date | None = None,
    reference_date: date | None = None,
) -> tuple[date, date, int]:
    reference_date = reference_date or date.today()
    endpoints = parse_timetable_endpoints(text)
    start_weekday = _weekday_only(endpoints, "start")
    end_weekday = _weekday_only(endpoints, "end")
    date_candidates = _endpoint_date_candidates(endpoints, current_year)
    starts = date_candidates.get("start")
    ends = date_candidates.get("end")

    if not starts and not ends and last_event_date is None:
        raise RuntimeError(
            "Could not identify a timetable date or a prior timetable "
            "event. Cannot safely infer the timetable week."
        )

    possible_ranges = set()

    if starts and ends:
        for start in starts:
            for end in ends:
                if 0 <= (end - start).days <= 7:
                    possible_ranges.add((start, end))
    elif ends:
        for end in ends:
            if start_weekday is not None:
                inferred_start = end - timedelta(
                    days=(end.weekday() - start_weekday) % 7 or 7
                )
            elif last_event_date is not None:
                inferred_start = last_event_date + timedelta(days=1)
                if not 0 <= (end - inferred_start).days <= 7:
                    inferred_weekday = _summer_start_weekday(end)
                    inferred_start = end - timedelta(
                        days=(end.weekday() - inferred_weekday) % 7 or 7
                    )
            else:
                inferred_weekday = _summer_start_weekday(end)
                inferred_start = end - timedelta(
                    days=(end.weekday() - inferred_weekday) % 7 or 7
                )
            possible_ranges.add((inferred_start, end))
    else:
        for start in starts or {last_event_date + timedelta(days=1)}:
            inferred_weekday = (
                end_weekday if end_weekday is not None else 4
            )
            inferred_end = start + timedelta(
                days=(inferred_weekday - start.weekday()) % 7 or 7
            )
            possible_ranges.add((start, inferred_end))

    valid_ranges = set()
    for start, end in possible_ranges:
        day_count = (end - start).days + 1
        if not 1 <= day_count <= 8:
            continue
        if start_weekday is not None and start.weekday() != start_weekday:
            continue
        if end_weekday is not None and end.weekday() != end_weekday:
            continue
        valid_ranges.add((start, end))

    if len(valid_ranges) > 1:
        def distance_from_reference(date_range):
            start, end = date_range
            if start <= reference_date <= end:
                return 0
            if reference_date < start:
                return (start - reference_date).days
            return (reference_date - end).days

        nearest_distance = min(
            distance_from_reference(date_range)
            for date_range in valid_ranges
        )
        valid_ranges = {
            date_range
            for date_range in valid_ranges
            if distance_from_reference(date_range) == nearest_distance
        }

    if len(valid_ranges) != 1:
        if not valid_ranges:
            raise RuntimeError(
                "OCR dates, weekdays, and available history do not "
                "identify a valid range of up to eight days."
            )
        raise RuntimeError(
            "The timetable year/week is ambiguous. Cannot safely "
            "select one date range; check the OCR dates and printed year."
        )

    start_date, end_date = next(iter(valid_ranges))
    return start_date, end_date, (end_date - start_date).days + 1
