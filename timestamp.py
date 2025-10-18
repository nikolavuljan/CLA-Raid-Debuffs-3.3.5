# timestamp.py
from __future__ import annotations
import re
from dataclasses import dataclass
from datetime import datetime

# e.g. "7/8 21:55:31.395"
FMT = "%m/%d %H:%M:%S.%f"

def split_arg0(arg0: str):
    """
    arg0 looks like: '7/8 21:55:31.395  SPELL_AURA_APPLIED'
    We split on the first run of >=2 spaces between timestamp and event.
    Returns (calendar_str, event_type) with BOTH sides stripped.
    """
    s = arg0.rstrip("\n")
    m = re.search(r"\s{2,}", s)
    if m:
        calendar = s[:m.start()].strip()
        event = s[m.end():].strip()
    else:
        # Fallback: split on last single space (handles odd lines)
        left, right = s.rsplit(" ", 1)
        calendar = left.strip()
        event = right.strip()
    return calendar, event

@dataclass
class LogClock:
    """Keeps a zero baseline at the first seen timestamp in the file."""
    _baseline_seconds: float | None = None

    def to_seconds(self, calendar_str: str) -> float:
        # No year in log; anchor to current year so we can compute deltas.
        dt = datetime.strptime(calendar_str, FMT).replace(year=datetime.now().year)
        sec = (dt - datetime(dt.year, 1, 1)).total_seconds()
        if self._baseline_seconds is None:
            self._baseline_seconds = sec
            return 0.0
        return max(0.0, sec - self._baseline_seconds)

def arg0_to_rel_seconds(arg0: str, clock: LogClock) -> float:
    calendar, _ = split_arg0(arg0)
    return clock.to_seconds(calendar)
