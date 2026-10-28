"""Slot picker (CLP-92): windows, spacing, posts per day, platform caps, warm-up ramp."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from clipper.posting.scheduler import (
    MAX_DAYS_AHEAD,
    PLATFORM_DAILY_CAPS,
    daily_limit,
    parse_windows,
    pick_slot,
    warmup_allowance,
)
from posting_support import FakeAccount

MANILA = ZoneInfo("Asia/Manila")  # UTC+8, no DST
NOW = datetime(2027, 1, 15, 0, 30, tzinfo=UTC)  # 08:30 in Manila
TODAY = date(2027, 1, 15)


def local(day: int, hour: int, minute: int = 0) -> datetime:
    """A Manila wall-clock time in January 2027, as an aware UTC datetime."""
    return datetime(2027, 1, day, hour, minute, tzinfo=MANILA).astimezone(UTC)


# --- windows ----------------------------------------------------------------------------


def test_first_slot_is_the_start_of_todays_first_window() -> None:
    assert pick_slot(FakeAccount(), [], NOW) == local(15, 9)


def test_slot_inside_an_open_window_starts_now_rounded_up_to_the_minute() -> None:
    now = local(15, 10, 15) + timedelta(seconds=20)

    assert pick_slot(FakeAccount(), [], now) == local(15, 10, 16)


def test_slot_is_returned_in_utc() -> None:
    slot = pick_slot(FakeAccount(), [], NOW)

    assert slot is not None
    assert slot.tzinfo is not None and slot.utcoffset() == timedelta(0)


def test_min_gap_pushes_the_next_slot_later_in_the_window() -> None:
    assert pick_slot(FakeAccount(), [local(15, 9)], NOW) == local(15, 11)


def test_a_slot_that_would_pass_the_window_end_moves_to_the_next_window() -> None:
    assert pick_slot(FakeAccount(), [local(15, 9), local(15, 11)], NOW) == local(15, 18)


def test_gap_is_kept_against_posts_before_the_candidate_too() -> None:
    account = FakeAccount(windows=["09:00-23:00"], min_gap_minutes=60)

    assert pick_slot(account, [local(15, 9, 30)], NOW) == local(15, 10, 30)


def test_the_window_end_minute_is_inside_the_window() -> None:
    # CLP-91's rule: "09:00-11:00" accepts an 11:00 slot.
    account = FakeAccount(windows=["09:00-11:00", "18:00-22:00"])

    assert pick_slot(account, [local(15, 9)], NOW) == local(15, 11)


def test_the_gap_does_not_carry_over_midnight() -> None:
    # CLP-91's rule: the minimum gap applies within one local day only.
    account = FakeAccount(windows=["00:00-23:59"], min_gap_minutes=120)
    now = local(15, 23, 50)

    assert pick_slot(account, [local(15, 23, 50)], now) == local(16, 0)


def test_after_todays_windows_close_the_slot_is_tomorrow() -> None:
    now = local(15, 22, 5)

    assert pick_slot(FakeAccount(), [], now) == local(16, 9)


def test_window_ending_at_midnight_is_open_until_midnight() -> None:
    account = FakeAccount(windows=["22:00-00:00"])
    now = local(15, 23, 30)

    assert pick_slot(account, [], now) == local(15, 23, 30)


def test_windows_are_used_in_time_order_whatever_the_config_order() -> None:
    account = FakeAccount(windows=["18:00-22:00", "09:00-12:00"])

    assert pick_slot(account, [], NOW) == local(15, 9)


@pytest.mark.parametrize("bad", ["9-12", "09:00", "12:00-09:00", "25:00-26:00", "09:00-09:00"])
def test_invalid_windows_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_windows([bad])


def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError):
        pick_slot(FakeAccount(), [], datetime(2027, 1, 15, 8, 0))


# --- posts per day and platform caps -----------------------------------------------------


def test_posts_per_day_full_moves_to_the_next_day() -> None:
    account = FakeAccount(posts_per_day=2)

    assert pick_slot(account, [local(15, 9), local(15, 18)], NOW) == local(16, 9)


def test_days_are_counted_in_the_account_timezone() -> None:
    account = FakeAccount(posts_per_day=1, windows=["06:00-12:00"])
    # 2027-01-14 23:30 UTC is 2027-01-15 07:30 in Manila: today is already full there.
    taken = [datetime(2027, 1, 14, 23, 30, tzinfo=UTC)]

    assert pick_slot(account, taken, NOW) == local(16, 6)


def test_platform_cap_applies_when_posts_per_day_is_higher() -> None:
    account = FakeAccount(posts_per_day=40, min_gap_minutes=1, windows=["00:00-23:59"])
    cap = PLATFORM_DAILY_CAPS["tiktok"]
    taken = [local(15, 9) + timedelta(minutes=n) for n in range(cap)]

    assert cap == 15
    assert pick_slot(account, taken, NOW) == local(16, 0)


def test_platform_caps_match_the_story() -> None:
    assert PLATFORM_DAILY_CAPS == {"tiktok": 15, "facebook": 30, "instagram": 50, "youtube": 100}


def test_daily_limit_is_the_smallest_of_posts_per_day_warmup_and_cap() -> None:
    assert daily_limit(FakeAccount(posts_per_day=40), TODAY) == 15
    assert daily_limit(FakeAccount(platform="instagram", posts_per_day=40), TODAY) == 40
    ramp = FakeAccount(posts_per_day=40, warmup_start=TODAY, warmup_days=14)
    assert daily_limit(ramp, TODAY) == 3  # ceil(40 * 1 / 14)


def test_many_slots_never_exceed_the_daily_limit() -> None:
    account = FakeAccount(posts_per_day=20, min_gap_minutes=5, windows=["00:00-23:59"])
    taken: list[datetime] = []
    for _ in range(60):
        slot = pick_slot(account, taken, NOW)
        assert slot is not None
        taken.append(slot)

    per_day = Counter(t.astimezone(MANILA).date() for t in taken)
    assert max(per_day.values()) <= PLATFORM_DAILY_CAPS["tiktok"]
    gaps = [b - a for a, b in zip(sorted(taken), sorted(taken)[1:], strict=False)]
    assert min(gaps) >= timedelta(minutes=5)


def test_nothing_free_within_seven_days_returns_none() -> None:
    account = FakeAccount(posts_per_day=1, windows=["09:00-10:00"])
    taken = [local(15 + n, 9) for n in range(MAX_DAYS_AHEAD + 1)]

    assert MAX_DAYS_AHEAD == 7
    assert pick_slot(account, taken, NOW) is None


def test_zero_posts_per_day_never_schedules() -> None:
    assert pick_slot(FakeAccount(posts_per_day=0), [], NOW) is None


# --- warm-up ramp ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day_offset", "expected"),
    [(-1, 0), (0, 1), (6, 2), (13, 3), (14, 3), (30, 3)],
)
def test_warmup_ramp_for_three_posts_a_day(day_offset: int, expected: int) -> None:
    account = FakeAccount(posts_per_day=3, warmup_start=TODAY, warmup_days=14)

    assert warmup_allowance(account, TODAY + timedelta(days=day_offset)) == expected


def test_warmup_ramp_formula_for_fourteen_posts_a_day() -> None:
    account = FakeAccount(posts_per_day=14, warmup_start=TODAY, warmup_days=14)

    allowed = [warmup_allowance(account, TODAY + timedelta(days=d)) for d in range(16)]

    assert allowed == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 14, 14]


def test_no_warmup_start_means_no_ramp() -> None:
    assert warmup_allowance(FakeAccount(posts_per_day=5), TODAY) == 5


def test_warmup_first_day_allows_one_post() -> None:
    account = FakeAccount(posts_per_day=3, warmup_start=TODAY, warmup_days=14)

    assert pick_slot(account, [local(15, 9)], NOW) == local(16, 9)


def test_nothing_is_scheduled_before_warmup_start() -> None:
    account = FakeAccount(warmup_start=TODAY + timedelta(days=3))

    assert pick_slot(account, [], NOW) == local(18, 9)


def test_warmup_starting_after_the_horizon_returns_none() -> None:
    account = FakeAccount(warmup_start=TODAY + timedelta(days=MAX_DAYS_AHEAD + 1))

    assert pick_slot(account, [], NOW) is None
