# Usage

## From slicer to printer

1. Slice your part in **Bambu Studio** (Bambu Lab) or **OrcaSlicer** (Bambu
   Lab *and* the other supported vendors). The part must be at least 7 mm
   tall so the toolhead can catch it.
2. Export **File → Export → Plate as .gcode.3mf** — or **All plates as
   .gcode.3mf** for a multi-plate run.
3. Upload the file. Each plate shows print time, filament, layers and its
   **lowest object** — the part that decides whether the push works. It turns
   red when it is below the minimum height.
4. Set the prints per plate with **−/+**. The time and filament for that plate
   update next to it.
5. Adjust the [settings](configuration.md) if needed — values outside the
   allowed range are marked red and block the build. Optionally limit the run
   to a [print window](print-window.md).
6. Check the **ejection preview** on the right. It follows your settings.
7. **Build sequence file**, download the `*_x<N>.gcode.3mf` and print it like
   any other file.

<p align="center">
  <img src="assets/plate-contents.png" alt="Plate summary for a 3DBenchy: render, print time, filament, layers, object height and the copies stepper" width="720">
</p>
<p align="center">
  <sub>Each plate at a glance. The copies stepper shows what the run will cost in time and filament.</sub>
</p>

<p align="center">
  <img src="assets/confirm.png" alt="Sequence confirmation dialog with plates, prints, filament, total time, schedule and ejection settings" width="800">
</p>
<p align="center">
  <sub>Every build is summarized once more before the file is generated.</sub>
</p>

## How a run works

<p align="center">
  <img src="assets/ejection.gif" alt="Animated preview of the ejection sequence" width="620">
</p>
<p align="center">
  <sub>Lift → park → cool → push → the part tips over the edge and drops. The preview uses the real object positions from your file.</sub>
</p>

```mermaid
flowchart TD
    A([Print finished]) --> B[Retract filament<br/>lift to safe Z<br/>hotend and fans off]
    B --> C[Bed fully forward<br/>toolhead parks behind plate]
    C --> D[Pick rear-most object]
    D --> E[Move X to object center]
    E --> F[Cool down: fixed time or<br/>until temperature + safety wait]
    F --> G[Push: bed retracts<br/>part slides off the plate]
    G --> H{Objects left?}
    H -- yes --> D
    H -- no --> W{Print window<br/>closed?}
    W -- yes --> X[Heaters and fans off<br/>wait for the next window]
    X --> I
    W -- no --> I[Re-heat bed<br/>nozzle standby]
    I --> J[Next copy: clean nozzle,<br/>level, heat, print]
    J --> A
```

The generated file is a normal single-plate `.gcode.3mf`: the printer's native
start and end blocks are preserved, and the ejection sequence is inserted
between copies. The motion design is documented in
[ejection logic](ejection_logic.md); the preview frames are validated against
the generated gcode in the test suite.

## Multi-plate files

Export with **All plates as .gcode.3mf** and upload it. Each plate gets its own
preview, object list and **prints** field (`0` = skip). The output is still a
single-plate `.gcode.3mf` whose gcode runs the plates back to back:

- every plate is printed as often as configured,
- every print is followed by that plate's ejection push,
- the first-print purge line runs **once** for the whole sequence,
- nozzle cleaning, homing and leveling still run before every print, and every
  print waits for the print temperature before its first layer,
- the native shutdown runs only after the very last job.

## The download

The file is named after your upload, e.g. `model_x12.gcode.3mf` for 12 total
prints, or `model_x12_start-2000.gcode.3mf` when it is built for a
[print window](print-window.md) starting at 20:00.

---

[← Back to README](../README.md) · [Installation](installation.md) · [Usage](usage.md) · [Configuration](configuration.md) · [Print window](print-window.md) · [Printers](printers.md) · [Safety](safety.md) · [Troubleshooting](troubleshooting.md) · [Development](development.md)
