"""Print scheduling: realistic per-copy durations and print-window planning.

The printer has no clock that G-code can read, so a schedule is a timetable
fixed when the file is built: every wait is a chain of G4 dwells counted from
the moment the user presses "print". The planned start time therefore has to
match the real start; the file name and the UI say which time it is built for.

All times here are seconds relative to that planned start (t = 0).
"""
from __future__ import annotations

import re

DAY_S = 24 * 3600

# Rough extras around the slicer print time. The slicer estimate covers the
# print itself (including the start sequence); the ejection and the re-heat
# between copies are PlateLoop's own additions. Keep in sync with index.html
# (jobDurationSec).
EJECT_BASE_S = 30        # retract, lift, park, descend (once per copy)
PUSH_MOVES_S = 20        # per object: move over, push, re-park
REHEAT_S = 180           # bed + nozzle back to print temperature (later copies)
TEMP_MODE_COOL_S = 900   # per object when cooling "until temperature" (M190)

# Safety margin against drift: the slicer estimate is never exact, and the
# error adds up copy after copy. A copy is only started when the time left in
# the print window is larger than this share of the time elapsed so far.
DEFAULT_MARGIN = 0.05

MAX_SCHEDULE_DAYS = 31

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(value: str) -> int:
    """'20:00' -> seconds after midnight. Raises ValueError on bad input."""
    m = _HHMM.match((value or "").strip())
    if not m:
        raise ValueError(f"invalid time {value!r}, expected HH:MM")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60


def fmt_hhmm(sec_of_day: int) -> str:
    sec_of_day %= DAY_S
    return f"{sec_of_day // 3600:02d}:{sec_of_day % 3600 // 60:02d}"


def job_duration_s(
    print_time_s: float,
    n_objects: int,
    cool_time_s: int,
    safety_wait_s: int,
    first: bool,
) -> int:
    """Estimated wall time of one copy: print + ejection (+ re-heat)."""
    n = max(1, int(n_objects or 1))
    cool = cool_time_s if cool_time_s > 0 else TEMP_MODE_COOL_S
    eject = EJECT_BASE_S + n * (cool + safety_wait_s + PUSH_MOVES_S)
    reheat = 0 if first else REHEAT_S
    return int(round((print_time_s or 0) + eject + reheat))


class Window:
    """Daily print window [open, close) in seconds after midnight; may wrap."""

    def __init__(self, open_s: int, close_s: int):
        if open_s % DAY_S == close_s % DAY_S:
            raise ValueError("print window start and end must differ")
        self.open_s = open_s % DAY_S
        self.close_s = close_s % DAY_S
        self.length_s = (self.close_s - self.open_s) % DAY_S

    def until_close(self, tod: int) -> int:
        """Seconds until the window closes, 0 when it is closed at `tod`."""
        into = (tod - self.open_s) % DAY_S
        return self.length_s - into if into < self.length_s else 0

    def until_open(self, tod: int) -> int:
        """Seconds until the next opening (0 exactly at the opening)."""
        return (self.open_s - tod) % DAY_S


def plan(
    durations_s: list[int],
    start_s: int,
    window: Window,
    margin: float = DEFAULT_MARGIN,
) -> dict:
    """Place copies into the daily print window.

    A copy starts only inside the window and only when the time left in the
    window exceeds `margin` x the time elapsed so far; it may run past the
    window's end. Otherwise the printer waits for the next opening.

    Returns {"jobs": [{"start_s", "end_s", "wait_s"}], "total_s"} with
    `wait_s` = idle time right before that copy.
    """
    t = 0
    jobs = []
    for dur in durations_s:
        tod = (start_s + t) % DAY_S
        left = window.until_close(tod)
        if left > 0 and left > margin * t:
            wait = 0
        elif left > 0:  # open, but too close to the end: skip to next night
            wait = left + window.until_open((tod + left) % DAY_S)
        else:
            wait = window.until_open(tod)
        t += wait
        jobs.append({"start_s": t, "end_s": t + int(dur), "wait_s": wait})
        t += int(dur)
        if t > MAX_SCHEDULE_DAYS * DAY_S:
            raise ValueError(
                f"the schedule would run longer than {MAX_SCHEDULE_DAYS} days - "
                "use fewer copies or a longer print window"
            )
    return {"jobs": jobs, "total_s": t}


def sessions(jobs: list[dict]) -> list[dict]:
    """Group copies that run back to back (one block per night)."""
    out: list[dict] = []
    for i, j in enumerate(jobs, 1):
        if out and j["wait_s"] == 0:
            out[-1]["last"] = i
            out[-1]["end_s"] = j["end_s"]
        else:
            out.append({"first": i, "last": i, "start_s": j["start_s"], "end_s": j["end_s"]})
    return out
