# Troubleshooting

**“This looks like a project 3MF without sliced gcode.”**
The upload contains a project, not a sliced plate. Slice it first, then use
*File → Export → Plate as .gcode.3mf*.

**“Object height … is below required 7 mm.”**
The jam guard rejected the build. Slice a taller object or lower the minimum
height — but check the first cycle closely if you do.

**“Printer … is not in the printer database.”**
The app falls back to A1 geometry so it can still build a file. Confirm the
park positions for your model before printing, or add the model to
`app/printers.json`.

**“Objects share the same push lane.”**
Their X ranges overlap, so the rear object may shove the front one along or
jam. Rearrange the plate, or watch the first cycle closely.

**“Preview unavailable for this plate.”**
The plate has no extrusion moves the app can read, so it cannot place the
objects. The build itself still works; watch the first cycle closely.

**The print window is off by some time.**
The printer has no clock: every wait counts from the moment you pressed
print. Start the file at the time it was built for (it is in the file name,
`…_start-2000.gcode.3mf`). Slicer time estimates also drift a little per copy;
over several nights the starts can move by a few minutes.

**The printer never pauses at the end of the window.**
A copy only *starts* inside the window and may run past its end, by design.
The wait comes after that copy has been pushed off.

**A setting is outlined in red.**
The value is outside the allowed range shown below the field. Building is
blocked until it is fixed.

**Upload fails with HTTP 413.**
Increase `MAX_UPLOAD_MB` (see [Installation](installation.md)); behind
Caddy also `MAX_BODY_MB` (see [Public hosting](DEPLOY.md)).

**Port 8000 is already in use.**
Start the local server with `PORT=8001 python -m app.main` (Linux/macOS) or
`$env:PORT=8001; python -m app.main` (PowerShell).

---

[← Back to README](../README.md) · [Installation](installation.md) · [Usage](usage.md) · [Configuration](configuration.md) · [Print window](print-window.md) · [Printers](printers.md) · [Safety](safety.md) · [Troubleshooting](troubleshooting.md) · [Development](development.md)
