"""Wall-time estimates and print-window scheduling."""
import io
import re
import zipfile
from pathlib import Path

from app import schedule
from app.schedule import job_duration_s


def test_job_duration_includes_ejection_per_object():
    # 1 h print, 2 objects, 45 min cooldown, 60 s safety wait
    first = job_duration_s(3600, 2, 2700, 60, first=True)
    assert first == 3600 + 30 + 2 * (2700 + 60 + 20)
    later = job_duration_s(3600, 2, 2700, 60, first=False)
    assert later == first + schedule.REHEAT_S


def test_job_duration_temperature_mode_uses_estimate():
    assert job_duration_s(0, 1, 0, 0, first=True) == 30 + schedule.TEMP_MODE_COOL_S + 20


def test_js_estimate_constants_match_python():
    html = Path("app/static/index.html").read_text(encoding="utf-8")
    m = re.search(r"const EST = \{([^}]*)\}", html)
    assert m, "EST constants missing in index.html"
    js = dict((k.strip(), int(v)) for k, v in (kv.split(":") for kv in m.group(1).split(",")))
    for name in ("EJECT_BASE_S", "PUSH_MOVES_S", "REHEAT_S", "TEMP_MODE_COOL_S"):
        assert js[name] == getattr(schedule, name), name


def test_sequence_prediction_includes_ejection(client, multi_3mf):
    r = client.post(
        "/api/sequence",
        files={"file": ("m.gcode.3mf", multi_3mf, "application/octet-stream")},
        data={"plate_prints": '{"1": 1, "2": 1}', "min_height_mm": "0"},
    )
    assert r.status_code == 200, r.text
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        si = zf.read("Metadata/slice_info.config").decode()
    pred = int(re.search(r'key="prediction" value="(\d+)"', si).group(1))
    st = client.post(
        "/api/stats", files={"file": ("m.gcode.3mf", multi_3mf, "application/octet-stream")}
    ).json()
    times = {p["index"]: p["print_time_sec"] for p in st["plates"]}
    # slicer time of both copies + one 45 min cooldown each + re-heat before copy 2
    assert pred >= times[1] + times[2] + 2 * 2700 + schedule.REHEAT_S
    assert pred > times[1] + times[2] + 2 * 2700


# ---------------- print-window planning ----------------

H = 3600


def _w(a: str, b: str) -> schedule.Window:
    return schedule.Window(schedule.parse_hhmm(a), schedule.parse_hhmm(b))


