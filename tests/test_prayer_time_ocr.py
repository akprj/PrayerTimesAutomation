import unittest
from datetime import time
from unittest.mock import patch

from PIL import Image

from prayer_time_ocr import (
    extract_vertical_middle_column_times,
    fill_missing_merged_prayer_times,
)


def ocr_data(times):
    data = {
        "text": [],
        "left": [],
        "top": [],
        "width": [],
        "height": [],
        "conf": [],
        "line_num": [],
    }

    for index, (text, top) in enumerate(times):
        data["text"].append(text)
        data["left"].append(20)
        data["top"].append(top)
        data["width"].append(46)
        data["height"].append(20)
        data["conf"].append("95")
        data["line_num"].append(index + 1)

    return data


class VerticalMiddleColumnOcrTests(unittest.TestCase):
    def test_reads_times_in_top_to_bottom_order_with_scan_consensus(self):
        scans = [
            ocr_data(
                [
                    ("06:20", 20),
                    ("13:30", 120),
                    ("17:20", 220),
                    ("19:00", 320),
                    ("13:15", 420),
                ]
            )
            for _ in range(3)
        ]

        with patch(
            "prayer_time_ocr.pytesseract.image_to_data",
            side_effect=scans,
        ):
            result = extract_vertical_middle_column_times(
                Image.new("RGB", (595, 813), "white")
            )

        self.assertEqual(
            result,
            [
                time(6, 20),
                time(13, 30),
                time(17, 20),
                time(19, 0),
                time(13, 15),
            ],
        )

    def test_marks_unconfirmed_column_time_inconclusive(self):
        scans = [
            ocr_data([("06:20", 20), ("13:30", 120)]),
            ocr_data([("06:20", 20), ("13:45", 120)]),
            ocr_data([("06:20", 20)]),
        ]

        with patch(
            "prayer_time_ocr.pytesseract.image_to_data",
            side_effect=scans,
        ):
            result = extract_vertical_middle_column_times(
                Image.new("RGB", (595, 813), "white")
            )

        self.assertEqual(result, [time(6, 20), None])

    def test_assigns_recovered_value_to_both_merged_pair_members(self):
        prayer_times = {"Fajr": time(6, 20), "Ishaa": time(19, 0)}

        filled = fill_missing_merged_prayer_times(
            prayer_times,
            [
                time(6, 20),
                time(13, 30),
                time(17, 20),
            ],
        )

        self.assertEqual(
            prayer_times,
            {
                "Fajr": time(6, 20),
                "Zohar": time(13, 30),
                "Assr": time(13, 30),
                "Maghrib": time(17, 20),
                "Ishaa": time(17, 20),
            },
        )
        self.assertEqual(
            filled,
            ["Zohar", "Assr", "Maghrib", "Ishaa"],
        )

    def test_assigns_merged_value_to_pair_when_primary_is_missing(self):
        prayer_times = {"Assr": time(13, 45)}

        filled = fill_missing_merged_prayer_times(
            prayer_times,
            [time(6, 20), time(13, 30)],
        )

        self.assertEqual(
            prayer_times,
            {
                "Assr": time(13, 30),
                "Zohar": time(13, 30),
            },
        )
        self.assertEqual(filled, ["Zohar", "Assr"])

    def test_does_not_change_resolved_primary_pair_member(self):
        prayer_times = {"Zohar": time(13, 25)}

        filled = fill_missing_merged_prayer_times(
            prayer_times,
            [time(6, 20), time(13, 30)],
        )

        self.assertEqual(
            prayer_times,
            {
                "Zohar": time(13, 25),
                "Assr": time(13, 30),
            },
        )
        self.assertEqual(filled, ["Assr"])

    def test_skips_inconclusive_slot_but_fills_confirmed_slot(self):
        prayer_times = {}

        filled = fill_missing_merged_prayer_times(
            prayer_times,
            [time(6, 20), None, time(17, 20)],
        )

        self.assertEqual(
            prayer_times,
            {
                "Maghrib": time(17, 20),
                "Ishaa": time(17, 20),
            },
        )
        self.assertEqual(filled, ["Maghrib", "Ishaa"])


if __name__ == "__main__":
    unittest.main()
