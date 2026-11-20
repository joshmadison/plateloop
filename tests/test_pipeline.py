"""Pipeline tests with V2 ejection logic (ejection_logic.md).

Tests the real Bambu Studio structure: HEADER_BLOCK, CONFIG_BLOCK,
EXECUTABLE_BLOCK_START/END, max_z_height in header, 2D bbox list in
plate JSON, M140/M104 real commands in the exec block.
"""
import io
import json
import re
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import PNG_1PX, make_multi_plate_3mf, make_project_3mf  # noqa: E402

from app.gcode import (  # noqa: E402
    build_ejection_snippet,
    build_multi_plate_sequence,
    build_purge_free_start,
    ejection_keyframes,
    find_block_indices,
    find_purge_zones,
    parse_max_z_height,
    parse_print_temps,
    split_start_block,
)
from app.gcode_bbox import find_push_lane_conflicts, parse_object_bboxes  # noqa: E402
from app.printer_db import (  # noqa: E402
    PRINTERS,
    PROVENANCE_LEVELS,
    STRUCTURES,
    SUITABILITY_LEVELS,
    VENDORS,
    get_printer,
    is_known_printer,
    resolve_display_name,
)
from app.threemf import (  # noqa: E402
    extract_plate_gcodes,
    parse_3mf,
    repack_single_plate_3mf,
)


def _gcode(data: bytes) -> str:
    """Single-plate gcode text from a fixture .gcode.3mf."""
    return extract_plate_gcodes(io.BytesIO(data))[1]


# ====== GCODE fixtures (mirror conftest but available for unit tests) ======

GCODE = """; HEADER_BLOCK_START
; BambuStudio 02.08.02.61
; model printing time: 1h 6m 48s; total estimated time: 1h 13m 5s
; total layer number: 150
; max_z_height: 30.00
; HEADER_BLOCK_END

; CONFIG_BLOCK_START
; bed_temperature_formula = by_first_filament
; machine_end_gcode = ;===== date: 20260513 =====================\\nM140 S0 ; turn off bed\\nM104 S0\\nM18 X Y Z\\n
; CONFIG_BLOCK_END

; EXECUTABLE_BLOCK_START
M73 P0 R73
M106 S0
G392 S0
M104 S140
M140 S65
M109 S25 H140
M104 S220
M109 S220
G28 Z
M190 S65; ensure bed temp
M104 S220 ; prepare to print
M109 S220
G1 X100 Y100 E1.0 F6000
G1 X150 Y100 E2.0 F6000
G1 X150 Y150 E3.0 F6000
M400
M140 S0 ; turn off bed
M106 S0 ; turn off fan
M620 S255
G1 X267 F15000
T255
M621 S255
M104 S0 ; turn off hotend
M17 S
G1 Z256 F600
M400 P100
M17 R
G90
G1 X-48 Y180 F3600
M220 S100
M201.2 K1.0
M73.2   R1.0
M1002 set_gcode_claim_speed_level : 0
M17
M400 S1
M1006 W
M400
M18 X Y Z
M73 P100 R0
; EXECUTABLE_BLOCK_END
"""

GCODE_PURGE = """; HEADER_BLOCK_START
; BambuStudio 02.08.02.61
; model printing time: 1h 6m 48s; total estimated time: 1h 13m 5s
; total layer number: 150
; max_z_height: 30.00
; HEADER_BLOCK_END

; EXECUTABLE_BLOCK_START
M140 S65
M109 S220
G28
;===== extrude cali test ===============================
M622 J1
    G0 X128 E10 F720
    G0 X148 E.3742 F4800
M623
;========turn off light and wait extrude temperature =============
G29.1 Z-0.02 ; for Textured PEI Plate
M106 S0 ; turn off fan
; MACHINE_START_GCODE_END
G1 X120 Y1.2 F6000
G1 X140 Y1.2 E.5 F6000 ; purge line on plate edge
G1 X100 Y100 E1.0 F6000
G1 X150 Y100 E2.0 F6000
G1 X150 Y150 E3.0 F6000
M140 S0 ; turn off bed
M18 X Y Z
; EXECUTABLE_BLOCK_END
"""


