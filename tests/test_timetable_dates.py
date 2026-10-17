import unittest
from datetime import date

from timetable_dates import resolve_timetable_dates


class ResolveTimetableDatesTests(unittest.TestCase):
    def resolve(self, text, last_event_date=None):
        return resolve_timetable_dates(
            text,
            2026,
            last_event_date=last_event_date,
            reference_date=date(2026, 10, 17),
        )

    def test_keeps_explicit_eight_day_transition_range(self):
        result = self.resolve(
            "ab Samstag, dem 17. Okt bis zum Samstag, dem 24. Okt"
        )

        self.assertEqual(
            result,
            (date(2026, 10, 17), date(2026, 10, 24), 8),
        )

    def test_rejects_explicit_ranges_longer_than_eight_days(self):
        with self.assertRaisesRegex(RuntimeError, "valid range"):
            self.resolve(
                "ab Samstag, dem 17. Okt "
                "bis zum Sonntag, dem 25. Okt 2026"
            )

    def test_keeps_ocr_weekdays_ahead_of_summer_defaults(self):
        result = self.resolve(
            "ab Sonntag, dem 1. Nov bis zum Freitag, dem 6. Nov"
        )

        self.assertEqual(
            result,
            (date(2026, 11, 1), date(2026, 11, 6), 6),
        )

    def test_ignores_unmarked_number_and_word_in_whole_image_ocr(self):
        result = self.resolve(
            "5 Fajr ab Samstag, dem 17. Okt "
            "bis zum Freitag, dem 23. Okt"
        )

        self.assertEqual(
            result,
            (date(2026, 10, 17), date(2026, 10, 23), 7),
        )

    def test_infers_missing_start_date_from_start_weekday(self):
        result = self.resolve(
            "ab Samstag, dem bis zum Samstag, dem 24. Okt"
        )

        self.assertEqual(
            result,
            (date(2026, 10, 17), date(2026, 10, 24), 8),
        )

    def test_infers_missing_end_date_from_end_weekday(self):
        result = self.resolve(
            "ab Samstag, dem 31. Okt bis zum Freitag, dem"
        )

        self.assertEqual(
            result,
            (date(2026, 10, 31), date(2026, 11, 6), 7),
        )

    def test_uses_saturday_start_after_fall_transition_week(self):
        result = self.resolve("bis zum Freitag, dem 6. Nov")

        self.assertEqual(
            result,
            (date(2026, 10, 31), date(2026, 11, 6), 7),
        )

    def test_infers_eight_day_fall_week_from_end_date(self):
        result = self.resolve("bis zum 24. Okt")

        self.assertEqual(
            result,
            (date(2026, 10, 17), date(2026, 10, 24), 8),
        )

    def test_infers_six_day_week_after_fall_transition(self):
        result = self.resolve("bis zum Freitag, dem 30. Okt")

        self.assertEqual(
            result,
            (date(2026, 10, 25), date(2026, 10, 30), 6),
        )

    def test_infers_eight_and_six_day_weeks_after_spring_transition(self):
        long_week = self.resolve("bis zum Samstag, dem 28. Mär")
        short_week = self.resolve("bis zum Freitag, dem 3. Apr")
        regular_week = self.resolve("bis zum Freitag, dem 10. Apr")

        self.assertEqual(
            long_week,
            (date(2026, 3, 21), date(2026, 3, 28), 8),
        )
        self.assertEqual(
            short_week,
            (date(2026, 3, 29), date(2026, 4, 3), 6),
        )
        self.assertEqual(
            regular_week,
            (date(2026, 4, 4), date(2026, 4, 10), 7),
        )

    def test_infers_dst_adjusted_end_dates_from_start_date(self):
        long_week = self.resolve(
            "ab Samstag, dem 21. Mär bis zum Samstag, dem"
        )
        short_week = self.resolve(
            "ab Sonntag, dem 29. Mär bis zum Freitag, dem"
        )
        fall_short_week = self.resolve(
            "ab Sonntag, dem 25. Okt bis zum Freitag, dem"
        )

        self.assertEqual(
            long_week,
            (date(2026, 3, 21), date(2026, 3, 28), 8),
        )
        self.assertEqual(
            short_week,
            (date(2026, 3, 29), date(2026, 4, 3), 6),
        )
        self.assertEqual(
            fall_short_week,
            (date(2026, 10, 25), date(2026, 10, 30), 6),
        )

    def test_rejects_end_date_outside_a_valid_inferred_week(self):
        with self.assertRaisesRegex(RuntimeError, "do not identify"):
            self.resolve("bis zum 7. Nov 2026")

    def test_uses_next_day_after_last_cron_event_for_missing_start(self):
        result = self.resolve(
            "bis zum Freitag, dem 30. Okt",
            last_event_date=date(2026, 10, 23),
        )

        self.assertEqual(
            result,
            (date(2026, 10, 25), date(2026, 10, 30), 6),
        )

    def test_history_only_fallback_uses_next_day_and_week_pattern(self):
        regular_week = self.resolve(
            "",
            last_event_date=date(2026, 10, 9),
        )
        eight_day_week = self.resolve(
            "",
            last_event_date=date(2026, 10, 16),
        )
        six_day_week = self.resolve(
            "",
            last_event_date=date(2026, 10, 24),
        )

        self.assertEqual(
            regular_week,
            (date(2026, 10, 10), date(2026, 10, 16), 7),
        )
        self.assertEqual(
            eight_day_week,
            (date(2026, 10, 17), date(2026, 10, 24), 8),
        )
        self.assertEqual(
            six_day_week,
            (date(2026, 10, 25), date(2026, 10, 30), 6),
        )

    def test_uses_season_default_when_history_is_outside_the_range(self):
        result = self.resolve(
            "bis zum Freitag, dem 16. Okt",
            last_event_date=date(2026, 10, 25),
        )

        self.assertEqual(
            result,
            (date(2026, 10, 10), date(2026, 10, 16), 7),
        )

    def test_rejects_date_and_weekday_disagreement(self):
        with self.assertRaisesRegex(RuntimeError, "do not identify"):
            self.resolve(
                "ab Sonntag, dem 17. Okt bis zum Samstag, dem 24. Okt"
            )

    def test_rejects_missing_dates_and_history(self):
        with self.assertRaisesRegex(RuntimeError, "Could not identify"):
            self.resolve("ab Samstag, dem bis zum Freitag, dem")


if __name__ == "__main__":
    unittest.main()
