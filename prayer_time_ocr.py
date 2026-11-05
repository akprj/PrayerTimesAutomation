import re
from datetime import time

import pytesseract
from PIL import Image, ImageOps


_TIME_PATTERN = re.compile(
    r"(?<!\d)(\d{1,2})\s*[:.,]\s*(\d{2})(?!\d)"
)
_MIDDLE_COLUMN_BOUNDS = (0.32, 0.70)
_VERTICAL_BOUNDS = (0.14, 0.80)
_MERGED_FALLBACK_BANDS = (
    (0.275, 0.455),
    (0.480, 0.560),
)


def _time_candidates_from_data(data, minimum_confidence=30):
    words = []

    for index, raw_text in enumerate(data["text"]):
        text = raw_text.strip()
        if not text:
            continue

        try:
            confidence = float(data["conf"][index])
        except (TypeError, ValueError):
            continue
        if confidence < minimum_confidence:
            continue

        left = int(data["left"][index])
        top = int(data["top"][index])
        width = int(data["width"][index])
        height = int(data["height"][index])
        line_numbers = data.get("line_num")

        words.append(
            {
                "text": text,
                "left": left,
                "right": left + width,
                "top": top,
                "center_y": top + height / 2,
                "height": height,
                "line": (
                    line_numbers[index]
                    if line_numbers is not None
                    else None
                ),
            }
        )

    words.sort(key=lambda word: (word["center_y"], word["left"]))
    candidates = []

    for start in range(len(words)):
        for length in (1, 2, 3):
            parts = words[start:start + length]
            if len(parts) != length:
                continue

            if any(
                first["line"] is not None
                and second["line"] is not None
                and first["line"] != second["line"]
                for first, second in zip(parts, parts[1:])
            ):
                continue

            if any(
                abs(second["center_y"] - first["center_y"])
                > 0.55 * max(first["height"], second["height"])
                for first, second in zip(parts, parts[1:])
            ):
                continue

            if any(
                second["left"] - first["right"]
                > 1.5 * max(first["height"], second["height"])
                for first, second in zip(parts, parts[1:])
            ):
                continue

            match = _TIME_PATTERN.fullmatch(
                "".join(part["text"] for part in parts)
            )
            if match is None:
                continue

            try:
                candidate = time(
                    int(match.group(1)),
                    int(match.group(2)),
                )
            except ValueError:
                continue

            candidates.append(
                (
                    sum(part["center_y"] for part in parts) / length,
                    candidate,
                )
            )

    return candidates


def _times_for_scan(
    data,
    scale,
    vertical_offset,
    minimum_confidence=30,
):
    observations = [
        (vertical_offset + center_y / scale, candidate)
        for center_y, candidate in _time_candidates_from_data(
            data,
            minimum_confidence,
        )
    ]
    observations.sort(key=lambda item: item[0])

    grouped = []
    tolerance = max(3, scale * 0.012)
    for center_y, candidate in observations:
        if grouped and center_y - grouped[-1][0] <= tolerance:
            grouped[-1][1].add(candidate)
        else:
            grouped.append((center_y, {candidate}))

    return [
        (center_y, next(iter(candidates)) if len(candidates) == 1 else None)
        for center_y, candidates in grouped
    ]


