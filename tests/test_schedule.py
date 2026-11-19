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
