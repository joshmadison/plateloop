"""Gcode manipulation for real Bambu Studio sliced files.

Ejection logic V2 (see docs/ejection_logic.md):
- Phase 1 (once): retract filament -> lift to safe_z -> bed fully forward ->
  center X -> descend to push_z.
- Phase 2 (repeat until the plate is empty): pick the next object -> X to
  its bbox center -> cool down -> safety wait -> push until
  front_lip_y -> bed fully forward (re-park).
- Phase 3: re-heat + next print (in build_multi_plate_sequence).

Kinematics (A1 bedslinger):
- Y = rear_limit (~262): bed fully FORWARD, toolhead behind the plate (park)
- Y = front_lip_y (~0): bed fully BACK, toolhead in front of the plate (push end)
- Y decreasing: toolhead pushes the object forward over the plate's front edge
"""
import re

EJECT_BEGIN = "; ===== plateloop auto-ejection (generated) ====="
EJECT_END = "; ===== end auto-ejection ====="

EXEC_START = "; EXECUTABLE_BLOCK_START"
EXEC_END = "; EXECUTABLE_BLOCK_END"


def find_block_indices(lines: list) -> tuple:
    """Return (exec_start_idx, exec_end_idx) or -1s."""
    exec_start = exec_end = -1
    for i, line in enumerate(lines):
        s = line.strip()
        if s == EXEC_START and exec_start == -1:
            exec_start = i
        elif s == EXEC_END:
            exec_end = i
    return exec_start, exec_end


MACHINE_END_START = "; MACHINE_END_GCODE_START"


def _shutdown_split_index(lines: list) -> int:
    """First line index of the native shutdown inside the exec block.

    The shutdown starts at the first 'M140 S0' of the machine END gcode.
    Searching from the top would also hit a start gcode or layer change that
    sets the bed to 0 C (bed temp 0 for other layers, Klipper-style starts)
    and turn the whole print into the "shutdown". The search starts at
    '; MACHINE_END_GCODE_START' (Bambu/Orca) or, without that marker, after
    the last layer change.
    """
    begin = 0
    for i, line in enumerate(lines):
        if line.strip() == MACHINE_END_START:
            begin = i
            break
    else:
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].startswith("; CHANGE_LAYER") or lines[i].startswith(";LAYER_CHANGE"):
                begin = i
                break
    for i in range(begin, len(lines)):
        if lines[i].strip().startswith("M140 S0"):
            return i
    return -1


START_GCODE_END = "; MACHINE_START_GCODE_END"
REHEAT_NOZZLE_STANDBY = 140

# Only the sections that extrude ONTO the plate are dropped for prints 2+.
# Nozzle cleaning (';===== wipe nozzle', brush + steel wipe + Z re-home) and
# the off-plate flow calibration (';===== auto extrude cali') stay: the A1
# probes the bed with the nozzle, so it must be clean before G29.
# A section without an end banner ends at the next ';=====' banner (or the
# start-gcode end): ';===== extrude cali test' is followed by the
# ';========turn off light ...' section with G29.1 (textured PEI offset),
# fans off and G29.4, which must stay.
_PURGE_SECTION_BANNERS = [
    (";===== nozzle load line ", ";===== nozzle load line end"),
    (";===== extrude cali test ", None),
]
_SECTION_BANNER = ";====="


def split_start_block(exec_block_lines: list) -> tuple:
    """Split the exec block at '; MACHINE_START_GCODE_END' (A1 native marker)."""
    for i, line in enumerate(exec_block_lines):
        if line.strip() == START_GCODE_END:
            return exec_block_lines[:i], exec_block_lines[i + 1:]
    return list(exec_block_lines), []


def find_purge_zones(exec_block_lines: list) -> list:
    """(start_idx, end_idx) spans of the front-of-plate purge/cali zone."""
    spans = []
    for start_banner, end_banner in _PURGE_SECTION_BANNERS:
        si = -1
        for i, line in enumerate(exec_block_lines):
            s = line.strip()
            if si == -1:
                if s.startswith(start_banner):
                    si = i
            elif end_banner is not None and s.startswith(end_banner):
                spans.append((si, i))
                si = -1
        if si != -1 and end_banner is None:
            ei = next(
                (
                    j for j in range(si + 1, len(exec_block_lines))
                    if exec_block_lines[j].strip() == START_GCODE_END
                    or exec_block_lines[j].strip().startswith(_SECTION_BANNER)
                ),
                len(exec_block_lines),
            )
            if ei > si:
                spans.append((si, ei - 1))
    return spans


def build_purge_free_start(exec_block_lines: list, nozzle_temp: int | None = None) -> list | None:
    """Machine-start section WITHOUT the first-print purge line.

    The dropped purge section also held the only 'M109 <print temp>' of the
    A1 start (after leveling the nozzle sits at 140 C and 'M104 S220 ;
    prepare to print' does not wait). With nozzle_temp the start therefore
    ends with: lift off the plate edge, wait for the print temperature.
    """
    pre, _post = split_start_block(exec_block_lines)
    if _post and not pre:
        return None
    spans = find_purge_zones(pre)
    if not spans:
        return None
    drop = set()
    for a, b in spans:
        drop.update(range(a, b + 1))
    kept = [ln for i, ln in enumerate(pre) if i not in drop]
    if nozzle_temp:
        kept.extend([
            "; plateloop: no purge line on prints 2+ - lift off the plate edge\n",
            "G90\n",
            "G1 Z5 F1200\n",
            f"M109 S{nozzle_temp} ; plateloop: wait for print temperature before the first layer\n",
        ])
    return kept


