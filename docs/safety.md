# Safety

> [!WARNING]
> **Never leave the printer unattended on the first full cycle.**
> PlateLoop changes the motion commands in the generated file. Watch the
> first complete print → eject → re-heat cycle for every new model or material.

- Point a container at the front of the printer; the part lands there.
- Parts must be tall enough for the toolhead to catch them. The default
  minimum is 7 mm; short parts can jam the toolhead. The check uses the
  lowest object on each plate, measured from the gcode.
- Objects sharing one push lane (overlapping X range) can cascade off together
  or jam. The app warns about these layouts before building.
- Plates the ejection cannot clear safely are refused: parts that the X
  gantry would sweep over during a push (on the A1 anything taller than about
  22 mm next to the pushed part), parts too close to the rear park position,
  and parts within 2 mm of the printer's Z limit. Plates with one part, or
  with all parts in one push lane, are not affected.
- Push behavior is **verified on the Bambu Lab A1**. A1 mini and A2L share the
  same mechanics and are marked *expected*. CoreXY models (P1, P2, X1, X2 and
  H2 series) move the toolhead instead of the bed — plausible, but needs a
  test print.
- Prusa, Creality, Elegoo, Anycubic, QIDI, Sovol and Voron models have **no
  rear park zone** in their native end gcode: the pusher starts at the rear
  bed edge, so keep the rear-most part at least ~35 mm away from it. These
  models are all marked *needs a test print*.
- The default cooldown is a fixed time (45 min) and never stalls; check that
  your parts release after it. In *Until temperature* mode the cooldown uses
  `M190 S<temp>`: Bambu firmware waits until the bed has cooled down, but on
  other firmware (Marlin-based in particular) `M190 S` may only wait while
  heating, so the push could start on a warm bed. Check this during the first
  cycle of a *needs a test print* model.
- Verify plate adhesion, container placement and the first cycle for every
  material you use.

## Before an unattended run

- Run one supervised cycle with the same model, material and plate.
- For a [print window](print-window.md), start the file at the planned time
  and test the waits once with a short window.
- Keep enough filament on the spool for the whole run, or use matching
  backup spools with AMS filament backup.

---

[← Back to README](../README.md) · [Installation](installation.md) · [Usage](usage.md) · [Configuration](configuration.md) · [Print window](print-window.md) · [Printers](printers.md) · [Safety](safety.md) · [Troubleshooting](troubleshooting.md) · [Development](development.md)
