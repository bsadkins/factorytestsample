"""Trailing calendar-month case-creation windows for Support case retrieval.

A run collects cases created in `[runStart - lookbackMonths calendar months, runStart)`. The run
start comes from the run ID, so every retry and duplicate delivery of a run uses the same window.
Subtracting months keeps the day of month and clamps it to the target month's last day, for
example 31 March minus one month is 28 February (29 February in a leap year).
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from common.account_list import validate_run_id

MIN_LOOKBACK_MONTHS: Final = 1
MAX_LOOKBACK_MONTHS: Final = 12
_RUN_TIME_LENGTH: Final = 16
_RUN_TIME_FORMAT: Final = "%Y%m%dT%H%M%SZ"
_MONTHS_PER_YEAR: Final = 12


def run_started_at(run_id: str) -> datetime:
    """The UTC start time encoded in a run ID (`YYYYMMDDTHHMMSSZ[-token]`)."""
    validate_run_id(run_id)
    try:
        parsed = datetime.strptime(run_id[:_RUN_TIME_LENGTH], _RUN_TIME_FORMAT)
    except ValueError:
        raise ValueError("invalid run ID") from None
    return parsed.replace(tzinfo=UTC)


def subtract_calendar_months(moment: datetime, months: int) -> datetime:
    """Return `moment` minus `months` calendar months, clamping the day to the month's end."""
    if months < 0:
        raise ValueError("months must not be negative")
    index = moment.year * _MONTHS_PER_YEAR + (moment.month - 1) - months
    year, month0 = divmod(index, _MONTHS_PER_YEAR)
    month = month0 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def format_support_time(moment: datetime) -> str:
    """ISO 8601 UTC timestamp accepted by the Support API, e.g. `2025-11-13T00:00:00Z`."""
    if moment.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_support_time(value: object) -> datetime | None:
    """Parse a Support API timestamp such as `2026-01-15T10:20:30.123Z`; `None` if invalid."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class CreationWindow:
    """Half-open creation-time window `[after, before)`."""

    after: datetime
    before: datetime

    def contains(self, moment: datetime) -> bool:
        """True when `moment` is at or after `after` and strictly before `before`."""
        return self.after <= moment < self.before

    def request_parameters(self) -> dict[str, str]:
        """`afterTime`/`beforeTime` parameters for `support:DescribeCases`."""
        return {
            "afterTime": format_support_time(self.after),
            "beforeTime": format_support_time(self.before),
        }


def validate_lookback_months(months: int) -> int:
    """Return `months` if it is an allowed lookback, otherwise raise `ValueError`."""
    if isinstance(months, bool) or not MIN_LOOKBACK_MONTHS <= months <= MAX_LOOKBACK_MONTHS:
        raise ValueError(
            f"lookback months must be between {MIN_LOOKBACK_MONTHS} and {MAX_LOOKBACK_MONTHS}"
        )
    return months


def creation_window(run_id: str, lookback_months: int) -> CreationWindow:
    """The case-creation window for `run_id` with a trailing `lookback_months` window."""
    before = run_started_at(run_id)
    after = subtract_calendar_months(before, validate_lookback_months(lookback_months))
    return CreationWindow(after=after, before=before)
