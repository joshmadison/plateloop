"""Realistic wall-time estimates for a print sequence."""
from __future__ import annotations

# Rough extras around the slicer print time. The slicer estimate covers the
# print itself (including the start sequence); the ejection and the re-heat
# between copies are PlateLoop's own additions. Keep in sync with index.html
# (jobDurationSec).
EJECT_BASE_S = 30        # retract, lift, park, descend (once per copy)
PUSH_MOVES_S = 20        # per object: move over, push, re-park
REHEAT_S = 180           # bed + nozzle back to print temperature (later copies)
TEMP_MODE_COOL_S = 900   # per object when cooling "until temperature" (M190)


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
