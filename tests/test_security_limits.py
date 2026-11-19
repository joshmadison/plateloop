"""Guards against decompression bombs and runaway output size."""
import io
import zipfile

import pytest

from app import main as main_module
from app.threemf import _check_zip_limits, extract_plate_gcodes


def _zip_with_entries(sizes: dict[str, int]) -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, size in sizes.items():
            zf.writestr(name, b"\0" * size)
    buf.seek(0)
    return zipfile.ZipFile(buf)


def test_zip_rejects_excessive_uncompressed_size():
    with _zip_with_entries({"big.bin": 2 * 1024 * 1024}) as zf:
        with pytest.raises(ValueError, match="zip bomb"):
            _check_zip_limits(zf, max_uncompressed_mb=1)


def test_zip_rejects_too_many_entries():
    with _zip_with_entries({"a.txt": 1, "b.txt": 1, "c.txt": 1}) as zf:
        with pytest.raises(ValueError, match="entries"):
            _check_zip_limits(zf, max_entries=2)


def test_zip_accepts_normal_archive():
    with _zip_with_entries({"a.txt": 10, "b.txt": 10}) as zf:
        _check_zip_limits(zf, max_uncompressed_mb=1, max_entries=10)


def _gcode_3mf_bytes(size: int) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Metadata/plate_1.gcode", b";" * size)
    buf.seek(0)
    return buf


def test_extract_plate_gcodes_honours_env_limit(monkeypatch):
    data = _gcode_3mf_bytes(2 * 1024 * 1024)
    monkeypatch.setenv("MAX_3MF_UNCOMPRESSED_MB", "1")
    with pytest.raises(ValueError, match="1 MB limit"):
        extract_plate_gcodes(data)
    data.seek(0)
    monkeypatch.setenv("MAX_3MF_UNCOMPRESSED_MB", "3")
    assert 1 in extract_plate_gcodes(data)


def test_sequence_rejects_oversized_output(client, tall_3mf, monkeypatch):
    monkeypatch.setattr(main_module, "MAX_OUTPUT_MB", 1)
    monkeypatch.setattr(main_module, "MAX_OUTPUT_BYTES", 1)

    response = client.post(
        "/api/sequence",
        files={"file": ("m.gcode.3mf", tall_3mf, "application/octet-stream")},
        data={"plate_prints": '{"1": 5}', "min_height_mm": "7"},
    )
    assert response.status_code == 422
    assert "output" in response.json()["detail"].lower()
