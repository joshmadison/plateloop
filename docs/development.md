# Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests -q          # tests
python -m ruff check app tests     # lint
```

## Project layout

| Path | Purpose |
| --- | --- |
| `app/main.py` | FastAPI routes, upload limits, request validation, allow-listed static assets |
| `app/middleware.py` | Rate limits, build cap, security headers |
| `app/legal.py`, `app/site.py` | Legal pages from `.env`, support-link defaults |
| `app/gcode.py` | Ejection sequence, schedule waits, animation keyframes, multi-plate merge |
| `app/schedule.py` | Wall-time estimate per copy, print-window planning |
| `app/gcode_bbox.py` | Object bounding boxes, push-lane conflicts, print-end position |
| `app/threemf.py` | 3MF parsing, statistics, thumbnails, single-plate repack |
| `app/printer_db.py` | Printer database loader and model resolution |
| `app/printers.json` | Bed, kinematics and toolhead geometry per printer |
| `app/static/index.html` | Web UI and canvas ejection preview |
| `app/static/about.html` | Public “How it works” page |
| `app/static/assets/` | Shared stylesheet (`base.css`), logo, favicons, self-hosted fonts |
| `app/static/legal/` | Impressum, Datenschutz and English translations |
| `docs/` | User docs, deployment guide, ejection logic, printer test protocol |
| `tests/` | API, pipeline and schedule tests, including animation/gcode parity |

Ejection changes must keep `test_keyframes_mirror_gcode` green: the preview and
the generated gcode are validated against each other. To add a printer, extend
`app/printers.json` and document where every value comes from in
[printer data sources](PRINTER_DB_SOURCES.md).

## Contributing

Issues and pull requests are welcome. Before opening a PR:

- run `python -m pytest tests -q` and `python -m ruff check app tests`,
- keep ejection changes covered by tests — especially the gcode/animation
  parity test,
- document new printer data with its source.

---

[← Back to README](../README.md) · [Installation](installation.md) · [Usage](usage.md) · [Configuration](configuration.md) · [Print window](print-window.md) · [Printers](printers.md) · [Safety](safety.md) · [Troubleshooting](troubleshooting.md) · [Development](development.md)
