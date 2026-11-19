# Printers

The printer database ([`app/printers.json`](../app/printers.json), provenance in
[printer data sources](PRINTER_DB_SOURCES.md)) covers 45 models in
8 vendor families:

| Vendor | Models | Status |
| --- | --- | --- |
| Bambu Lab | A1, A1 mini, A2L, P1P, P1S, P2S, X1, X1C, X1E, X2D, H2S, H2D, H2D Pro, H2C | A1 *verified*, A1 mini/A2L *expected*, rest *needs a test print* |
| Prusa | MK4S, MK3S, MK3.5, MINI, CORE One, XL | needs a test print |
| Creality | K1, K1C, K1 Max, K2 Plus, Ender-3 V3, Ender-3 V3 KE | needs a test print |
| Elegoo | Centauri, Centauri Carbon, Neptune 4 Pro, Neptune 4 Max | needs a test print |
| Anycubic | Kobra 3, Kobra 3 Max, Kobra S1, Kobra S1 Max | needs a test print |
| QIDI | Q1 Pro, X-Plus 4, X-Max 4, Q2 | needs a test print |
| Sovol | SV06 ACE, SV08, SV08 MAX, Zero | needs a test print |
| Voron | 2.4 350, Trident 300, 0.1 | needs a test print |

Filenames and printer-model identifiers from Bambu Studio and OrcaSlicer are
resolved automatically — you never pick a printer manually. Unknown models
fall back to A1 geometry and show a warning instead of silently assuming the
wrong values. Each model's rear Y limit is tagged `verified`, `from end gcode`,
`no park zone` or `estimated`, so you can see what is proven.

> Non-Bambu files must be sliced with **OrcaSlicer** and exported as
> `.gcode.3mf`. Native PrusaSlicer `.gcode` / `.bgcode` is not supported yet.
> The Printers page groups models by vendor and marks what still needs a test
> print on real hardware.

<p align="center">
  <img src="assets/printers.png" alt="PlateLoop printer compatibility page" width="100%">
</p>

## Help verify a printer

Only the Bambu Lab A1 is verified on real hardware today; 42 of the 45 models
are still marked *needs a test print*. If you own one of them, you can help:

1. Read the [printer test protocol](PRINTER_TEST_PROTOCOL.md).
2. Run a supervised three-copy test with a simple part.
3. Submit a [printer verification report](https://github.com/joshmadison/plateloop/issues/new?template=printer-verification.yml)
   with the details you have. Video and photos are optional; a short written
   report is enough to start the review.

Reports are reviewed before a model is marked *verified*. A failed test is
useful too: it documents the model's limits.

---

[← Back to README](../README.md) · [Installation](installation.md) · [Usage](usage.md) · [Configuration](configuration.md) · [Print window](print-window.md) · [Printers](printers.md) · [Safety](safety.md) · [Troubleshooting](troubleshooting.md) · [Development](development.md)
