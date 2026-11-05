import unittest
from datetime import datetime

from prayer_schedule import (
    build_daily_prayer_entries,
    format_prayer_entry_time,
    prayer_event_duration_minutes,
)


class DailyPrayerEntriesTests(unittest.TestCase):
    def test_creates_one_entry_for_each_recovered_merged_pair(self):
        prayers = ["Fajr", "Zohar", "Assr", "Maghrib", "Ishaa"]

        entries = build_daily_prayer_entries(
            prayers,
            {("Zohar", "Assr"), ("Maghrib", "Ishaa")},
        )

        self.assertEqual(
            entries,
            [
                ("Fajr", "Fajr", "Fajr"),
                ("Zohar Assr Jama (combined)", "Zohar", "Zohar"),
                (
                    "Maghrib Isha Jama (combined)",
                    "Maghrib",
                    "Maghrib",
                ),
            ],
        )

    def test_leaves_nonmerged_prayers_as_individual_entries(self):
        prayers = ["Fajr", "Zohar", "Assr", "Maghrib", "Ishaa"]

        entries = build_daily_prayer_entries(prayers, set())

        self.assertEqual(
            entries,
            [(prayer, prayer, prayer) for prayer in prayers],
        )

    def test_preserves_friday_zohar_replacement_behavior(self):
        prayers = ["Fajr", "Juma", "Assr", "Maghrib", "Ishaa"]

        entries = build_daily_prayer_entries(
            prayers,
            {("Zohar", "Assr"), ("Maghrib", "Ishaa")},
        )

        self.assertEqual(
            entries,
            [
                ("Fajr", "Fajr", "Fajr"),
                ("Juma", "Juma", "Juma"),
                ("Assr", "Assr", "Assr"),
                (
                    "Maghrib Isha Jama (combined)",
                    "Maghrib",
                    "Maghrib",
                ),
            ],
        )


class PrayerEntryTimeFormatTests(unittest.TestCase):
    def test_combined_entry_description_shows_only_ocr_start_time(self):
        starts_at = datetime(2026, 11, 2, 13, 30)

        self.assertEqual(
            format_prayer_entry_time(
                starts_at,
            ),
            "13:30",
        )

    def test_individual_entry_shows_only_ocr_start_time(self):
        starts_at = datetime(2026, 11, 2, 6, 20)

        self.assertEqual(
            format_prayer_entry_time(starts_at),
            "06:20",
        )

    def test_combined_event_has_ten_minute_native_duration(self):
        self.assertEqual(
            prayer_event_duration_minutes(
                "Zohar Assr Jama (combined)"
            ),
            10,
        )

    def test_individual_event_keeps_five_minute_native_duration(self):
        self.assertEqual(prayer_event_duration_minutes("Fajr"), 5)


if __name__ == "__main__":
    unittest.main()