def test_parse_hhmm():
    assert schedule.parse_hhmm("20:00") == 20 * H
    assert schedule.parse_hhmm("7:05") == 7 * H + 5 * 60
    for bad in ("24:00", "12:60", "noon", "", "12"):
        try:
            schedule.parse_hhmm(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_window_over_midnight():
    w = _w("20:00", "07:00")
    assert w.until_close(21 * H) == 10 * H
    assert w.until_close(3 * H) == 4 * H
    assert w.until_close(8 * H) == 0
    assert w.until_open(8 * H) == 12 * H
    assert w.until_open(20 * H) == 0


def test_window_start_equals_end_rejected():
    try:
        _w("20:00", "20:00")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_plan_fills_night_then_waits_for_next_evening():
    p = schedule.plan([4 * H] * 4, 20 * H, _w("20:00", "07:00"))
    starts = [j["start_s"] for j in p["jobs"]]
    # 20:00, 00:00, 04:00 - then 08:00 is outside the window -> next 20:00
    assert starts == [0, 4 * H, 8 * H, 24 * H]
    assert [j["wait_s"] for j in p["jobs"]] == [0, 0, 0, 12 * H]
    assert p["total_s"] == 28 * H
    assert [(s["first"], s["last"]) for s in schedule.sessions(p["jobs"])] == [(1, 3), (4, 4)]


def test_plan_copy_may_run_past_window_end():
    # second copy starts 06:00 (1 h left, margin 5 % of 10 h = 30 min) and
    # runs to 08:00 although the window closes at 07:00
    p = schedule.plan([10 * H, 2 * H], 20 * H, _w("20:00", "07:00"))
    assert [j["wait_s"] for j in p["jobs"]] == [0, 0]


def test_plan_margin_skips_copy_too_close_to_window_end():
    # first copy ends 06:36: 24 min left < 5 % of 10.6 h -> wait until 20:00
    d = int(10.6 * H)
    p = schedule.plan([d, H], 20 * H, _w("20:00", "07:00"))
    assert p["jobs"][1]["wait_s"] == 24 * H - d
    assert p["jobs"][1]["start_s"] == 24 * H


def test_plan_delayed_start():
    # pressed print at 15:00, window opens 20:00 -> 5 h idle first
    p = schedule.plan([H], 15 * H, _w("20:00", "07:00"))
    assert p["jobs"][0]["wait_s"] == 5 * H


def test_plan_refuses_endless_schedule():
    try:
        schedule.plan([5 * H] * 400, 20 * H, _w("20:00", "21:00"))
    except ValueError as e:
        assert "days" in str(e)
        return
    raise AssertionError("expected ValueError")


# ---------------- gcode ----------------

def test_pause_block_turns_everything_off_and_splits_g4():
    from app.gcode import schedule_pause_block

    blk = schedule_pause_block(12 * H + 17, 4, 6, resume_at="20:00")
    for cmd in ("M400", "M104 S0", "M140 S0", "M106 S0", "M106 P2 S0", "M106 P3 S0"):
        assert any(ln.startswith(cmd + " ") or ln == cmd for ln in blk.splitlines()), cmd
    waits = [int(m) for m in re.findall(r"^G4 S(\d+)", blk, re.MULTILINE)]
    assert sum(waits) == 12 * H + 17 and max(waits) <= 90
    assert "print 4 of 6" in blk and "resumes ~20:00" in blk
    # non-Bambu printers only get the generic fan command
    other = schedule_pause_block(600, 2, 2, bambu=False)
    assert "M106 P2" not in other and "M106 S0" in other


def _post_sequence(client, data_3mf, prints=2, **form):
    return client.post(
        "/api/sequence",
        files={"file": ("m.gcode.3mf", data_3mf, "application/octet-stream")},
        data={"plate_prints": f'{{"1": {prints}}}', **form},
    )


def _gcode_of(resp) -> str:
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        return zf.read([n for n in zf.namelist() if n.endswith(".gcode")][0]).decode()


def test_sequence_without_schedule_has_no_waits(client, tall_3mf):
    r = _post_sequence(client, tall_3mf)
    assert r.status_code == 200, r.text
    assert "plateloop schedule" not in _gcode_of(r)
    assert "_start-" not in r.headers["content-disposition"]


def test_sequence_schedule_delayed_start_and_night_wait(client, tall_3mf):
    # ~1.9 h per copy, window 20:00-21:00: copy 1 at 20:00 after a 5 h delay
    # (pressed at 15:00), copy 2 the next evening
    r = _post_sequence(client, tall_3mf, schedule_start="15:00",
                       window_from="20:00", window_to="21:00")
    assert r.status_code == 200, r.text
    assert "_start-1500" in r.headers["content-disposition"]
    gc = _gcode_of(r)
    lines = gc.splitlines()
    heads = [i for i, ln in enumerate(lines)
             if ln.startswith("; ===== plateloop schedule: wait ") and "before print" in ln]
    assert len(heads) == 2
    # delayed start: inside the executable block, before any start gcode
    exec_start = lines.index("; EXECUTABLE_BLOCK_START")
    assert heads[0] == exec_start + 1
    assert "wait 5 h before print 1 of 2" in lines[heads[0]]
    # night wait: after copy 1's ejection, before copy 2's re-heat
    last_push = max(i for i, ln in enumerate(lines) if "step 10" in ln and i < heads[1])
    reheat = next(i for i, ln in enumerate(lines) if ln.startswith("; re-heat for next print"))
    assert last_push < heads[1] < reheat
    waits = [int(m) for m in re.findall(r"^G4 S(\d+)", gc, re.MULTILINE)]
    assert max(waits) <= 90


def test_sequence_schedule_rejects_bad_times(client, tall_3mf):
    for form in ({"schedule_start": "25:00"},
                 {"schedule_start": "20:00", "window_from": "20:00", "window_to": "20:00"}):
        r = _post_sequence(client, tall_3mf, **form)
        assert r.status_code == 422, form


def test_schedule_preview_endpoint(client):
    r = client.get("/api/schedule", params={
        "jobs": ",".join([str(4 * H)] * 4), "start": "20:00",
        "window_from": "20:00", "window_to": "07:00"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["finish"] == {"day": 2, "time": "00:00"}
    assert [(s["first"], s["last"]) for s in d["sessions"]] == [(1, 3), (4, 4)]
    assert d["sessions"][1]["start"] == {"day": 1, "time": "20:00"}
    assert d["waiting_s"] == 12 * H
    assert client.get("/api/schedule", params={"jobs": "x"}).status_code == 422
