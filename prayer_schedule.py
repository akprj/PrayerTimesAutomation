from datetime import timedelta


COMBINED_PRAYER_LABELS = {
    ("Zohar", "Assr"): "Zohar Assr Jama (combined)",
    ("Maghrib", "Ishaa"): "Maghrib Isha Jama (combined)",
}


def format_prayer_entry_time(event_label, starts_at):
    start_time = starts_at.strftime("%H:%M")
    if event_label not in COMBINED_PRAYER_LABELS.values():
        return start_time

    end_time = (starts_at + timedelta(minutes=10)).strftime("%H:%M")
    return f"{start_time} to {end_time}"


def build_daily_prayer_entries(prayers, combined_prayer_pairs):
    """Return event label, time key, and reminder key for each daily entry."""
    entries = []
    skipped_prayers = set()

    for prayer in prayers:
        if prayer in skipped_prayers:
            continue

        combined_pair = next(
            (
                pair
                for pair in combined_prayer_pairs
                if pair[0] == prayer
                and pair[1] in prayers
                and pair in COMBINED_PRAYER_LABELS
            ),
            None,
        )
        if combined_pair is None:
            entries.append((prayer, prayer, prayer))
            continue

        first, second = combined_pair
        entries.append(
            (COMBINED_PRAYER_LABELS[combined_pair], first, first)
        )
        skipped_prayers.add(second)

    return entries
