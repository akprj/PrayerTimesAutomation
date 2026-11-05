import unittest

from prayer_schedule import build_daily_prayer_entries


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


if __name__ == "__main__":
    unittest.main()
