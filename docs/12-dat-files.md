# Working with SoundCheck `.dat` files (Listen, Inc.)

Companion to [10-a4db-files.md](10-a4db-files.md). Same triple-deck pattern (parser service + compute adapter + Panel report); different file format. This doc is the playbook for **getting clean curve data out of a SoundCheck binary**, the version-dispatch quirks, and the visualisation flow.

The reference implementation is in [backend/app/services/soundcheck_dat.py](../backend/app/services/soundcheck_dat.py); the run-compute adapter is [scripts/compute/dat_parse/code.py](../scripts/compute/dat_parse/code.py); the curve viewer is [scripts/reports/dat_curves/code.py](../scripts/reports/dat_curves/code.py).

## TL;DR

| Topic | Rule |
| --- | --- |
| File family | `.dat` (curves), `.wfm` (waveforms), `.res` (pass/fail). All share an outer envelope. |
| Format | LabVIEW flattened cluster: **big-endian**, IEEE-754 doubles, 32-bit length-prefixed ASCII strings (no NUL terminator). |
| Versions | DAT v2 (SC 4.13), v3 (SC 5.54), v6 (SC ≥ 6.01). v6 has two on-disk variants: **with** `Fill Baseline` (SC ≥ 14) and **without** (SC 6.11). |
| Detection | Cluster type tag (16 bytes, space-padded) at byte 8 must be `Data`, `Waveform`, or `Result`. |
| LabVIEW reference | NI flattened-data convention — strings prefixed by 32-bit length, big-endian numerics, doubles for floats. |
| Output curves shape | Each `Data` cluster → one curve with `series=[X, Y, Z]` and metadata pulled out of the title (`s/n`, `Time`, `kind`). Plays nicely with the existing `python_storage` curves contract. |

---

## 1. Format envelope

```
File:
    4B u32  number of items                ← curves / waveforms / results count

Item:
    4B u32  struct_bytes                   ← does NOT include this u32 itself
    16B str cluster type, right-padded:
        "Data            " | "Waveform        " | "Result          "
    2B u16  cluster_version                 ← 2 / 3 / 6 (DAT), 3 (WFM), 1 (RES)
    1B u8   n_dims (legacy; 0)
    42B str fixed-width title               ← "PFR  s/n: 15  Time: 2023/1/11 9:42:39"
    3B      reserved zeros
    ...     version-specific fields
```

Two **separate** length encodings:

* The 42-byte fixed title is just the first 42 bytes of the human-readable label (right-padded with spaces, no length prefix).
* Then every text field after the header — the curve name itself, X/Y/Z units, Test Info, etc. — is a length-prefixed LabVIEW string (`u32` length + bytes).

The 42-byte title is followed by the same name as a length-prefixed string. They almost always match; we use the length-prefixed one for everything downstream.

---

## 2. DAT version dispatch

Documented in SoundCheck 21 manual §35.1–§35.3:

| Version | First seen | Trailing fields after units + dB refs + single-value flag |
| --- | --- | --- |
| v2  (§35.1) | SC 4.13  | — (just the single-value flag) |
| v3  (§35.2) | SC 5.54  | `protected`, `display X/Y/Z`, `Plot Color (RGBa)` |
| v6  (§35.3) | SC 6.01–7.01 | v3 fields **plus** `Plot Interp/Pt/Line/PtColor/LineWidth/BarStyle`, `Fill Baseline` (i16), `Test Info` (length-prefixed string). **v6 dropped the SI prefix strings (`Xprefix/Yprefix/Zprefix`) that v2/v3 carried.** |

The SC 6.11 manual prints a v6-shaped layout **without** `Fill Baseline`. To accept both real-world v6 sub-revisions cleanly, the parser probes:

```python
if r.remaining() >= 6:
    saved = r.pos
    baseline = r.i16()
    n = r.u32()
    if n <= r.remaining():
        # SC ≥ 14 — Fill Baseline present
        fill_baseline = baseline
        test_info_n = n
    else:
        # SC 6.11 — roll back, treat next u32 directly as Test Info length
        r.pos = saved
        test_info_n = r.u32()
```

Both branches end up with `extra_hex == ""` (the per-curve declared `struct_bytes` is exactly consumed) — that's the verifiable signal the parser landed in the right branch.

---

## 3. The "TestInfo" red herring

A naive scan for the bytes `TestInfo        ` (16-byte space-padded tag) inside a v6 file finds 7344 occurrences in our reference file — exactly equal to the curve count. It's tempting to treat them as a separate top-level item type. **They aren't.** Each curve's `Test Info` length-prefixed string happens to *contain* a flattened LabVIEW sub-cluster of type `TestInfo` (148 bytes per curve in the reference file). The manual documents the field as an opaque string; the inner cluster's schema is undocumented. The parser keeps `test_info_raw_hex` as a hex blob and lets compute scripts decode further if needed.

---

## 4. Curve title parsing

SoundCheck titles look like:

```
PFR  s/n: 15  Time: 2023/1/11 9:42:39
THD  s/n: 15  Time: 2023/1/11 9:42:40
HD2  s/n: 15  Time: 2023/1/11 9:42:40
```

Three positional fields separated by **two-space gaps**. The parser pulls them out:

| Token | Becomes metadata key | Notes |
| --- | --- | --- |
| First whitespace token | `kind` | `"Total Distortion"` is kept as a two-token literal — it's the only multi-word kind in the reference data |
| `s/n: <value>` | `s/n` | Used in the report viewer as the curve identity within a kind |
| `Time: <value>` | `Time` | Free-form timestamp string; SC writes it as `YYYY/M/D H:MM:SS` (no zero-padding) |