_PRINT_NOZZLE_MIN_C = 180


def parse_print_temps(gcode: str) -> tuple:
    r"""(bed_temp, nozzle_temp) of the first layer.

    Nozzle, first match wins:
      1. 'M104 S<t> ; prepare to print' (A1 start: the first filament's
         first-layer temperature)
      2. slicer config 'nozzle_temperature_initial_layer = 220[,230...]' /
         'first_layer_temperature = ...' (first filament)
      3. the last M104/M109 target >= 180 C before the first layer (the
         A1 start also heats to 140/170 C for probing and wiping)
    0 = unknown; build_multi_plate_sequence refuses to build then.
    The machine_end_gcode config comment ('... M104 S0') never matches:
    every command pattern is line-anchored. Patterns use [ \t]* instead of
    \s* so a match can never run across lines (no quadratic backtracking
    on crafted whitespace).
    """
    bed = 65
    nozzle = 0
    m = re.search(r"^M140 S(\d+)", gcode, re.MULTILINE)
    if m:
        bed = int(m.group(1))
    m = re.search(r"^M104 S(\d+)[ \t]*;[ \t]*prepare to print", gcode, re.MULTILINE)
    if m:
        nozzle = int(m.group(1))
    if not nozzle:
        m = re.search(
            r"^;[ \t]*(?:nozzle_temperature_initial_layer|first_layer_temperature)[ \t]*=[ \t]*(\d+)",
            gcode, re.MULTILINE,
        )
        if m:
            nozzle = int(m.group(1))
    if not nozzle:
        # last real heat-up target before the first layer (flush/standby
        # temperatures earlier in the start gcode are not the print temp)
        first_layer = gcode.find("\n; CHANGE_LAYER")
        start = gcode if first_layer == -1 else gcode[:first_layer]
        temps = [int(t) for t in re.findall(r"^M10[49] S(\d+)", start, re.MULTILINE)]
        temps = [t for t in temps if t >= _PRINT_NOZZLE_MIN_C]
        nozzle = temps[-1] if temps else 0
    return bed, nozzle


def parse_max_z_height(gcode: str) -> float:
    """max_z_height from the gcode header ('; max_z_height: 30.00')."""
    m = re.search(r";\s*max_z_height:\s*([\d.]+)", gcode)
    return float(m.group(1)) if m else 0.0


def _strip_native_shutdown(exec_block_lines: list) -> list:
    idx = _shutdown_split_index(exec_block_lines)
    if idx == -1:
        return list(exec_block_lines)
    return list(exec_block_lines[:idx])


def _native_shutdown(exec_block_lines: list) -> list:
    idx = _shutdown_split_index(exec_block_lines)
    if idx == -1:
        return []
    return list(exec_block_lines[idx:])


