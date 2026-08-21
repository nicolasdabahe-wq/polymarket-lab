from datetime import datetime, timezone

from pmbot.data.gamma import categorize_tags
from pmbot.scheduler.daily import next_daily_run


def tag(label: str) -> dict:
    return {"label": label}


def test_specific_tag_wins_over_general():
    # Los eventos de la Fed traen tags específicos primero y Politics después.
    tags = [tag("fomc"), tag("Economic Policy"), tag("Politics")]
    assert categorize_tags(tags) == "economy"


def test_politics_tag():
    assert categorize_tags([tag("Politics")]) == "politics"


def test_unknown_tags_are_other():
    assert categorize_tags([tag("Something Odd")]) == "other"
    assert categorize_tags([]) == "other"
    assert categorize_tags(None) == "other"


def test_next_daily_run_future_today():
    now = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)
    run = next_daily_run(now, "11:00")
    assert run == datetime(2026, 8, 21, 11, 0, tzinfo=timezone.utc)


def test_next_daily_run_rolls_to_tomorrow():
    now = datetime(2026, 8, 21, 12, 30, tzinfo=timezone.utc)
    run = next_daily_run(now, "11:00")
    assert run == datetime(2026, 8, 22, 11, 0, tzinfo=timezone.utc)


def test_next_daily_run_exact_time_goes_tomorrow():
    now = datetime(2026, 8, 21, 11, 0, tzinfo=timezone.utc)
    assert next_daily_run(now, "11:00").day == 22