def _make_3mf(height: float = 30.0, with_purge: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr(
            "Metadata/model_settings.config",
            '<config><plate><metadata key="gcode_file" value="Metadata/plate_1.gcode"/></plate></config>',
        )
        objects = {
            "bbox_all": [50, 150, 100, 200],
            "bbox_objects": [
                {"area": 1756.13, "bbox": [50, 150, 100, 200], "id": 223, "layer_height": 0.2, "name": "Lenker.stl"}
            ],
            "bed_type": "textured_plate",
            "version": 2,
        }
        zf.writestr("Metadata/plate_1.json", json.dumps(objects))
        zf.writestr("Metadata/plate_1.gcode", (GCODE_PURGE if with_purge else GCODE).replace("30.00", f"{height:.2f}"))
    return buf.getvalue()


# ====== Tests ======

def test_parser_reads_max_z_height():
    """max_z_height from gcode header is parsed into ThreeMFInfo and PlateInfo."""
    tall = _make_3mf(30.0)
    info = parse_3mf(io.BytesIO(tall), "lenker.gcode.3mf")
    assert info.is_gcode_3mf
    assert info.object_count == 1
    assert abs(info.max_z_height - 30.0) < 0.01
    assert abs(info.plates[0].max_z_height - 30.0) < 0.01
    assert abs(info.min_object_height - 30.0) < 0.01


def test_parse_print_temps():
    """Bed + nozzle temp from real commands in the exec block."""
    tall = _make_3mf(30.0)
    gc = _gcode(tall)
    bed, noz = parse_print_temps(gc)
    assert bed == 65
    assert noz == 220


def test_parse_print_temps_ignores_end_gcode_comment():
    """machine_end_gcode metadata 'M104 S0' must never become the reheat target."""
    gcode = (
        "; CONFIG_BLOCK_START\n"
        "; machine_end_gcode = ;===== date =====\\nM140 S0 ; turn off bed\\nM104 S0\\nM18 X Y Z\\n"
        "; CONFIG_BLOCK_END\n"
        "; EXECUTABLE_BLOCK_START\n"
        "M140 S65\n"
        "M104 S250\n"
        "M109 S250\n"
        "; EXECUTABLE_BLOCK_END\n"
    )
    bed, noz = parse_print_temps(gcode)
    assert bed == 65
    assert noz == 250, "line-anchored M104 must skip the comment metadata"


def test_parse_max_z_height_from_gcode():
    """parse_max_z_height extracts the value from the gcode header."""
    gc = _gcode(_make_3mf(30.0))
    assert abs(parse_max_z_height(gc) - 30.0) < 0.01


def test_sequence_no_ejection_mode():
    """Ejection disabled: full exec blocks repeat, no ejection code."""
    gc = _gcode(_make_3mf(30.0))
    new_gc, _ = build_multi_plate_sequence({1: gc}, {1: 2}, eject_enabled=False)
    ls = new_gc.splitlines()
    starts = sum(1 for ln in ls if ln.strip() == "; EXECUTABLE_BLOCK_START")
    assert starts == 2
    assert "M190 S45" not in new_gc


def test_ejection_snippet_geometry():
    """safe_z = max_z_height + t_lift, push_z correct, step order enforced."""
    snip = build_ejection_snippet(
        max_z_height=30.0, t_lift=5.0, push_z=0.4,
        cool_temp_bed=50, safety_wait_s=60,
    )
    assert "G1 Z0.4 F600 ; step 5: descend to push height" in snip
    assert "G1 Z35" in snip, "safe z wrong (30+5=35)"
    li = snip.splitlines()

    def order_of(pat):
        return next((i for i, ln in enumerate(li) if pat in ln), -1)

    o_retract = order_of("step 1: retract")
    o_lift = order_of("step 2: lift")
    o_park = order_of("step 3: bed fully forward")
    o_x = order_of("step 7")
    o_descend = order_of("step 5: descend")
    o_cool = order_of("step 8: set bed target")
    o_wait = order_of("step 9: safety wait")
    o_push = order_of("step 10")
    # X travel happens at safe height, the descend to push_z only above the
    # object's center (never a sideways move at push_z along the rear edge)
    assert 0 < o_retract < o_lift < o_park < o_x < o_descend
    assert o_descend < o_cool < o_wait < o_push
    assert "G28" not in snip, "G28 Z re-home must not be in ejection"


def test_ejection_g4_s_format():
    """G4 uses S<seconds> (Bambu convention), not P<milliseconds>."""
    snip = build_ejection_snippet(
        max_z_height=30.0, safety_wait_s=60,
    )
    assert "G4 S60" in snip, "must use G4 S<seconds>"
    assert "G4 P" not in snip, "must NOT use G4 P<ms>"


def test_dwell_lines_split_at_90_s():
    """Bambu caps one G4 at 90 s: longer waits become a chain of G4 lines."""
    from app.gcode import G4_MAX_S, _dwell_lines

    assert _dwell_lines(0, "x") == []
    assert _dwell_lines(60, "x") == ["G4 S60 ; x"]
    assert _dwell_lines(90, "x") == ["G4 S90 ; x"]
    assert _dwell_lines(91, "x") == ["G4 S90 ; x (1/2)", "G4 S1 ; x (2/2)"]
    for total in (179, 180, 600, 3600, 43200):
        lines = _dwell_lines(total, "x")
        secs = [int(ln.split()[1][1:]) for ln in lines]
        assert sum(secs) == total
        assert max(secs) <= G4_MAX_S


def test_ejection_never_emits_g4_over_90_s():
    import re

    snip = build_ejection_snippet(max_z_height=30.0, safety_wait_s=600, cool_time_s=3600)
    waits = [int(m) for m in re.findall(r"^G4 S(\d+)", snip, re.MULTILINE)]
    assert waits and max(waits) <= 90
    assert sum(waits) == 3600 + 600


def test_ejection_implied_lift_between_objects():
    """Multi-object: lift+repark+descend between objects (collision avoidance)."""
    objects = [
        {"id": 100, "x_min": 100.0, "x_max": 150.0, "y_min": 105.0, "y_max": 115.0, "moves": 10},
        {"id": 200, "x_min": 200.0, "x_max": 220.0, "y_min": 150.0, "y_max": 200.0, "moves": 10},
    ]
    snip = build_ejection_snippet(
        max_z_height=30.0, t_lift=5.0, push_z=0.4,
        printer=get_printer("A1"), objects=objects,
    )
    assert "2 object(s)" in snip
    li = snip.splitlines()

    # After first push (step 10), before next object's X-move:
    # must have lift (11a) and repark (11b); the descend follows the X move
    push1_idx = next(i for i, ln in enumerate(li) if "step 10" in ln and "object 1/2" in ln)
    xcycle2_idx = next(i for i, ln in enumerate(li) if "step 7" in ln and "object 2/2" in ln)
    between = li[push1_idx:xcycle2_idx]

    has_lift = any("step 11a: lift" in ln for ln in between)
    has_repark = any("step 11b: bed fully forward" in ln for ln in between)
    assert has_lift, "missing lift between objects (collision!)"
    assert has_repark, "missing repark between objects"
    assert not any(ln.startswith("G1 Z0.4") for ln in between), "no descend before the X move"
    assert li[xcycle2_idx + 1].startswith("G1 Z0.4") and "step 5" in li[xcycle2_idx + 1]

    # After last push: lift to safe height FIRST, then final park - the
    # toolhead must never sweep back over the plate at push height (A2).
    push2_idx = next(i for i, ln in enumerate(li) if "step 10" in ln and "object 2/2" in ln)
    after_last = li[push2_idx:]
    has_final_park = any("step 11: bed fully forward (final park)" in ln for ln in after_last)
    assert has_final_park, "missing final park after last push"
    lift_final = [i for i, ln in enumerate(after_last) if "step 11a: lift to safe height (final)" in ln]
    park_final = [i for i, ln in enumerate(after_last) if "step 11: bed fully forward (final park)" in ln]
    assert lift_final, "final park must be preceded by a lift to safe_z"
    assert lift_final[0] < park_final[0], "lift must come BEFORE the final park"


def test_object_selection_rear_most_first():
    """Rear-most object (smallest abstand = rear_limit - y_max) pushed first."""
    objects = [
        {"id": 100, "x_min": 100.0, "x_max": 150.0, "y_min": 105.0, "y_max": 115.0, "moves": 10},
        {"id": 200, "x_min": 200.0, "x_max": 220.0, "y_min": 150.0, "y_max": 200.0, "moves": 10},
    ]
    pr = get_printer("A1")
    # abstand_100 = 262-115 = 147, abstand_200 = 262-200 = 62
    # obj 200 has smaller abstand → pushed first
    snip = build_ejection_snippet(
        max_z_height=30.0, printer=pr, objects=objects,
    )
    li = snip.splitlines()
    first_x = next(ln for ln in li if "step 7" in ln and "object 1/2" in ln)
    assert "X210" in first_x, f"rear-most (obj200, cx=210) must be first: {first_x}"


def test_object_selection_tiebreaker():
    """Same y_max → closest to X=0 (smallest cx) pushed first."""
    objects = [
        {"id": 1, "x_min": 180.0, "x_max": 220.0, "y_min": 100.0, "y_max": 200.0, "moves": 10},
        {"id": 2, "x_min": 10.0, "x_max": 50.0, "y_min": 100.0, "y_max": 200.0, "moves": 10},
    ]
    pr = get_printer("A1")
    snip = build_ejection_snippet(
        max_z_height=30.0, printer=pr, objects=objects,
    )
    li = snip.splitlines()
    first_x = next(ln for ln in li if "step 7" in ln and "object 1/2" in ln)
    # obj2 cx=30, obj1 cx=200 → obj2 closer to X=0 → pushed first
    assert "X30" in first_x, f"tiebreaker: closest to X=0 first: {first_x}"


def test_push_lane_conflict_detection():
    """The toolhead lane (center ± half width) decides conflicts, not bbox touch."""
    overlapping = [
        {"id": 11, "x_min": 100.0, "x_max": 150.0, "y_min": 120.0, "y_max": 150.0},
        {"id": 22, "x_min": 120.0, "x_max": 180.0, "y_min": 40.0, "y_max": 90.0},
    ]
    conflicts = find_push_lane_conflicts(overlapping)
    assert len(conflicts) == 1
    rear, at_risk = conflicts[0]
    assert rear["id"] == 11 and at_risk["id"] == 22, "rear-most is pushed first"
    # near miss: boxes do NOT touch, but the toolhead body still reaches it
    near = [
        {"id": 1, "x_min": 140.0, "x_max": 160.0, "y_min": 100.0, "y_max": 200.0},
        {"id": 2, "x_min": 162.0, "x_max": 200.0, "y_min": 100.0, "y_max": 150.0},
    ]
    assert len(find_push_lane_conflicts(near)) == 1, \
        "object within half toolhead width must be flagged"
    disjoint = [
        {"id": 1, "x_min": 10.0, "x_max": 50.0, "y_min": 10.0, "y_max": 60.0},
        {"id": 2, "x_min": 200.0, "x_max": 240.0, "y_min": 100.0, "y_max": 200.0},
    ]
    assert find_push_lane_conflicts(disjoint) == []


def test_keyframes_mirror_gcode():
    """Keyframes must mirror the generated gcode for single object."""
    pr = get_printer("A1")
    max_z = 30.0
    t_lift = 5.0
    push_z = 0.4
    snip = build_ejection_snippet(
        max_z_height=max_z, t_lift=t_lift, push_z=push_z, printer=pr,
    )
    kf = ejection_keyframes(
        max_z_height=max_z, t_lift=t_lift, push_z=push_z, printer=pr,
    )
    # safe_z matches
    safe_z_match = re.search(r"safe_z=(\S+)", snip)
    assert safe_z_match
    assert abs(float(safe_z_match.group(1).split()[0]) - kf["safe_z"]) < 0.01
    # push_end_y matches
    assert abs(kf["push_end_y"] - pr["kinematics"]["front_lip_y"]) < 0.01
    # plate_center_x matches
    assert abs(kf["plate_center_x"] - pr["machine"]["printable"]["x"] / 2) < 0.01
    # toolhead geometry from printer db
    assert kf["toolhead_dims"]["clearance_radius"] == pr["toolhead"]["clearance_radius"]
    assert kf["toolhead_dims"]["body_height"] == pr["toolhead"]["body_height"]
    # frames: done, lift, park, xcycle1 (X at safe height), lower1, cool1,
    # wait1, push1_1, push1_2, push1 (push travel sub-steps), done_all
    frame_ids = [f["id"] for f in kf["frames"]]
    assert frame_ids == ["done", "lift", "park",
                         "xcycle1", "lower1", "cool1", "wait1",
                         "push1_1", "push1_2", "push1", "done_all"], \
        f"single-object frame ids changed: {frame_ids}"
    # final park happens at safe height (A2: never sweep the plate at push_z)
    done_all = next(f for f in kf["frames"] if f["id"] == "done_all")
    assert abs(done_all["toolhead"]["z"] - kf["safe_z"]) < 0.01
    # print-end frame sits at the top layer, the lift frame at safe_z
    done = next(f for f in kf["frames"] if f["id"] == "done")
    lift = next(f for f in kf["frames"] if f["id"] == "lift")
    assert abs(done["toolhead"]["z"] - max_z) < 0.01
    assert abs(lift["toolhead"]["z"] - kf["safe_z"]) < 0.01


def test_keyframes_cascade_pushes_lane_objects():
    """Objects in the same push lane move along once contact happens."""
    objects = [
        {"id": 1, "x_min": 100.0, "x_max": 150.0, "y_min": 150.0, "y_max": 200.0, "moves": 10},
        {"id": 2, "x_min": 100.0, "x_max": 150.0, "y_min": 30.0, "y_max": 80.0, "moves": 10},
        {"id": 3, "x_min": 220.0, "x_max": 250.0, "y_min": 100.0, "y_max": 160.0, "moves": 10},
    ]
    pr = get_printer("A1")
    kf = ejection_keyframes(max_z_height=30.0, push_z=0.4, printer=pr, objects=objects)
    fr = {f["id"]: f for f in kf["frames"]}

    def states(fid):
        return {o["index"]: o for o in fr[fid]["objects"]}

    # phase 1: all objects on the plate, none gone
    for fid in ("done", "lift", "park", "xcycle1", "lower1"):
        s = states(fid)
        assert set(s) == {0, 1, 2}, f"{fid}: must carry all objects"
        assert not any(s[i]["gone"] for i in s), f"{fid}: nothing may be gone yet"
        assert s[0]["y_rear"] == 200.0 and s[1]["y_rear"] == 80.0

    # first push: rear-most (index 0) moves, the front object only after
    # contact (gap 70mm > travel/3), other lane stays static
    s1 = states("push1_1")
    assert s1[0]["y_rear"] < 200.0, "pushed object must move"
    assert s1[1]["y_rear"] == 80.0, "front object not yet contacted (gap 70)"
    assert s1[2]["y_rear"] == 160.0, "object in another lane must not move"
    s2 = states("push1_2")
    assert s2[1]["y_rear"] < 80.0, "front object must follow after contact"
    sf = states("push1")
    assert abs(sf[0]["y_rear"] - kf["push_end_y"]) < 0.01, "pushed object at the edge"
    assert sf[0]["gone"] is False, "pushed object is drawn at the edge in the push frame"
    assert sf[1]["gone"] is True, "cascaded object fell off with this push"

    # later cycles: both lane objects are gone, toolhead still does the
    # (wasted) cycle that the generated gcode contains
    s3 = states("xcycle2")
    assert s3[0]["gone"] and s3[1]["gone"] and not s3[2]["gone"]
    done = states("done_all")
    assert all(done[i]["gone"] for i in done)


def test_keyframes_multi_object_lift_between():
    """Multi-object keyframes: lift+repark+descend frames between push cycles."""
    objects = [
        {"id": 100, "x_min": 100.0, "x_max": 150.0, "y_min": 105.0, "y_max": 115.0, "moves": 10},
        {"id": 200, "x_min": 200.0, "x_max": 220.0, "y_min": 150.0, "y_max": 200.0, "moves": 10},
    ]
    pr = get_printer("A1")
    kf = ejection_keyframes(
        max_z_height=30.0, t_lift=5.0, push_z=0.4,
        printer=pr, objects=objects,
    )
    frame_ids = [f["id"] for f in kf["frames"]]
    # cycle 1: xcycle1, lower1, cool1, wait1, push1, lift1, repark1
    # cycle 2: xcycle2, lower2, cool2, wait2, push2
    # done_all
    assert "lift1" in frame_ids, "lift frame between objects missing"
    assert "repark1" in frame_ids, "repark frame between objects missing"
    assert "lower2" in frame_ids, "descend frame of cycle 2 missing"
    # lift frame must be at safe_z
    lift_frame = next(f for f in kf["frames"] if f["id"] == "lift1")
    assert abs(lift_frame["toolhead"]["z"] - kf["safe_z"]) < 0.01
    # repark frame must be at safe_z
    repark_frame = next(f for f in kf["frames"] if f["id"] == "repark1")
    assert abs(repark_frame["toolhead"]["z"] - kf["safe_z"]) < 0.01
    # X travel of cycle 2 at safe_z, then descend to push_z
    x2 = next(f for f in kf["frames"] if f["id"] == "xcycle2")
    assert abs(x2["toolhead"]["z"] - kf["safe_z"]) < 0.01
    descend_frame = next(f for f in kf["frames"] if f["id"] == "lower2")
    assert abs(descend_frame["toolhead"]["z"] - 0.4) < 0.01


def test_keyframes_cool_wait_per_object():
    """V2: cool + wait happen EVERY object cycle (not once)."""
    objects = [
        {"id": 1, "x_min": 10.0, "x_max": 50.0, "y_min": 100.0, "y_max": 200.0, "moves": 10},
        {"id": 2, "x_min": 100.0, "x_max": 150.0, "y_min": 100.0, "y_max": 180.0, "moves": 10},
        {"id": 3, "x_min": 200.0, "x_max": 240.0, "y_min": 100.0, "y_max": 160.0, "moves": 10},
    ]
    pr = get_printer("A1")
    kf = ejection_keyframes(
        max_z_height=30.0, cool_temp_bed=45, safety_wait_s=60,
        printer=pr, objects=objects,
    )
    cool_frames = [f for f in kf["frames"] if f["id"].startswith("cool")]
    wait_frames = [f for f in kf["frames"] if f["id"].startswith("wait")]
    assert len(cool_frames) == 3, "must cool EVERY object cycle"
    assert len(wait_frames) == 3, "must wait EVERY object cycle"
    # all cool/wait frames have wait=True
    assert all(f.get("wait") is True for f in cool_frames)
    assert all(f.get("wait") is True for f in wait_frames)


def test_keyframes_obj_index_matches_bbox_list():
    """obj_index maps to the id-sorted bbox list; 'pushed' tracks ejected ones.

    Dynamic selection can push a later-listed object first - the UI indexes
    objects_bboxes by obj_index, so that must stay the list index, not the
    push order.
    """
    objects = [
        {"id": 1, "x_min": 10.0, "x_max": 50.0, "y_min": 100.0, "y_max": 180.0, "moves": 10},
        {"id": 2, "x_min": 200.0, "x_max": 240.0, "y_min": 100.0, "y_max": 200.0, "moves": 10},
    ]
    pr = get_printer("A1")
    kf = ejection_keyframes(max_z_height=30.0, printer=pr, objects=objects)
    # abstand: obj1 = 262-180 = 82, obj2 = 262-200 = 62 -> obj2 pushed first
    xcycle1 = next(f for f in kf["frames"] if f["id"] == "xcycle1")
    assert xcycle1["obj_index"] == 1, "obj_index must be the bbox-list index (id 2 = index 1)"
    assert xcycle1["toolhead"]["x"] == 220.0, "first cycle must target obj2's center"
    assert xcycle1["pushed"] == []
    push1 = next(f for f in kf["frames"] if f["id"] == "push1")
    assert push1["obj_index"] == 1 and push1["pushed"] == [1]
    xcycle2 = next(f for f in kf["frames"] if f["id"] == "xcycle2")
    assert xcycle2["obj_index"] == 0 and xcycle2["pushed"] == [1]
    done_all = next(f for f in kf["frames"] if f["id"] == "done_all")
    assert sorted(done_all["pushed"]) == [0, 1]


def test_purge_strip_skipped_in_copies():
    """Copies 2+ skip the front-of-plate purge/cali block."""
    gc_p = _gcode(_make_3mf(30.0, with_purge=True))
    assert "MACHINE_START_GCODE_END" in gc_p
    assert "G0 X128 E10" in gc_p

    lines_p = gc_p.splitlines(keepends=True)
    es, ee = find_block_indices(lines_p)
    block_p = lines_p[es:ee + 1]
    pre_p, post_p = split_start_block(block_p)
    assert pre_p and post_p
    zones = find_purge_zones(block_p)
    assert zones
    pf = build_purge_free_start(block_p)
    assert pf is not None
    pf_txt = "".join(pf)
    assert "G0 X128 E10" not in pf_txt, "cali purge must be stripped"
    assert "G28" in pf_txt, "heat/home must STAY in purge-free start"

    new_gc, notes = build_multi_plate_sequence(
        {1: gc_p}, {1: 3},
        t_lift=5.0, push_z=0.4, cool_temp_bed=45, safety_wait_s=60,
        eject_enabled=True, min_height_mm=0, plate_max_z={1: 30.0},
    )
    count_purge = sum(1 for ln in new_gc.splitlines() if "G0 X128 E10" in ln)
    assert count_purge == 1, f"cali purge must appear only in copy 1: found {count_purge}"
    count_model = sum(1 for ln in new_gc.splitlines() if ln.strip().startswith("G1 X150 Y150 E3.0"))
    assert count_model == 3, f"model print must be in every copy: found {count_model}"
    assert any("purge/calibration strip" in n for n in notes)


def test_multi_object_per_object_pushes():
    """Per-OBJECT_ID bboxes → per-object push cycles in snippet + keyframes."""
    gc_p = _gcode(_make_3mf(30.0, with_purge=True))
    gc_multi = gc_p.replace(
        "G1 X100 Y100 E1.0 F6000",
        "; OBJECT_ID: 100\nG1 X100 Y105 E1.0 F6000\n; OBJECT_ID: 200\nG0 X200 Y200 F6000\nG1 X200 Y200 E2.0 F6000"
    ).replace(
        "G1 X150 Y100 E2.0 F6000",
        "; OBJECT_ID: 100\nG1 X150 Y115 E2.0 F6000"
    ).replace(
        "G1 X150 Y150 E3.0 F6000",
        "; OBJECT_ID: 200\nG1 X220 Y150 E3.0 F6000"
    )
    objs_m = parse_object_bboxes(gc_multi)
    ids = [o["id"] for o in objs_m]
    assert ids == [100, 200]
    o100 = next(o for o in objs_m if o["id"] == 100)
    o200 = next(o for o in objs_m if o["id"] == 200)
    assert o100["x_min"] == 100.0 and o100["x_max"] == 150.0 and o100["y_min"] == 105.0
    assert o200["x_min"] == 200.0 and o200["x_max"] == 220.0 and o200["y_max"] == 200.0

    pr = get_printer("A1")
    snip_m = build_ejection_snippet(
        max_z_height=30.0, printer=pr, objects=objs_m,
    )
    assert "2 object(s)" in snip_m
    # V2: cooldown EVERY cycle
    assert snip_m.count("M190 S30") == 2, f"cooldown must happen every cycle (V2): {snip_m.count('M190 S30')}"
    assert "object 1/2" in snip_m and "object 2/2" in snip_m
    # collision safety: Y must NEVER increase at push_z - every re-park
    # (between objects AND the final one) requires a prior lift to safe_z.
    li_m = snip_m.splitlines()
    z = 20.0
    y_pos = pr["kinematics"]["rear_command_limit"]
    for ln in li_m:
        if ln.startswith("G1"):
            if " Z" in ln:
                z = float(ln.split(" Z")[1].split()[0])
            if " Y" in ln:
                y_new = float(ln.split(" Y")[1].split()[0])
                if z < 5.0:
                    assert y_new <= y_pos + 0.01, \
                        f"Y increased at push height without lift: {y_pos} -> {y_new}"
                    y_pos = y_new
                else:
                    y_pos = y_new


def test_printer_db_valid():
    """All printer models are valid against the schema v5 vocabulary."""
    for name, p in PRINTERS.items():
        machine, kin, th = p["machine"], p["kinematics"], p["toolhead"]
        printable = machine["printable"]
        assert printable["x"] > 0 and printable["y"] > 0, f"{name}: bad bed"
        assert printable["z"] > 0, f"{name}: missing max height"
        assert machine["structure"] in STRUCTURES, f"{name}: unknown structure"
        assert p.get("vendor") in VENDORS, f"{name}: unknown vendor"
        src = kin["rear_command_limit_source"]
        assert src in PROVENANCE_LEVELS, f"{name}: unknown park provenance"
        if src == "no_park":
            # no rear park zone: the pusher starts at the printable rear edge
            assert kin["rear_command_limit"] == printable["y"], name
        else:
            assert kin["rear_command_limit"] > printable["y"], \
                f"{name}: rear limit must exceed bed depth"
        assert th["clearance_radius"] > 0 and th["body_height"] > 0, f"{name}: bad toolhead geometry"
        assert th["dist_to_rod"] > 0, f"{name}: missing extruder-to-rod distance"
        assert th["footprint"]["estimated"] is True, f"{name}: footprint must be flagged as estimate"
        assert p["suitability"] in SUITABILITY_LEVELS, f"{name}: unknown suitability"
    # A1 alias resolution
    a1 = get_printer("N2S")
    assert a1["machine"]["printable"]["x"] == 256.0
    assert a1["toolhead"]["body_height"] == 4.76
    assert a1["machine"]["printable"]["z"] == 256.0
    assert a1["kinematics"]["rear_command_limit"] == 262.5


def test_printer_vendors_and_aliases():
    """Vendors group the DB; slicer model ids and full names resolve to keys."""
    assert {"bambu", "prusa", "creality", "elegoo", "anycubic", "qidi",
            "sovol", "voron"} <= set(VENDORS)
    for vid, vendor in VENDORS.items():
        assert vendor["label"] and vendor["prefixes"] and vendor["order"], vid
        assert any(p["vendor"] == vid for p in PRINTERS.values()), f"{vid}: empty"
    assert len(PRINTERS) == 45

    assert get_printer("Creality-K1")["name"] == "K1"
    assert get_printer("Elegoo-CC")["name"] == "Centauri Carbon"
    assert get_printer("Qidi-Q1Pro")["name"] == "Q1 Pro"
    assert get_printer("MK4S")["vendor"] == "prusa"
    assert is_known_printer("Prusa MK4S")
    assert is_known_printer("Bambu Lab A1")
    assert not is_known_printer("Unknown Maker 9000")

    assert resolve_display_name("Creality-K1") == "K1"
    assert resolve_display_name("", "Prusa MK4S") == "MK4S"
    assert resolve_display_name("", "Bambu Lab X1 Carbon") == "X1C"
    assert resolve_display_name("Prusa XL") == "XL"
    assert resolve_display_name("Qidi-Q1Pro", "Qidi Q1 Pro") == "Q1 Pro"


def test_non_bambu_ejection_uses_rear_bed_edge():
    """no_park models start the push at the bed's rear edge, not behind it."""
    prusa = get_printer("MK4S")
    g = build_ejection_snippet(
        30.0,
        printer=prusa,
        objects=[{"x_min": 100.0, "x_max": 150.0, "y_min": 50.0, "y_max": 100.0}],
    )
    assert "G1 Y210 " in g
    assert "G1 Y0 " in g
    assert "step 3: rear bed edge (no park zone" in g
    assert "step 11: rear bed edge (final, no park zone)" in g


def test_orca_prusa_file_builds_without_bambu_markers():
    """Non-Bambu Orca exports have no EXECUTABLE_BLOCK markers - fallback mode."""
    src = zipfile.ZipFile(io.BytesIO(make_multi_plate_3mf()))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        for name in src.namelist():
            body = src.read(name)
            if name.endswith(".gcode"):
                body = b"\n".join(
                    line for line in body.splitlines()
                    if b"EXECUTABLE_BLOCK" not in line
                    and b"MACHINE_START_GCODE_END" not in line
                )
            if name.endswith("slice_info.config"):
                body = body.replace(b"N2S", b"MK4S")
            out.writestr(name, body)

    gcodes = extract_plate_gcodes(io.BytesIO(buf.getvalue()))
    g, notes = build_multi_plate_sequence(
        gcodes, {1: 2}, eject_enabled=True, printer=get_printer("MK4S")
    )
    assert g.count("plateloop auto-ejection") == 2
    assert "step 3: rear bed edge (no park zone" in g
    assert "re-heat for next print" in g
    assert any("fallback append mode" in n for n in notes), notes


def test_printer_db_corrected_values():
    """Regression against the re-fetched BambuStudio/OrcaSlicer profiles.

    Catches silent data drift: these values were corrected on 2026-10-01
    (see PRINTER_DB_SOURCES.md).
    """
    a1 = get_printer("N2S")
    assert a1["machine"]["bed_exclude"] == {"x": 0.0, "y": 0.0}, "A1 has no bed exclusion"
    assert a1["kinematics"]["rear_command_limit_source"] == "verified_gcode"
    # X1/P1 family: 18x28 exclusion, park at 265, profile Z limit 250
    for model in ("X1C", "X1", "X1E", "P1P", "P1S"):
        p = get_printer(model)
        assert p["machine"]["bed_exclude"] == {"x": 18.0, "y": 28.0}, model
        assert p["kinematics"]["rear_command_limit"] == 265.0, model
        assert p["kinematics"]["rear_command_limit_source"] == "end_gcode_park", model
        assert p["machine"]["printable"]["z"] == 250.0, model
        assert p["toolhead"]["dist_to_rod"] == 33.0, model
    p2s = get_printer("N7")
    assert p2s["toolhead"]["clearance_radius"] == 72.0, "BambuStudio says 72 (Orca still 68)"
    assert p2s["machine"]["bed_exclude"] == {"x": 0.0, "y": 0.0}
    x2d = get_printer("N6")
    assert x2d["toolhead"]["body_height"] == 4.0 and x2d["toolhead"]["dist_to_rod"] == 33.0
    for model in ("H2S", "H2D", "H2D Pro", "H2C"):
        assert get_printer(model)["toolhead"]["body_height"] == 4.0, model
        assert get_printer(model)["kinematics"]["rear_command_limit_source"] == "estimate", model


def test_short_object_detection():
    """Short object height parsed correctly."""
    short = _make_3mf(5.0)
    info = parse_3mf(io.BytesIO(short), "short.gcode.3mf")
    assert abs(info.min_object_height - 5.0) < 0.01
    assert abs(info.max_z_height - 5.0) < 0.01


def test_project_3mf_maps_objects_to_own_geometry():
    """Unsliced project: each object gets ITS part's dimensions (not the tallest)."""
    info = parse_3mf(io.BytesIO(make_project_3mf()), "project.3mf")
    assert not info.is_gcode_3mf
    assert [p.index for p in info.plates] == [1, 2]
    p1 = info.plates[0]
    assert [o.name for o in p1.objects] == ["Short.stl", "Tall.stl", "Inline.stl"]
    assert [o.height_mm for o in p1.objects] == [30.0, 60.0, 15.0]
    assert [o.width_mm for o in p1.objects] == [10.0, 40.0, 5.0]
    assert [o.depth_mm for o in p1.objects] == [20.0, 50.0, 5.0]
    p2 = info.plates[1]
    assert [(o.name, o.height_mm) for o in p2.objects] == [("Tall.stl", 60.0)]
    # min across plates: plate 1 contains the 15 mm object
    assert abs(info.min_object_height - 15.0) < 0.01
    assert not info.warnings, f"no warnings expected, got {info.warnings}"


# ===== multi-plate =====


def test_parse_multi_plate_stats():
    """Both plates parsed with their own stats, temps and filaments."""
    info = parse_3mf(io.BytesIO(make_multi_plate_3mf()), "multi.gcode.3mf")
    assert [p.index for p in info.plates] == [1, 2]
    assert abs(info.plates[0].max_z_height - 30.0) < 0.01
    assert abs(info.plates[1].max_z_height - 54.0) < 0.01
    assert abs(info.plates[0].filament_weight_g - 33.55) < 0.01
    assert abs(info.plates[1].filament_weight_g - 42.92) < 0.01
    assert info.plates[0].first_layer_bed_temp == 65
    assert info.plates[1].first_layer_bed_temp == 60
    assert [f["type"] for f in info.plates[0].filaments] == ["PLA"]
    assert [f["type"] for f in info.plates[1].filaments] == ["PETG"]
    assert info.object_names == {"11": "Plate1.stl", "22": "Plate2.stl"}
    assert info.layer_count == 300  # 150 per plate
    assert info.max_z_height == 54.0  # tallest plate
    assert info.gcode_size > 0


def test_extract_plate_gcodes():
    gcodes = extract_plate_gcodes(io.BytesIO(make_multi_plate_3mf()))
    assert sorted(gcodes) == [1, 2]
    assert "OBJECT_ID: 11" in gcodes[1]
    assert "OBJECT_ID: 22" in gcodes[2]


def _multi_pieces():
    gcodes = extract_plate_gcodes(io.BytesIO(make_multi_plate_3mf()))
    pr = get_printer("A1")
    limit = pr["machine"]["printable"]["y"] + 8.0
    objs = {i: parse_object_bboxes(g, limit) for i, g in gcodes.items()}
    return gcodes, pr, objs


def test_multi_plate_sequence_purge_once_and_order():
    """Jobs: plate1 x2, plate2 x1 - purge once, per-plate safe_z/push/reheat."""
    gcodes, pr, objs = _multi_pieces()
    assert [o["id"] for o in objs[1]] == [11]
    assert [o["id"] for o in objs[2]] == [22]
    new_gc, notes = build_multi_plate_sequence(
        gcodes, {1: 2, 2: 1},
        t_lift=5.0, push_z=0.4, cool_temp_bed=45, safety_wait_s=60,
        printer=pr, plate_objects=objs, plate_max_z={1: 30.0, 2: 54.0},
    )
    ls = new_gc.splitlines()
    starts = sum(1 for ln in ls if ln.strip() == "; EXECUTABLE_BLOCK_START")
    ends = sum(1 for ln in ls if ln.strip() == "; EXECUTABLE_BLOCK_END")
    assert starts == ends == 3
    assert sum(1 for ln in ls if ln.strip().startswith("M140 S0")) == 1
    assert new_gc.count("auto-ejection (generated)") == 3
    # purge/calibration strip exactly ONCE (first job = plate 1)
    assert sum(1 for ln in ls if "G0 X128 E10" in ln) == 1
    assert "safe_z=35" in new_gc  # plate 1 ejection
    assert "safe_z=59" in new_gc  # plate 2 ejection
    # push targets = per-plate object centers (plate1 100-150, plate2 60-110)
    step7 = [ln for ln in ls if "step 7" in ln]
    xs = [float(re.search(r"X([\d.]+)", ln).group(1)) for ln in step7]
    assert xs == [125.0, 125.0, 85.0]
    # reheat before jobs 2+ uses the TARGET plate temps
    assert new_gc.count("M190 S65") == 1  # before plate1 print 2
    assert new_gc.count("M190 S60") == 1  # before plate2 print 1
    assert "M109 S230" in new_gc  # plate 2 nozzle temp
    # job markers in order
    assert new_gc.index("(plate 1, print 2 of 2)") < new_gc.index("(plate 2, print 1 of 1)")
    assert any("purge/calibration strip" in n for n in notes)


def test_multi_plate_sequence_skip_plate():
    """prints=0 skips a plate; the first selected plate provides the header."""
    gcodes, pr, objs = _multi_pieces()
    new_gc, _ = build_multi_plate_sequence(
        gcodes, {1: 0, 2: 2},
        t_lift=5.0, printer=pr, plate_objects=objs, plate_max_z={1: 30.0, 2: 54.0},
    )
    ls = new_gc.splitlines()
    assert sum(1 for ln in ls if "step 7" in ln) == 2
    assert all("X85" in ln for ln in ls if "step 7" in ln)
    assert "safe_z=59" in new_gc and "safe_z=35" not in new_gc
    # purge strip still exactly once (plate 2 is the first selected job)
    assert sum(1 for ln in ls if "G0 X128 E10" in ln) == 1


def test_multi_plate_sequence_no_plates():
    gcodes = extract_plate_gcodes(io.BytesIO(make_multi_plate_3mf()))
    out, notes = build_multi_plate_sequence(gcodes, {1: 0, 2: 0})
    assert out == ""
    assert notes


def test_repack_single_plate_trim():
    """Output collapses to one plate: other assets/config entries removed."""
    import hashlib

    data = make_multi_plate_3mf()
    out = repack_single_plate_3mf(
        io.BytesIO(data), "; test gcode\n", 1,
        {"prediction_sec": 1234.0, "weight_g": 76.47,
         "per_filament": {("PLA", "#D5B6A4"): {"used_g": 76.47, "used_m": 25.64}}},
    )
    zf = zipfile.ZipFile(out)
    names = zf.namelist()
    for gone in ("Metadata/plate_2.gcode", "Metadata/plate_2.gcode.md5",
                 "Metadata/plate_2.json", "Metadata/plate_2.png",
                 "Metadata/plate_no_light_2.png", "Metadata/top_2.png",
                 "Metadata/pick_2.png"):
        assert gone not in names, f"{gone} must be removed"
    g = zf.read("Metadata/plate_1.gcode")
    assert g.decode() == "; test gcode\n"
    assert zf.read("Metadata/plate_1.gcode.md5").decode() == hashlib.md5(g).hexdigest().upper()
    ms = zf.read("Metadata/model_settings.config").decode()
    assert "plate_1" in ms and "plate_2" not in ms and 'value="2"' not in ms
    rels = zf.read("Metadata/_rels/model_settings.config.rels").decode()
    assert "plate_1.gcode" in rels and "plate_2.gcode" not in rels
    assert "plate_2" not in zf.read("Metadata/filament_sequence.json").decode()
    si = zf.read("Metadata/slice_info.config").decode()
    assert 'value="2"' not in si
    assert 'key="weight" value="76.47"' in si
    assert 'used_g="76.47"' in si and 'used_m="25.64"' in si
    assert 'prediction"' in si and 'value="1234"' in si
    # shared assets survive
    assert "Metadata/plate_no_light_1.png" in names
    assert "[Content_Types].xml" in names


def test_trim_slice_info_multi_filament():
    """Each filament keeps its own aggregated use; weight metadata is updated."""
    from app.threemf import _trim_slice_info

    xml = (
        b'<?xml version="1.0"?><config><plate>'
        b'<metadata key="index" value="1"/>'
        b'<metadata key="prediction" value="100"/>'
        b'<metadata key="weight" value="15.0"/>'
        b'<filament id="1" type="PLA" color="#FF0000" used_m="10" used_g="10.0"/>'
        b'<filament id="2" type="PETG" color="#00FF00" used_m="5" used_g="5.0"/>'
        b'</plate></config>'
    )
    out = _trim_slice_info(xml, 1, {
        "prediction_sec": 900.0,
        "weight_g": 45.0,
        "per_filament": {
            ("PLA", "#FF0000"): {"used_g": 30.0, "used_m": 30.0},
            ("PETG", "#00FF00"): {"used_g": 15.0, "used_m": 15.0},
        },
    }).decode()
    assert 'used_g="30.00"' in out and 'used_g="15.00"' in out, "per-color split lost"
    assert 'used_m="30.00"' in out and 'used_m="15.00"' in out
    assert 'key="weight" value="45.00"' in out
    assert 'key="prediction" value="900"' in out


def test_thumbnail_never_crosses_plates():
    """Thumbnail fallback must stay on the requested plate."""
    from app.threemf import extract_thumbnail

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Metadata/plate_2.png", PNG_1PX)
    data = buf.getvalue()
    assert extract_thumbnail(io.BytesIO(data), plate=2) == PNG_1PX
    assert extract_thumbnail(io.BytesIO(data), plate=1) is None


# ---- real A1 start gcode (Bambu Studio 02.09, A1 start gcode 2026-05-13) ----

_A1_START = (Path(__file__).parent / "fixtures" / "a1_start_20260513.gcode").read_text(encoding="utf-8")


def _a1_gcode() -> str:
    return (
        "; HEADER_BLOCK_START\n"
        "; max_z_height: 30.00\n"
        "; HEADER_BLOCK_END\n"
        + _A1_START
        + "; CHANGE_LAYER\n"
        "; Z_HEIGHT: 0.2\n"
        "G1 X100 Y100 F42000\n"
        "G1 Z.2\n"
        "G1 X150 Y100 E2.0 F3000\n"
        "G1 X150 Y150 E3.0 F3000\n"
        "M400\n"
        "M140 S0 ; turn off bed\n"
        "M104 S0 ; turn off hotend\n"
        "M18 X Y Z\n"
        "; EXECUTABLE_BLOCK_END\n"
    )


def _job_start(gcode: str, job_no: int) -> list:
    """Lines of one job from its start marker up to its first layer."""
    ls = gcode.splitlines()
    if job_no == 1:
        i = 0
    else:
        i = next(k for k, ln in enumerate(ls) if f"plateloop print {job_no} of" in ln)
    j = next(k for k in range(i, len(ls)) if ls[k].startswith("; CHANGE_LAYER"))
    return ls[i:j]


def test_a1_purge_free_start_keeps_cleaning_and_leveling():
    """Prints 2+ drop only the front purge line - cleaning, leveling and
    the textured-PEI offset stay (the A1 probes the bed with the nozzle)."""
    new_gc, _ = build_multi_plate_sequence(
        {1: _a1_gcode()}, {1: 2}, cool_time_s=300, min_height_mm=0,
        plate_max_z={1: 30.0},
    )
    first = "\n".join(_job_start(new_gc, 1))
    second = "\n".join(_job_start(new_gc, 2))
    assert "G0 X128 E8" in first, "copy 1 keeps the purge line"
    assert "G0 X128 E8" not in second and "G0 X128 E10" not in second, "no purge line on prints 2+"
    for kept in (";===== wipe nozzle ====", ";===== brush material wipe nozzle =====",
                 "G28 Z P0 T300", ";===== auto extrude cali start", "G29 A1 ",
                 "G29.1 Z-0.02", "M1007 S1", "G29.4"):
        assert kept in second, f"{kept!r} must stay in the start of print 2"
    # nothing is extruded onto the plate before the first layer (flushes run
    # off the plate at X-48.2 / at the rear Y254 position like copy 1)
    assert not any(re.match(r"\s*G[01] .*X(1[0-9]{2}|[1-9][0-9])\.?\d* .*E\d", ln)
                   for ln in second.splitlines())


def test_a1_every_print_waits_for_print_temp_before_first_layer():
    """The last temperature command before the first layer must be a WAIT
    for the print temperature - copy 2 used to start at the 140 C probe temp."""
    new_gc, _ = build_multi_plate_sequence(
        {1: _a1_gcode()}, {1: 3}, cool_time_s=300, min_height_mm=0,
        plate_max_z={1: 30.0},
    )
    for job in (1, 2, 3):
        temps = [ln.strip() for ln in _job_start(new_gc, job)
                 if re.match(r"\s*M10[49] S\d+", ln)]
        assert temps, f"print {job}: no nozzle temperature commands"
        assert temps[-1].startswith("M109 S220"), f"print {job}: last temp command is {temps[-1]!r}"


def test_ejection_turns_hotend_and_fans_off_while_waiting():
    """While the part cools the nozzle must not idle hot over the plate."""
    new_gc, _ = build_multi_plate_sequence(
        {1: _a1_gcode()}, {1: 2}, cool_time_s=300, min_height_mm=0,
        plate_max_z={1: 30.0},
    )
    ls = new_gc.splitlines()
    begin = next(k for k, ln in enumerate(ls) if "plateloop auto-ejection (generated)" in ln)
    lift = next(k for k in range(begin, len(ls)) if "step 2: lift" in ls[k])
    cool = next(k for k in range(begin, len(ls)) if "step 8" in ls[k])
    block = ls[lift:cool]
    assert any(ln.startswith("M104 S0") for ln in block), "hotend off before the cooldown"
    assert any(ln.startswith("M106 S0") for ln in block), "part fan off before the cooldown"
    # re-heat only puts the nozzle on standby; the start gcode cleans it first
    reheat = ls[next(k for k, ln in enumerate(ls) if ln.startswith("; re-heat for next print")):][:5]
    assert "M104 S140 ; nozzle standby (no ooze before cleaning)" in reheat
    assert not any(ln.startswith("M109 S220") for ln in reheat)


def test_plate_json_bbox_is_xmin_ymin_xmax_ymax():
    """Bambu writes bbox_objects as [xmin, ymin, xmax, ymax] (real Benchy
    plate_1.json from Bambu Studio 02.09) - not [xmin, xmax, ymin, ymax]."""
    plate_json = {
        "bbox_all": [120.43747, 111.62346, 136.67176, 150.65659],
        "bbox_objects": [{"area": 437.26, "bbox": [120.43747, 111.62346, 136.67176, 150.65659],
                          "id": 390, "layer_height": 0.25, "name": "3DBenchy.stl"}],
        "bed_type": "textured_plate",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Metadata/plate_1.json", json.dumps(plate_json))
        zf.writestr("Metadata/plate_1.gcode", "; max_z_height: 48.00\nG1 X1 Y1\n")
    info = parse_3mf(io.BytesIO(buf.getvalue()), "benchy.gcode.3mf")
    obj = info.plates[0].objects[0]
    assert (obj.x_min, obj.y_min, obj.x_max, obj.y_max) == (120.44, 111.62, 136.67, 150.66)
    assert obj.width_mm == 16.23 and obj.depth_mm == 39.03


# ---- ejection safety (review 2026-11-20) ----

def _two(o1, o2):
    return [{"id": 1, "moves": 10, **o1}, {"id": 2, "moves": 10, **o2}]


def test_safe_z_is_capped_at_the_printer_z_limit():
    """A1 start turns soft endstops off: Z above 256 would hit the hard stop."""
    from app.gcode import EjectionUnsafe

    pr = get_printer("A1")
    snip = build_ejection_snippet(max_z_height=250.0, t_lift=10.0, printer=pr)
    zs = [float(z) for z in re.findall(r"^G1 Z([\d.]+)", snip, re.MULTILINE)]
    assert max(zs) == 256.0, zs
    with pytest.raises(EjectionUnsafe):
        build_ejection_snippet(max_z_height=255.0, t_lift=5.0, printer=pr)


def test_x_travel_at_safe_height_and_brush_zone_avoided():
    """The nozzle never moves sideways at push_z along the rear edge; in the
    A1 wiper-brush zone it descends in front of the brush."""
    pr = get_printer("A1")
    obj = [{"id": 1, "x_min": 25.0, "x_max": 55.0, "y_min": 100.0, "y_max": 150.0, "moves": 10}]
    li = build_ejection_snippet(max_z_height=30.0, printer=pr, objects=obj).splitlines()
    x = next(i for i, ln in enumerate(li) if "step 7" in ln)
    assert li[x].startswith("G1 X40 ")
    assert li[x + 1].startswith("G1 Y258 ") and "step 4b" in li[x + 1]
    assert li[x + 2].startswith("G1 Z0.4 ")
    # no move at push height before the toolhead is above the object
    assert not any(ln.startswith("G1 Z0.4") for ln in li[:x])
    # outside the brush zone: descend at the normal park position
    obj2 = [{"id": 1, "x_min": 110.0, "x_max": 150.0, "y_min": 100.0, "y_max": 150.0, "moves": 10}]
    li2 = build_ejection_snippet(max_z_height=30.0, printer=pr, objects=obj2).splitlines()
    assert not any("step 4b" in ln for ln in li2)


def test_timed_cooldown_never_heats_the_bed():
    """Fixed-time mode switches the bed heater off; the (hidden) ejection
    temperature field must not become a heating target."""
    snip = build_ejection_snippet(max_z_height=30.0, cool_temp_bed=70, cool_time_s=600)
    assert "M140 S0 ; step 8: bed heater off while cooling" in snip
    assert "M140 S70" not in snip and "M190" not in snip


def test_clearance_single_object_is_fine():
    from app.gcode import ejection_clearance_problems

    obj = [{"id": 1, "x_min": 113.3, "x_max": 143.8, "y_min": 95.1, "y_max": 154.5,
            "z_max": 48.0, "moves": 10}]
    assert ejection_clearance_problems(obj, get_printer("A1"), 48.0) == []


def test_clearance_gantry_hits_part_outside_the_push_path():
    """Two Benchys side by side: pushing one sweeps the X gantry (25 mm above
    the nozzle on the A1) over the other one."""
    from app.gcode import ejection_clearance_problems

    objs = _two({"x_min": 60.0, "x_max": 90.5, "y_min": 95.0, "y_max": 154.5, "z_max": 48.0},
                {"x_min": 160.0, "x_max": 190.5, "y_min": 95.0, "y_max": 154.5, "z_max": 48.0})
    probs = ejection_clearance_problems(objs, get_printer("A1"), 48.0)
    assert any("X gantry" in p for p in probs), probs
    # flat parts pass under the gantry
    flat = _two({"x_min": 60.0, "x_max": 90.5, "y_min": 95.0, "y_max": 154.5, "z_max": 15.0},
                {"x_min": 160.0, "x_max": 190.5, "y_min": 95.0, "y_max": 154.5, "z_max": 15.0})
    assert ejection_clearance_problems(flat, get_printer("A1"), 15.0) == []


def test_clearance_rear_gap_too_small():
    """rutsche1 plate 3: part reaches to Y 254.7 - the toolhead would descend
    onto it at the park position Y 262.5."""
    from app.gcode import ejection_clearance_problems

    objs = _two({"x_min": 51.5, "x_max": 251.0, "y_min": 160.0, "y_max": 254.73, "z_max": 250.0},
                {"x_min": 4.9, "x_max": 96.0, "y_min": 1.2, "y_max": 92.4, "z_max": 200.0})
    probs = ejection_clearance_problems(objs, get_printer("A1"), 250.0)
    assert any("descend onto it" in p for p in probs), probs


def test_push_lane_conflict_uses_the_pushed_parts_width():
    """A wide rear part shoves a front part that is outside the toolhead lane
    but inside the part's own X span (rutsche1 plate 3)."""
    from app.gcode_bbox import find_push_lane_conflicts

    objs = _two({"x_min": 51.5, "x_max": 251.0, "y_min": 160.0, "y_max": 250.0},
                {"x_min": 4.9, "x_max": 96.0, "y_min": 1.2, "y_max": 92.4})
    assert len(find_push_lane_conflicts(objs, 18.0)) == 1


def test_nozzle_temp_without_prepare_comment():
    """The print temperature must not depend on the '; prepare to print'
    comment - without it copy 2 used to start at the 140 C probe temp."""
    gc = _a1_gcode().replace("M104 S220 ; prepare to print", "M104 S220")
    assert parse_print_temps(gc) == (65, 220)
    new_gc, _ = build_multi_plate_sequence({1: gc}, {1: 2}, cool_time_s=300,
                                           min_height_mm=0, plate_max_z={1: 30.0})
    temps = [ln.strip() for ln in _job_start(new_gc, 2) if re.match(r"\s*M10[49] S\d+", ln)]
    assert temps[-1].startswith("M109 S220"), temps[-3:]


def test_nozzle_temp_from_slicer_config():
    gc = "; nozzle_temperature_initial_layer = 235,250\nM104 S140\nM140 S60\n"
    assert parse_print_temps(gc) == (60, 235)


def test_unknown_nozzle_temp_refuses_to_build():
    from app.gcode import EjectionUnsafe

    gc = _a1_gcode().replace("M104 S220 ; prepare to print", "")
    gc = re.sub(r"^M10([49]) S220.*$", r"M10\1 S140", gc, flags=re.MULTILINE)
    with pytest.raises(EjectionUnsafe):
        build_multi_plate_sequence({1: gc}, {1: 2}, min_height_mm=0, plate_max_z={1: 30.0})


def test_parse_print_temps_no_quadratic_backtracking():
    import time

    gc = "M104 S1" + " " * 200_000 + "x\n"
    t0 = time.perf_counter()
    parse_print_temps(gc)
    assert time.perf_counter() - t0 < 0.5


def test_bed_temp_zero_does_not_split_the_print():
    """'M140 S0' in the START gcode (bed temp 0) is not the native shutdown:
    every copy must still contain the whole print."""
    gc = _a1_gcode()
    gc = gc.replace("M140 S65", "M140 S0").replace("M190 S65", "M190 S0")
    gc = gc.replace("M140 S0 ; turn off bed", "; MACHINE_END_GCODE_START\nM140 S0 ; turn off bed")
    new_gc, _ = build_multi_plate_sequence({1: gc}, {1: 3}, cool_time_s=300,
                                           min_height_mm=0, plate_max_z={1: 30.0})
    ls = new_gc.splitlines()
    assert sum(1 for ln in ls if ln.startswith("; CHANGE_LAYER")) == 3
    assert sum(1 for ln in ls if ln.startswith("M140 S0 ; turn off bed")) == 1
    # the first ejection comes after the first print, not right after the start
    first_eject = next(i for i, ln in enumerate(ls) if "auto-ejection (generated)" in ln)
    first_layer = next(i for i, ln in enumerate(ls) if ln.startswith("; CHANGE_LAYER"))
    assert first_layer < first_eject