def _fmt(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _select_next_object(remaining: list, rear_limit: float) -> dict:
    """Dynamic object selection, ejection logic step 6 + 6.1.

    Criterion: smallest Y distance between the bbox edge (y_max) and the
    toolhead at Y = rear_limit.  distance = rear_limit - object.y_max
    The smallest distance = the rear-most object = pushed first.

    Tie-breaker (6.1): on equal distance pick the object whose bbox center
    (x_min + x_max) / 2 is closest to X = 0.
    """
    return min(remaining, key=lambda o: (
        rear_limit - o["y_max"],
        (o["x_min"] + o["x_max"]) / 2,
    ))


def _fmt_duration_s(sec: int) -> str:
    """'5 min', '90 s', '2 min 30 s' for G-code comments and preview labels."""
    sec = int(sec)
    m, r = divmod(sec, 60)
    if m and r:
        return f"{m} min {r} s"
    return f"{m} min" if m else f"{r} s"


# Bambu firmware caps a single G4 dwell at 90 s (community-documented, see
# x1plus-testing Gcode.md); longer waits are written as a chain of G4 lines.
G4_MAX_S = 90


def _dwell_lines(seconds: int, comment: str) -> list[str]:
    """G4 S<n> lines totalling `seconds`, none longer than G4_MAX_S."""
    seconds = int(seconds)
    if seconds <= 0:
        return []
    chunks = [G4_MAX_S] * (seconds // G4_MAX_S)
    if seconds % G4_MAX_S:
        chunks.append(seconds % G4_MAX_S)
    if len(chunks) == 1:
        return [f"G4 S{chunks[0]} ; {comment}"]
    n = len(chunks)
    return [f"G4 S{c} ; {comment} ({i}/{n})" for i, c in enumerate(chunks, 1)]


def _fmt_hm(sec: int) -> str:
    """'12 h 30 min', '45 min' for schedule comments."""
    h, m = divmod(int(sec) // 60, 60)
    if h and m:
        return f"{h} h {m} min"
    return f"{h} h" if h else f"{m} min"


def schedule_pause_block(
    wait_s: int, job_no: int, total_jobs: int, resume_at: str = "", bambu: bool = True
) -> str:
    """Idle wait before a print in a print-window schedule.

    Heaters and fans off, then a chain of G4 S<=90 dwells (the printer has no
    readable clock, so the wait is pure elapsed time). Placed only between
    a finished ejection and the next print's re-heat, never mid-print, so no
    part is left on the plate while it cools down.
    """
    when = f", resumes ~{resume_at}" if resume_at else ""
    lines = [
        f"; ===== plateloop schedule: wait {_fmt_hm(wait_s)} before print "
        f"{job_no} of {total_jobs}{when} =====",
        "M400 ; finish all queued moves before the wait",
        "M104 S0 ; schedule wait: nozzle heater off",
        "M140 S0 ; schedule wait: bed heater off",
        "M106 S0 ; schedule wait: part cooling fan off",
    ]
    if bambu:
        lines += [
            "M106 P2 S0 ; schedule wait: aux fan off",
            "M106 P3 S0 ; schedule wait: chamber fan off",
        ]
    lines += _dwell_lines(wait_s, "schedule wait")
    lines.append("; ===== plateloop schedule: wait end =====")
    return "\n".join(lines) + "\n"


# Minimum gap between the tallest part and the printer's Z limit: the
# toolhead travels over the parts at safe_z.
MIN_Z_CLEARANCE_MM = 2.0
# Extra margin for the gantry / toolhead clearance checks.
CLEARANCE_MARGIN_MM = 2.0


class EjectionUnsafe(ValueError):
    """The ejection for this plate would crash into the printer or a part."""


def _ejection_geometry(printer: dict) -> dict:
    machine = printer.get("machine", {})
    kin = printer.get("kinematics", {})
    th = printer.get("toolhead", {})
    printable = machine.get("printable", {})
    footprint = th.get("footprint", {})
    clearance_radius = th.get("clearance_radius", 73.0)
    return {
        "rear_limit": kin.get("rear_command_limit", 262.0),
        "front_lip": kin.get("front_lip_y", 0.0),
        "printable_x": printable.get("x", 256.0),
        "printable_z": printable.get("z", 0.0) or 0.0,
        "no_park": kin.get("rear_command_limit_source") == "no_park",
        "park_avoid": kin.get("park_avoid") or [],
        "structure": machine.get("structure", "bedslinger"),
        "head_w": footprint.get("width", min(36.0, clearance_radius * 0.5)),
        "head_d": footprint.get("depth", 28.0),
        # unknown rod geometry = 0: every part outside the push path counts
        "rod_h": th.get("height_to_rod", 0.0) or 0.0,
        "rod_dist": th.get("dist_to_rod", 0.0) or 0.0,
    }


def _park_y_for(cx: float, geo: dict) -> float:
    """Y where the toolhead descends behind the part.

    Normally rear_limit; inside a park_avoid zone (A1: nozzle wiper brush
    at the rear left) the toolhead descends in front of that zone instead.
    """
    for zone in geo["park_avoid"]:
        if zone.get("x_min", 0.0) <= cx <= zone.get("x_max", 0.0):
            return float(zone["y"])
    return geo["rear_limit"]


def _safe_z(max_z_height: float, t_lift: float, geo: dict) -> float:
    """Travel height over the parts, capped at the printer's Z limit.

    The A1 start gcode turns the soft endstops off (M211 X0 Y0 Z0), so a
    Z above printable.z drives Z into the hard stop and loses steps.
    """
    safe_z = max_z_height + t_lift
    z_limit = geo["printable_z"]
    if z_limit > 0:
        if max_z_height + MIN_Z_CLEARANCE_MM > z_limit:
            raise EjectionUnsafe(
                f"Part height {_fmt(max_z_height)} mm leaves less than "
                f"{_fmt(MIN_Z_CLEARANCE_MM)} mm to the printer's Z limit "
                f"({_fmt(z_limit)} mm) - the toolhead cannot travel over it"
            )
        safe_z = min(safe_z, z_limit)
    return round(safe_z, 2)


def _norm_objects(objects: list | None, geo: dict, max_z_height: float) -> list:
    objs = []
    for o in objects or []:
        xmin = o.get("x_min", 0.0)
        xmax = o.get("x_max", 0.0)
        ymin = o.get("y_min", 0.0)
        ymax = o.get("y_max", 0.0)
        if xmax > xmin and ymax > ymin:
            objs.append({
                "cx": round((xmin + xmax) / 2, 2),
                "x_min": xmin, "x_max": xmax,
                "y_min": ymin, "y_max": ymax,
                "z_max": o.get("z_max") or max_z_height,
                "name": o.get("name") or (f"Object {o['id']}" if "id" in o else "object"),
            })
    return objs


def ejection_clearance_problems(
    objects: list | None,
    printer: dict,
    max_z_height: float,
    push_z: float = 0.4,
    t_lift: float = 5.0,
) -> list:
    """Collisions the ejection would cause; empty list = safe to build.

    Replays the push order of build_ejection_snippet and checks per cycle:
    - Z: the tallest part must leave room below the printer's Z limit.
    - Descend: the toolhead lowers to push_z behind the pushed part; any
      part within the toolhead's X band whose rear edge is closer to the
      park position than half the toolhead depth is hit from above.
    - Gantry: while the push runs at push_z the X gantry (height_to_rod
      above the nozzle) sweeps over every part outside the push path. On a
      bedslinger only parts reaching further back than dist_to_rod from
      the push end are swept; on CoreXY machines all of them.
    Parts in front of the pushed part inside its X span are shoved along
    (cascade) - that is a warning (find_push_lane_conflicts), not a crash.
    """
    geo = _ejection_geometry(printer)
    problems = []
    try:
        _safe_z(max_z_height, t_lift, geo)
    except EjectionUnsafe as exc:
        problems.append(str(exc))
    objs = _norm_objects(objects, geo, max_z_height)
    half_w = geo["head_w"] / 2
    push_end_y = geo["front_lip"]
    remaining = list(objs)
    while remaining:
        obj = _select_next_object(remaining, geo["rear_limit"])
        remaining.remove(obj)
        cx = obj["cx"]
        park_y = _park_y_for(cx, geo)
        # descend behind the part (and next to any other part in the band)
        for other in [obj] + remaining:
            if other["x_max"] > cx - half_w and other["x_min"] < cx + half_w:
                gap = park_y - other["y_max"]
                if gap < geo["head_d"] / 2 + CLEARANCE_MARGIN_MM:
                    problems.append(
                        f"'{other['name']}' reaches to {_fmt(other['y_max'])} mm - only "
                        f"{_fmt(gap)} mm in front of the push start (Y {_fmt(park_y)}); "
                        "the toolhead would descend onto it"
                    )
        # gantry sweep over parts outside the push path
        path_min = min(obj["x_min"], cx - half_w)
        path_max = max(obj["x_max"], cx + half_w)
        rod_clear = geo["rod_h"] - push_z - CLEARANCE_MARGIN_MM
        for other in remaining:
            in_path = other["x_max"] > path_min and other["x_min"] < path_max
            if in_path and other["y_max"] <= obj["y_max"]:
                continue  # shoved along with the pushed part (cascade warning)
            swept = (geo["structure"] != "bedslinger"
                     or other["y_max"] > push_end_y + geo["rod_dist"])
            if swept and other["z_max"] > rod_clear:
                problems.append(
                    f"'{other['name']}' ({_fmt(other['z_max'])} mm tall) stands outside the push "
                    f"path of '{obj['name']}' - the X gantry ({_fmt(geo['rod_h'])} mm above the "
                    "nozzle) would hit it during the push"
                )
    # one message per problem
    seen = set()
    return [p for p in problems if not (p in seen or seen.add(p))]


def build_ejection_snippet(
    max_z_height: float,
    t_lift: float = 5.0,
    push_z: float = 0.4,
    cool_temp_bed: int = 30,
    safety_wait_s: int = 60,
    cool_time_s: int = 0,
    push_speed: int = 3000,
    printer: dict | None = None,
    objects: list | None = None,
) -> str:
    """Ejection sequence per ejection_logic.md (V2).

    Phase 1 (once, after the print ends):
      1. Print done - retract filament (E-6, fixed)
      2. Lift the toolhead to safe_z = max_z_height + t_lift (capped at the
         printer's Z limit), then hotend and fans off: the nozzle must not
         idle hot over the plate while the part cools (ooze, burnt PLA on
         the nozzle before the next probe)
      3. Bed fully forward - Y to rear_limit (toolhead behind the plate)

    Phase 2 (repeat until the plate is empty):
      6. Find the next object (dynamic, see _select_next_object)
      7. Toolhead X to the object's bbox center - still at safe_z, so the
         nozzle never drags along the rear edge (A1 wiper brush) at push_z
     4b. Inside a park_avoid zone (A1 wiper brush): Y in front of it
      5. Descend to push_z
    (Step numbers are kept from ejection_logic.md V2; steps 4/5 used to
    run once at the plate center before the X move.)
      8. Cool down: cool_time_s == 0 -> wait for cool_temp_bed (M140 + M190);
         cool_time_s > 0 -> bed heater off (M140 S0) and wait exactly that
         long (chained G4, max 90 s each). G-code has no "wait for
         temperature, but at most N s", and M190 alone can hang forever
         when the bed cannot get that cool (enclosure, warm room).
      9. Safety wait safety_wait_s (chained G4, max 90 s each)
     10. Push - Y to front_lip_y (object slides over the plate's front edge)
     11. Lift to safe_z, bed fully forward (re-park) -> continue at 6

    objects: per-object bboxes from parse_object_bboxes() (list of dicts
    with x_min/x_max/y_min/y_max). None/empty = single-object fallback.
    Raises EjectionUnsafe when the parts leave no room below the Z limit.
    """
    if printer is None:
        from .printer_db import get_printer
        printer = get_printer("A1")

    geo = _ejection_geometry(printer)
    rear_limit = geo["rear_limit"]
    no_park = geo["no_park"]
    park_comment = ("rear bed edge (no park zone - keep the rear-most part clear)"
                    if no_park else "bed fully forward (park)")
    repark_comment = ("rear bed edge (no park zone)" if no_park
                      else "bed fully forward (re-park)")
    final_park_comment = ("rear bed edge (final, no park zone)" if no_park
                          else "bed fully forward (final park)")

    safe_z = _safe_z(max_z_height, t_lift, geo)
    push_end_y = round(geo["front_lip"], 2)
    plate_center_x = round(geo["printable_x"] / 2, 2)

    objs = _norm_objects(objects, geo, max_z_height)
    if not objs:
        objs = [{"cx": plate_center_x, "x_min": 0.0, "x_max": geo["printable_x"],
                 "y_min": 0.0, "y_max": rear_limit * 0.8}]

    remaining = list(objs)

    lines = [
        EJECT_BEGIN,
        (
            f"; ejection V2: {len(objs)} object(s), safe_z={_fmt(safe_z)} "
            f"(max_z={_fmt(max_z_height)} + t_lift={_fmt(t_lift)})"
        ),
        f"; push_z={_fmt(push_z)}, "
        + (f"cool_time={int(cool_time_s)}s" if cool_time_s > 0 else f"cool_temp={cool_temp_bed}C")
        + f", safety_wait={safety_wait_s}s",
        "; ===== Phase 1: preparation =====",
        "M400 ; wait for motion buffer clear",
        "G91",
        "G1 E-6.0 F1200 ; step 1: retract filament",
        "G90",
        f"G1 Z{_fmt(safe_z)} F3000 ; step 2: lift toolhead to safe height",
        "M104 S0 ; step 2b: hotend off while the part cools (no ooze)",
        "M106 S0 ; step 2b: part cooling fan off",
        *(
            ["M106 P2 S0 ; step 2b: aux fan off", "M106 P3 S0 ; step 2b: chamber fan off"]
            if printer.get("vendor") == "bambu" else []
        ),
        f"G1 Y{_fmt(rear_limit)} F12000 ; step 3: {park_comment}",
    ]

    cycle = 0
    current_y = rear_limit
    while remaining:
        cycle += 1
        obj = _select_next_object(remaining, rear_limit)
        remaining.remove(obj)
        tag = f" (object {cycle}/{len(objs)})" if len(objs) > 1 else ""
        park_y = _park_y_for(obj["cx"], geo)

        lines.append(f"; ===== Phase 2: ejection cycle {cycle}/{len(objs)} =====")
        lines.append(
            f"G1 X{_fmt(obj['cx'])} F12000 ; step 7{tag}: toolhead to object center "
            f"at safe height (cx={_fmt(obj['cx'])})"
        )
        if park_y != current_y:
            lines.append(
                f"G1 Y{_fmt(park_y)} F12000 ; step 4b{tag}: move in front of the "
                "wiper brush / park-avoid zone"
            )
            current_y = park_y
        lines.append(f"G1 Z{_fmt(push_z)} F600 ; step 5{tag}: descend to push height")
        if cool_time_s > 0:
            lines.append("M140 S0 ; step 8: bed heater off while cooling")
            lines.extend(
                _dwell_lines(cool_time_s, f"step 8: cool down for {_fmt_duration_s(cool_time_s)}")
            )
        else:
            lines.append(f"M140 S{cool_temp_bed} ; step 8: set bed target to cooldown temp")
            lines.append(f"M190 S{cool_temp_bed} ; step 8: wait until bed reached cooldown temp")
        lines.extend(_dwell_lines(safety_wait_s, f"step 9: safety wait {safety_wait_s}s"))
        lines.append(
            f"G1 Y{_fmt(push_end_y)} F{push_speed} ; step 10{tag}: push - "
            f"bed fully retracts, toolhead pushes object off plate"
        )
        current_y = push_end_y
        if remaining:
            lines.append(
                f"G1 Z{_fmt(safe_z)} F3000 ; step 11a: lift to safe height "
                f"(clear remaining objects)"
            )
            lines.append(
                f"G1 Y{_fmt(rear_limit)} F12000 ; step 11b: {repark_comment}"
            )
            current_y = rear_limit
        else:
            lines.append(
                f"G1 Z{_fmt(safe_z)} F3000 ; step 11a: lift to safe height (final)"
            )
            lines.append(
                f"G1 Y{_fmt(rear_limit)} F12000 ; step 11: {final_park_comment}"
            )

    lines.append(EJECT_END)
    return "\n".join(lines) + "\n"


def _lane_displacements(objs: list, lane: list, toolhead_y: float) -> dict:
    """Displacement of each lane object while the toolhead's edge is at Y.

    lane: object indices sorted rear-most first (the pushed object first).
    The pushed object moves once the edge passes its rear (y_max); every
    object in front follows after contact, keeping its original gap.
    """
    dys: dict = {}
    prev = None
    for i in lane:
        if prev is None:
            d = min(0.0, toolhead_y - objs[i]["y_max"])
        else:
            gap = objs[prev]["y_min"] - objs[i]["y_max"]
            d = min(0.0, dys[prev] + gap)
        dys[i] = d
        prev = i
    return dys


def _frame_objects(objs: list, dys: dict, gone: set) -> list:
    """Per-frame state of EVERY object: index, current Y range, gone flag.

    dys: {object_index: displacement} for the objects currently being pushed
    (0 for everything else). gone objects are off the plate and not drawn.
    """
    out = []
    for i, ob in enumerate(objs):
        if i in gone:
            out.append({"index": i, "y_front": None, "y_rear": None, "gone": True})
        else:
            d = dys.get(i, 0.0)
            out.append({
                "index": i,
                "y_front": round(ob["y_min"] + d, 2),
                "y_rear": round(ob["y_max"] + d, 2),
                "gone": False,
            })
    return out


def ejection_keyframes(
    max_z_height: float,
    t_lift: float = 5.0,
    push_z: float = 0.4,
    cool_temp_bed: int = 30,
    safety_wait_s: int = 60,
    cool_time_s: int = 0,
    printer: dict | None = None,
    objects: list | None = None,
    print_end_pos: dict | None = None,
) -> dict:
    """Animation waypoints for the UI - MUST mirror build_ejection_snippet.

    Frames for phase 1 + phase 2 (per object) + closing.
    """
    if printer is None:
        from .printer_db import get_printer
        printer = get_printer("A1")

    machine = printer.get("machine", {})
    kin = printer.get("kinematics", {})
    th = printer.get("toolhead", {})
    geo = _ejection_geometry(printer)
    rear_limit = geo["rear_limit"]
    printable_x = geo["printable_x"]

    no_park = kin.get("rear_command_limit_source") == "no_park"

    try:
        safe_z = _safe_z(max_z_height, t_lift, geo)
    except EjectionUnsafe:
        # the build refuses this plate; the preview still shows the motion
        safe_z = round(max_z_height + t_lift, 2)
    push_end_y = round(geo["front_lip"], 2)
    plate_center_x = round(printable_x / 2, 2)

    clearance_radius = th.get("clearance_radius", 73.0)
    body_height = th.get("body_height", 4.76)
    footprint = th.get("footprint", {})
    toolhead_w = footprint.get("width", min(36.0, clearance_radius * 0.5))
    toolhead_d = footprint.get("depth", min(28.0, body_height * 3.0))

    objs = []
    if objects:
        for o in objects:
            xmin = o.get("x_min", 0.0)
            xmax = o.get("x_max", 0.0)
            ymin = o.get("y_min", 0.0)
            ymax = o.get("y_max", 0.0)
            if xmax > xmin and ymax > ymin:
                objs.append({
                    "cx": round((xmin + xmax) / 2, 2),
                    "x_min": xmin, "x_max": xmax,
                    "y_min": ymin, "y_max": ymax,
                })
    if not objs:
        objs = [{"cx": plate_center_x, "x_min": 0.0, "x_max": printable_x,
                 "y_min": 0.0, "y_max": rear_limit * 0.8}]

    remaining = list(objs)
    multi = len(objs) > 1

    end_x = plate_center_x
    end_y_pos = rear_limit * 0.8
    if print_end_pos and (print_end_pos.get("x") is not None or print_end_pos.get("y") is not None):
        end_x = print_end_pos.get("x") if print_end_pos.get("x") is not None else plate_center_x
        end_y_pos = print_end_pos.get("y") if print_end_pos.get("y") is not None else rear_limit * 0.8

    frames = []
    gone_set: set = set()

    def add_frame(fid, label, tx, ty, tz, dys=None, obj_index=None,
                  pushed_list=None, gone_now=None, wait=False):
        frame = {
            "id": fid,
            "label": label,
            "toolhead": {"x": round(tx, 2), "y": round(ty, 2), "z": round(tz, 2)},
            "objects": _frame_objects(objs, dys or {}, gone_set if gone_now is None else gone_now),
        }
        if obj_index is not None:
            frame["obj_index"] = obj_index
        if pushed_list is not None:
            frame["pushed"] = list(pushed_list)
        if wait:
            frame["wait"] = True
        frames.append(frame)

    # Phase 1: every object is on the plate and static (all drawn in 3D).
    # At print end the nozzle sits at the top layer (max_z_height); the real
    # lift to safe_z happens in the "lift" frame / native step 2.
    add_frame("done", "Print finished - retracting filament", end_x, end_y_pos, max_z_height)
    add_frame("lift", f"Lift toolhead to safe height ({_fmt(safe_z)} mm)", end_x, end_y_pos, safe_z)
    add_frame("park", ("Rear bed edge - toolhead at the plate edge (no park zone)"
                       if no_park else
                       "Bed slides fully forward - toolhead parks behind the plate"),
              end_x, rear_limit, safe_z)

    # Phase 2 (per object). obj_index refers to the input bbox list (same
    # order as objects_bboxes from /api/animation); 'pushed' = cumulative
    # ejected indices. Every frame carries ALL objects with their current
    # positions - objects sharing the push lane are shoved along (cascade).
    half_lane = toolhead_w / 2
    cycle = 0
    pushed = []
    while remaining:
        cycle += 1
        obj = _select_next_object(remaining, rear_limit)
        oidx = objs.index(obj)
        remaining.remove(obj)
        tag = f" - object {cycle}/{len(objs)}" if multi else ""

        # lane: remaining objects whose X band overlaps the toolhead lane
        # (same rule as the push-lane warning in gcode_bbox)
        lane = [
            i for i, ob in enumerate(objs)
            if i != oidx and i not in gone_set
            and ob["x_max"] > obj["cx"] - half_lane
            and ob["x_min"] < obj["cx"] + half_lane
        ]
        lane.sort(key=lambda i: -objs[i]["y_max"])
        if oidx not in gone_set:
            lane.insert(0, oidx)

        park_y = _park_y_for(obj["cx"], geo)
        add_frame(f"xcycle{cycle}", f"Toolhead moves to object center at safe height{tag}",
                  obj["cx"], rear_limit, safe_z, obj_index=oidx, pushed_list=pushed)
        if park_y != rear_limit:
            add_frame(f"avoid{cycle}", f"Move in front of the wiper brush{tag}",
                      obj["cx"], park_y, safe_z, obj_index=oidx, pushed_list=pushed)
        add_frame(f"lower{cycle}", f"Toolhead descends to push height ({_fmt(push_z)} mm){tag}",
                  obj["cx"], park_y, push_z, obj_index=oidx, pushed_list=pushed)
        cool_label = (f"Cooling for {_fmt_duration_s(cool_time_s)}" if cool_time_s > 0
                      else f"Cooling to {cool_temp_bed}°C")
        add_frame(f"cool{cycle}", f"{cool_label}{tag}",
                  obj["cx"], park_y, push_z, obj_index=oidx, pushed_list=pushed, wait=True)
        add_frame(f"wait{cycle}", f"Safety wait {safety_wait_s}s{tag}",
                  obj["cx"], park_y, push_z, obj_index=oidx, pushed_list=pushed, wait=True)

        pushed.append(oidx)
        contact_y = obj["y_max"]
        span = contact_y - push_end_y
        if span > 0:
            for step, frac in enumerate((1 / 3, 2 / 3), start=1):
                ty = contact_y - span * frac
                add_frame(f"push{cycle}_{step}", f"Push - object sliding off the plate{tag}",
                          obj["cx"], ty, push_z, dys=_lane_displacements(objs, lane, ty),
                          obj_index=oidx, pushed_list=pushed)
        final_dys = _lane_displacements(objs, lane, push_end_y)
        # objects shoved fully past the front edge fall off with this push
        fell = {i for i in lane
                if i != oidx and objs[i]["y_max"] + final_dys.get(i, 0.0) <= 0}
        add_frame(f"push{cycle}", f"Push - toolhead pushes the object off the plate{tag}",
                  obj["cx"], push_end_y, push_z, dys=final_dys,
                  obj_index=oidx, pushed_list=pushed, gone_now=gone_set | fell)
        gone_set.update(lane)

        if remaining:
            add_frame(f"lift{cycle}", f"Lift to safe height{tag}",
                      obj["cx"], push_end_y, safe_z, obj_index=oidx, pushed_list=pushed)
            add_frame(f"repark{cycle}", ("Rear bed edge - re-park (no park zone)" if no_park
                                         else "Bed fully forward - re-park") + tag,
                      obj["cx"], rear_limit, safe_z, obj_index=oidx, pushed_list=pushed)

    add_frame("done_all", "Plate empty - ready for next print",
              objs[pushed[-1]]["cx"], rear_limit, safe_z, pushed_list=pushed)

    return {
        "printable_area": {"width": printable_x, "depth": machine.get("printable", {}).get("y", 256.0)},
        "safe_z": safe_z,
        "push_end_y": push_end_y,
        "plate_center_x": plate_center_x,
        "objects": [{"id": i, "cx": ob["cx"], "rear_y": ob["y_max"],
                      "y_min": ob["y_min"], "y_max": ob["y_max"]}
                     for i, ob in enumerate(objs)],
        "toolhead_dims": {"width": toolhead_w, "depth": toolhead_d,
                          "body_height": body_height, "clearance_radius": clearance_radius,
                          "structure": machine.get("structure", "bedslinger")},
        "printer": {"model_name": printer.get("name", ""),
                     "rear_limit": rear_limit,
                     "rear_limit_source": kin.get("rear_command_limit_source"),
                     "suitability": printer.get("suitability", "needs-test"),
                     "structure": machine.get("structure", "bedslinger")},
        "frames": frames,
    }


def build_multi_plate_sequence(
    plate_gcodes: dict,
    plate_prints: dict,
    t_lift: float = 5.0,
    push_z: float = 0.4,
    cool_temp_bed: int = 30,
    safety_wait_s: int = 60,
    cool_time_s: int = 0,
    eject_enabled: bool = True,
    min_height_mm: float = 7.0,
    printer: dict | None = None,
    plate_objects: dict | None = None,
    plate_max_z: dict | None = None,
    job_waits: dict | None = None,
) -> tuple:
    """Merge several plate gcodes into ONE sequential print job.

    Job order: plates ascending, each plate runs its own number of prints
    (plate_prints = TOTAL prints per plate, 0/None = skip the plate). Every
    job is followed by that plate's ejection push (including the very last
    job). The first-print purge/calibration strip runs exactly ONCE for the
    whole sequence; every later job (including the first print of the next
    plate) uses its plate's purge-free start (cleaning + leveling kept, wait
    for print temperature before the first layer). Re-heat targets the NEXT
    job's bed temp; the nozzle only goes to standby. The native shutdown runs only after the very last job.

    plate_gcodes:  {plate_index: gcode_text}
    plate_prints:  {plate_index: total_prints}
    plate_objects: {plate_index: parse_object_bboxes() result}
    plate_max_z:   {plate_index: max_z_height} (parsed from gcode when absent)
    job_waits:     {job_no (1-based): schedule_pause_block() text} - idle wait
                   right before that print (print-window schedule). Job 1's
                   wait runs before the start gcode; later waits run after the
                   previous job's ejection, before the re-heat.
    Returns (new_gcode, notes).
    """
    notes = []
    if printer is None:
        from .printer_db import get_printer
        printer = get_printer("A1")

    jobs = []
    for idx in sorted(plate_gcodes):
        n = int((plate_prints or {}).get(idx, 0) or 0)
        if n > 0:
            jobs.append((idx, n))
    if not jobs:
        return "", ["No plates selected - nothing to build"]

    pinfo: dict = {}
    purge_free_used = False
    active = {idx for idx, _ in jobs}
    for idx, gcode in plate_gcodes.items():
        if idx not in active:
            continue  # skipped plates are never split (memory) or checked
        if parse_print_temps(gcode)[1] < _PRINT_NOZZLE_MIN_C:
            raise EjectionUnsafe(
                f"Plate {idx}: could not determine the nozzle print temperature - "
                "refusing to build (the next copy would start with a cold nozzle)"
            )
        lines = gcode.splitlines(keepends=True)
        exec_start, exec_end = find_block_indices(lines)
        has_markers = exec_start != -1 and exec_end != -1
        if has_markers:
            header = lines[:exec_start]
            exec_block = lines[exec_start : exec_end + 1]
            body = _strip_native_shutdown(exec_block)
            shutdown = _native_shutdown(exec_block)
            body_first = body
            body_rest = body
            _pre, post = split_start_block(exec_block)
            if post and eject_enabled:
                purge_free = build_purge_free_start(exec_block, parse_print_temps(gcode)[1])
                if purge_free is not None:
                    body_rest = _strip_native_shutdown(purge_free + post)
                    purge_free_used = True
                else:
                    notes.append(
                        f"Plate {idx}: no purge-section banners found - full "
                        "start gcode replayed for its prints"
                    )
        else:
            notes.append(f"Plate {idx}: no EXECUTABLE_BLOCK markers found - using fallback append mode")
            header = []
            body = _strip_native_shutdown(lines)
            shutdown = _native_shutdown(lines)
            body_first = body_rest = body

        objs = (plate_objects or {}).get(idx)
        mz = (plate_max_z or {}).get(idx, 0.0) or 0.0
        if not mz:
            mz = parse_max_z_height(gcode)
        if not mz:
            mz = 20.0
            notes.append(f"Plate {idx}: no max_z_height found - using 20mm fallback")
        if eject_enabled and not objs:
            notes.append(f"Plate {idx}: no object bbox available - blind push at plate center used")
        ejection = (
            build_ejection_snippet(
                mz,
                t_lift=t_lift,
                push_z=push_z,
                cool_temp_bed=cool_temp_bed,
                safety_wait_s=safety_wait_s,
                cool_time_s=cool_time_s,
                printer=printer,
                objects=objs,
            )
            if eject_enabled
            else ""
        )
        bed_temp, _nozzle_temp = parse_print_temps(gcode)
        # The nozzle is only preheated (140 C, the A1 start's own standby
        # value): at print temperature it would ooze before the start gcode
        # cleans it. The start gcode heats to print temperature itself; the
        # purge-free start ends with an explicit M109.
        reheat = (
            f"; re-heat for next print\n"
            f"M140 S{bed_temp} ; heat bed\n"
            f"M104 S{REHEAT_NOZZLE_STANDBY} ; nozzle standby (no ooze before cleaning)\n"
            f"M190 S{bed_temp} ; wait bed\n"
        )
        pinfo[idx] = {
            "header": header,
            "body_first": body_first,
            "body_rest": body_rest,
            "shutdown": shutdown,
            "has_markers": has_markers,
            "ejection": ejection,
            "reheat": reheat,
        }

    if purge_free_used:
        notes.append(
            "Every print after the very first skips the first-print "
            "purge/calibration strip (would print onto the ejected part)"
        )

    total_jobs = sum(n for _, n in jobs)
    parts = ["".join(pinfo[jobs[0][0]]["header"])]
    job_no = 0
    for idx, n in jobs:
        for c in range(n):
            job_no += 1
            first_job = job_no == 1
            last_job = job_no == total_jobs
            info = pinfo[idx]
            if eject_enabled and not first_job:
                parts.append(
                    f"\n; ===== plateloop print {job_no} of {total_jobs} "
                    f"(plate {idx}, print {c + 1} of {n}) =====\n"
                )
                parts.append(info["reheat"])
            body = list(info["body_first"] if first_job else info["body_rest"])
            if first_job and (job_waits or {}).get(1):
                at = 1 if body and body[0].strip() == EXEC_START else 0
                body.insert(at, job_waits[1])
            parts.append("".join(body))
            if eject_enabled:
                parts.append(info["ejection"])
                if not last_job and (job_waits or {}).get(job_no + 1):
                    parts.append(job_waits[job_no + 1])
                if not last_job:
                    if info["has_markers"]:
                        parts.append(EXEC_END + "\n")  # balance START/END per job
                else:
                    parts.append("".join(info["shutdown"]))  # includes END
    parts.append("\n")

    if eject_enabled and min_height_mm and min_height_mm > 0:
        notes.append(
            f"Ejection push assumes objects >= {min_height_mm}mm tall - "
            "shorter parts may jam the toolhead"
        )
    return "".join(parts), notes