These keys come back as `meta.kind` / `meta.s/n` / `meta.Time` after `_flatten_and_pickle` — same shape as Drive-ingested curves files, so any existing pandas tooling works without changes.

---

## 5. Coordinate axes per curve

A SoundCheck `Data` cluster always carries three series — `X`, `Y`, `Z`. For loudspeaker tests:

* X is the sweep variable (frequency, in Hz), log-axis on display.
* Y is the measurement (Pa for SPL, Ohms for impedance, % for distortion ratios, Phons for loudness, dB for relative levels).
* Z is normally **phase in degrees** (so a complete polar response can be reconstructed from a single sweep).

The reference file's five distinct unit triples:

```
(Hz, Pa,    deg.)   ← PFR — sound pressure response, Y dB ref = 2e-5 Pa
(Hz, Ohms,  deg.)   ← ZFR — impedance magnitude + phase
(Hz, Ohms,  Q)      ← Q-of-resonance fits
(Hz, %,     deg.)   ← THD / HD2 / HD3 / TotalDistortion / TD6 / HOHD6 / HOHD10 / F0
(Hz, Phons, deg.)   ← loudness-weighted curve
```

The report viewer picks `x_axis_type="log"` when the X unit is `"Hz"` and linear otherwise.

---

## 6. End-to-end recipe

```
┌───────────────────────────────────────────────────────────────┐
│ SOURCE                                                        │
│  user uploads SoundCheck.dat → python_storage.put_file()      │
│  → snapshot_<handle>/ contains the raw .dat                   │
└──────────────────────────────┬────────────────────────────────┘
                               │ args = {"snapshot": "py_xxx"}
                               ▼
┌───────────────────────────────────────────────────────────────┐
│ COMPUTE  dat_parse                                            │
│  1. find_dat_in_dir(snap_dir) — magic check + ext probe       │
│  2. parse_dat_file(path) — version-aware walk                 │
│  3. dat_to_curves_body(parsed) — canonical curves shape       │
│  4. write into snap_dir:                                      │
│     • dat_summary.json   — kinds/serials/versions             │
│     • dat_curves.json    — full {curves:[…]} body             │
│     • curves.pkl         — long-form pandas DataFrame for     │
│                            ctx.dataframe(handle)              │
└──────────────────────────────┬────────────────────────────────┘
                               │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│ REPORT  dat_curves                                            │
│  • Find most-recent snapshot with both summary + curves JSON  │
│  • Bin curves by kind (PFR, HD2, HD3, …)                      │
│  • Per kind: Bokeh figure, ≤ 64 stride-sampled overlays +     │
│    a per-X median in orange                                   │
│  • Render via Panel pn.Tabs in an iframe                      │
└───────────────────────────────────────────────────────────────┘
```

---

## 7. Sanity checks before reporting "done"

| Check | What it tells you |
| --- | --- |
| `parse_dat_to_snapshot()` summary has `all_clean_parses: true` | Every per-item `struct_bytes` was exactly consumed — version dispatch correct |
| `leftover_bytes == 0` | The file's outer count matched the actual layout |
| `cluster_versions == [6]` (or `[2]`, `[3]`) | One DAT version per file (mixed versions would be unusual; flag if seen) |
| `curve_kinds` × `n_unique_serials` ≈ `n_items` | Cycle structure looks regular |
| Y dB ref of a PFR curve | Should be `2e-5` (Pa); other refs reveal calibration drift |

If any of these is off, don't trust the curve plot — re-check the file family detection in `find_dat_in_dir`.

---

## 8. Other versions the parser handles synthetically

The parser supports DAT v2 (SC 4.13), v3 (SC 5.54), v6 (with and without `Fill Baseline`), WFM v3, and RES via spec round-trips — synthetic one-item files constructed from the manual layout, parsed back, every field asserted. That proves parser/spec consistency for the older variants we don't have real-world samples for. Real-file coverage so far is DAT v6 only (verified on multiple loudspeaker test sequences).

The canonical reference is the SoundCheck 21 user manual, chapter 35 ("Data File Format"), which documents the on-disk layout for every supported version. LabVIEW flattened-data conventions (big-endian, 32-bit length-prefixed strings, IEEE 754 doubles) are documented in NI's LabVIEW reference and apply uniformly.

---

## 9. Bug-class summary

| Bug | Symptom | Fix |
| --- | --- | --- |
| Treating "TestInfo" as a top-level item | Item count doubles, parsers misalign | TestInfo is the *content* of each curve's Test Info string; not a top-level item |
| Assuming little-endian | All numbers nonsense | LabVIEW flattened defaults to **big-endian** (refs 9–12) |
| Missing the `Fill Baseline` SC6.11 variant | `extra_hex` non-empty for old files | Probe `i16 + u32`; if u32 won't fit, treat as no-baseline branch |
| Reading the 42-byte title as length-prefixed | Garbage names + offsets shift | The 42B title is fixed-width, space-padded; the LabVIEW length-prefixed name comes after the header |
| Forgetting v2/v3 had `Xprefix/Yprefix/Zprefix` | Older files report wrong units | Read three additional length-prefixed strings before the unit triple in v2/v3 |
| Plotting raw Hz on a linear axis | Sweep tells you nothing | `x_axis_type="log"` whenever the X unit is `"Hz"` |
| Overlaying every serial | Iframe goes catatonic above ~200 lines | Stride to ≤ 64 visible curves and add a median line on top |