def extract_vertical_middle_column_observations(image):
    """Read times and their normalized vertical positions from the time column."""
    left = int(image.width * _MIDDLE_COLUMN_BOUNDS[0])
    right = int(image.width * _MIDDLE_COLUMN_BOUNDS[1])
    top = int(image.height * _VERTICAL_BOUNDS[0])
    bottom = int(image.height * _VERTICAL_BOUNDS[1])
    source_scale = 4

    with image.crop((left, top, right, bottom)) as column:
        grayscale = ImageOps.grayscale(column)
        try:
            enlarged = grayscale.resize(
                (grayscale.width * source_scale, grayscale.height * source_scale),
                Image.Resampling.LANCZOS,
            )
        finally:
            grayscale.close()

    try:
        contrasted = ImageOps.autocontrast(enlarged)
    finally:
        enlarged.close()

    variants = [contrasted]
    variants.extend(
        contrasted.point(
            lambda pixel, cutoff=threshold: (
                0 if pixel < cutoff else 255
            )
        )
        for threshold in (145, 180)
    )

    scans = []
    try:
        for variant in variants:
            data = pytesseract.image_to_data(
                variant,
                lang="eng",
                config=(
                    "--oem 3 --psm 6 "
                    "-c tessedit_char_whitelist=0123456789:.,"
                ),
                output_type=pytesseract.Output.DICT,
            )
            scans.append(
                _times_for_scan(
                    data,
                    source_scale,
                    top,
                )
            )
    finally:
        for variant in variants:
            variant.close()

    for band_top, band_bottom in _MERGED_FALLBACK_BANDS:
        band_y = int(image.height * band_top)
        band_bottom_y = int(image.height * band_bottom)
        with image.crop((left, band_y, right, band_bottom_y)) as band:
            grayscale = ImageOps.grayscale(band)
            try:
                enlarged = grayscale.resize(
                    (
                        grayscale.width * source_scale,
                        grayscale.height * source_scale,
                    ),
                    Image.Resampling.LANCZOS,
                )
            finally:
                grayscale.close()

        try:
            contrasted = ImageOps.autocontrast(enlarged)
        finally:
            enlarged.close()

        band_variants = [contrasted]
        band_variants.extend(
            contrasted.point(
                lambda pixel, cutoff=threshold: (
                    0 if pixel < cutoff else 255
                )
            )
            for threshold in (145, 180)
        )
        try:
            for variant in band_variants:
                data = pytesseract.image_to_data(
                    variant,
                    lang="eng",
                    config=(
                        "--oem 3 --psm 7 "
                        "-c tessedit_char_whitelist=0123456789:.,"
                    ),
                    output_type=pytesseract.Output.DICT,
                )
                scans.append(
                    _times_for_scan(
                        data,
                        source_scale,
                        band_y,
                        minimum_confidence=0,
                    )
                )
        finally:
            for variant in band_variants:
                variant.close()

    observations = sorted(
        (
            (center_y, scan_index, candidate)
            for scan_index, scan in enumerate(scans)
            for center_y, candidate in scan
        ),
        key=lambda item: item[0],
    )
    clusters = []
    position_tolerance = max(3, image.height * 0.025)

    for center_y, scan_index, candidate in observations:
        if (
            clusters
            and center_y - clusters[-1]["center_y"] <= position_tolerance
        ):
            cluster = clusters[-1]
            cluster["positions"].append(center_y)
            if candidate is not None:
                cluster["votes"].setdefault(scan_index, set()).add(candidate)
        else:
            cluster = {
                "center_y": center_y,
                "positions": [center_y],
                "votes": {},
            }
            if candidate is not None:
                cluster["votes"][scan_index] = {candidate}
            clusters.append(cluster)

        clusters[-1]["center_y"] = sum(
            clusters[-1]["positions"]
        ) / len(clusters[-1]["positions"])

    ordered_observations = []
    for cluster in clusters:
        votes = {}
        for candidates in cluster["votes"].values():
            if len(candidates) != 1:
                continue
            candidate = next(iter(candidates))
            votes[candidate] = votes.get(candidate, 0) + 1

        if not votes:
            ordered_observations.append(
                (cluster["center_y"] / image.height, None)
            )
            continue

        highest_vote_count = max(votes.values())
        winners = [
            candidate
            for candidate, count in votes.items()
            if count == highest_vote_count
        ]
        ordered_observations.append(
            (
                cluster["center_y"] / image.height,
                winners[0]
                if highest_vote_count >= 2 and len(winners) == 1
                else None,
            )
        )

    return ordered_observations


def extract_vertical_middle_column_times(image):
    """Read distinct, vertically ordered times from the timetable's time column."""
    return [
        value
        for _, value in extract_vertical_middle_column_observations(image)
    ]


def fill_missing_merged_prayer_times(
    prayer_times,
    column_observations,
    row_bounds,
):
    """Fill merged prayer pairs from OCR times positioned in their row."""
    filled = []

    for first, second, band_start, band_end in (
        (
            "Zohar",
            "Assr",
            row_bounds["Zohar"][0],
            row_bounds["Assr"][1],
        ),
        (
            "Maghrib",
            "Ishaa",
            row_bounds["Maghrib"][0],
            row_bounds["Maghrib"][1],
        ),
    ):
        row_times = {
            value
            for position, value in column_observations
            if band_start <= position <= band_end and value is not None
        }
        if len(row_times) != 1:
            continue
        column_time = next(iter(row_times))

        if first in prayer_times:
            continue

        prayer_times[first] = column_time
        filled.append(first)
        if prayer_times.get(second) != column_time:
            prayer_times[second] = column_time
            filled.append(second)

    return filled
