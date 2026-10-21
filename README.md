# Bertec Jump Lab — local hardware dashboard

A Windows x64 local web app for the photographed Bertec BP5046 balance plate. It uses the official Bertec Device SDK 2.57 (July 2026) through ctypes, not a reverse-engineered USB protocol. There is no live demo generator. With no device, all measurement cards remain empty.

**Status (2026-10-21):** research build in progress — full suite green (**1,114 tests** at the last full run; the suite grows with every build wave — run the command under Tests for the current count). Modules added since the first weight-capture release: `metrics.py` (pure-stdlib COP and postural-sway math — 10 Hz zero-phase low-pass, decimation to 100 Hz, path length/velocity, 95% confidence ellipse, Romberg/foam ratios, ASI, MDC change flags; `PROCESSING_VERSION` stamps every stored trial with its processing chain), `protocols.py` (guided protocol catalog plus session/trial runner engine), `conductor/` (Phase 6A session-manifest skeleton for the later multi-modal summary), `tools/verify_cop.py` (read-only operator sign/scale verification of the live plate). Every metric is a research estimate for qualified staff; the safety rails below are unchanged, and nothing here is a medical device.

## Run

Double-click `Start Jump Lab.cmd`, then open http://127.0.0.1:8765 . Keep the launcher window open. Closing it stops the application. Only one instance should own the device.

Developer start: `.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8765 --workers 1`.

## Connect and measure safely

1. Put the plate on a firm, flat surface and follow Bertec's hardware instructions. Connect its USB-B port to THIS Windows computer. Verify its specified power connection. A phone browser alone cannot access the SDK/USB device.
2. Close Digital Acquire, Balance Advantage, or other applications owning the same plate. Open the dashboard and choose Connect / rescan. A loaded SDK is not proof of a connected device: the dashboard must list its model/serial and receive fresh samples.
3. If no device appears, check Windows Device Manager, the USB cable and power. The SDK Readme recommends installing the current Bertec USB driver via Digital Acquire: https://www.bertec.com/downloads . No firmware was flashed or driver replaced by this project.
4. Completely unload the plate, choose Zero and explicitly confirm it is empty. Keep it empty during the settling period and until the app verifies at least one second within ±20 N. The app never zeroes automatically under a person.
5. Start a recording, then collect at least two seconds of quiet standing. The body-weight card fills once a full 2 s still-standing window passes the stability check; it reports the measured weight force (N), mass (kg), pounds and the window SD. Use an anonymous athlete ID where possible. Force traces and raw CSV data are available; do not perform jumping until the BP5046 limitation below is resolved.
6. Stop recording and download the raw CSV and JSON report. Files remain on this PC under `data/<record-id>/`. Recordings have a ten-minute cap. Unexpected shutdown can leave raw CSV and an `in_progress.json`; those are incomplete captures, not valid completed reports.

## Important BP5046 limitation

Bertec's Static System manual lists Essential Balance Plate USB-B, 1000 Hz output, and maximum patient weight 500 lb / 227 kg. That is not a verified dynamic jump/landing impact limit. This app does not establish mechanical safety or validate clinical jump measurements. Ask support@bertec.com whether your exact BP5046 and serial are suitable for repeated jumping, and obtain the allowable force/impact range and calibration requirements. A generic force-plate marketing page is not confirmation for this balance-plate model.

The local `config.json` leaves jump testing disabled. Only AFTER receiving model/unit-specific confirmation and validating calibration, a qualified operator may set `jump_testing.approved` true, enter the manufacturer's reference and the exact live SDK serial, and restart. The app requires an exact serial match. This configuration is not a substitute for hardware approval. No browser control silently bypasses it. The dashboard no longer shows a standing safety banner (operator request for space), but the lock itself is unchanged: jump/power cards stay disabled and the full warning text remains in every API status and session report.

## Acquisition and calculations

- SDK device discovery, calibrated channels and native timestamps; no fake sample fallback.
- One connected device is supported, deliberately rejecting ambiguous multi-plate selection.
- Explicit FZ channel selection by name, never a hardcoded channel index.
- FZ is preserved signed. The app expects compression/upward ground reaction force to be positive; verify against a known stationary load. Negative values are not converted with abs().
- Left/right loads are shown only for explicit FZL/FZR channel labels. Unknown labels remain unavailable pending confirmation, never guessed as half of total load. All received channels are preserved in raw CSV.
- Full-rate sample processing and raw capture; the browser force trace is decimated to about 100 Hz and refreshed at 10 Hz. The browser view is not the raw analysis stream.
- Calibrated force is in newtons; mass estimated from stable force/g. Body weight = mean Fz of the latest stable >=2 s still-standing window on the zeroed plate (kg, lb, N and window SD). It is a measured force divided by g, not a certified scale; any discontinuity, re-zero, load change or stale stream clears or hides it. The unvalidated research estimates integrate net acceleration `(Fz - mg)/m` from quiet standing, with external COM power `Fz * velocity`.
- Flight-time height uses `g*T*T/8`; it assumes equal COM height at takeoff and landing. Tucked-leg landings and stepping off invalidate that assumption. Power is an estimate, not a directly measured SDK channel.
- Discontinuity, stale data, nonfinite values and buffer errors stop a recording as incomplete rather than silently filling samples. Do not interpret invalidated output as a clinical result.
- Default jump lock leaves force/mass available and power/flight/jump-height unavailable.

## Protocol API & COP surfaces

A guided-protocol runner (research use) is being integrated on top of this weight-capture service. Under the frozen contract for that integration wave, every `/api/status` snapshot and every `/ws` trace point carries the moments and COP channels `mx_n`, `my_n`, `copx_mm` and `copy_mm` alongside `t`/`fz_n`; COP stays `None` on an unloaded plate (below the 20 N force gate) and is never fabricated or imputed. Stored trials are stamped with `metrics.PROCESSING_VERSION` (`lp10-dc100-v1` = 10 Hz zero-phase low-pass, then decimation to 100 Hz), so any stored value is traceable to the processing that produced it — filter and decimation settings dominate between-session comparability more than the metric formulas do, and a settings change must bump the version rather than silently re-scale old recordings.

Frozen protocol endpoint contract (loopback-only local operator control; no cloud path, no auth surface added by these routes):

- `/api/protocol/catalog` — protocol catalog (protocol ids, conditions, trial counts, safety-rail text).
- `/api/protocol/state` — current runner state (status, condition/trial index, timer, live quality flags).
- `/api/protocol/start` — start the selected protocol session.
- `/api/protocol/begin_trial` and `/api/protocol/end_trial` — open and close one trial; samples stream between them.
- `/api/protocol/keep_trial`, `/api/protocol/discard_trial`, `/api/protocol/repeat_trial` — operator trial control (keep / discard / repeat, parity with the Balance Advantage Ctrl-Del flow).
- `/api/protocol/skip_condition` — skip the current condition; `/api/protocol/note` — attach an operator note.
- `/api/protocol/close` — close the session and write its artifacts (raw data plus JSON report).

### Plate verification (first live unit)

First live connection, **2026-10-16**: model **AM6147-E**, serial **00000000-S017620**, firmware **fw1101**, 1000 Hz, channels **Fz / Mx / My**. Read un-zeroed, the plate settled at about **-7203 N** on Fz (stable; SD 0.07 N), i.e. a large offset — which is exactly why un-zeroed absolute values must not be interpreted, and why the verification tooling works from differences.

Operator procedure before any stored metric is trusted:

1. Completely unload the plate, zero it in the dashboard, and explicitly confirm the plate is empty (the app never zeroes automatically under a person).
2. Run `.venv\Scripts\python.exe tools\verify_cop.py --weight-kg <your mass>` and follow its prompts (it steps you through an empty read, a loaded read, and an unload check). The tool is read-only: it never zeroes, records, connects/disconnects, or writes config.
3. Read the sign verdict (does loading the plate increase Fz?) and the scale verdict (does a known mass produce the expected force?) from its report under `verification/`.

Both verdicts come from the DELTA between empty and loaded readings, which is offset-independent — so the un-zeroed offset above cannot invalidate them, while un-zeroed absolute values remain invalid until the plate is zeroed. Until sign and scale pass for THIS serial, absolute force/COP values are not meaningful and sessions must not be compared across units.

## Vision-only assessment

A camera-only lane runs without the plate, so a movement session is still
possible when the plate is disconnected or unsuitable. It uses the MediaPipe
Pose Landmarker (Full) sidecar, records `data/vision_sessions/<id>/`
(`session.json`, `frames.jsonl`, a report route), and writes a printable,
self-contained HTML report (`vision/vision_only_report.py`).

- **Operator guide:** [`docs/VISION-ASSESSMENT-GUIDE.md`](docs/VISION-ASSESSMENT-GUIDE.md) — view choice, the recommended battery (double-leg EO/EC 30 s ×2, single-leg L/R 30 s ×3 with a spotter — six trials total, tandem 30 s ×2, squat 5 reps, sit-to-stand 5 reps, walk 10 m), camera placement (side/oblique 30–45° at hip height, 2.5–4 m), recording-length evidence, audio/face consent, privacy and model choice. The camera records **posture and kinematics** for each task; hold time, repetition counts and gait events are **not** scored by the camera — time or count them with the plate or a stopwatch and record them as operator marks.
- **What a session stores:** pose-estimated joint angles and trunk-sway ranges as estimates (every angle cell is labelled an estimate; BlazePose knee angles carry a 15.49° static RMSE and ICC 0.136 in single-leg stance, and quiet standing is the worst case), frame/coverage/processing-rate quality, worker events, optional audio activity with a local transcript and labelled keyword matches, optional face-landmark proxies from a front view, and operator marks (pain / did not understand / note). Audio, speech-to-text and face analysis are **off by default** and start only when enabled and consented; transcripts are stored locally as text.
- **What it never does:** diagnose, detect pain or emotion, score balance or fall risk, or clear anyone for sport. It computes no knee-valgus angle (not even a change score), no hold time, no rep count and no gait metric. Audio and face values are labelled observational and speech-to-text may be wrong; the report prints the full do-not-claim list and a "not a diagnosis" footer.
- The plate report keeps its vision section and adds a compact **Session observations** block (operator marks, audio activity, face proxies, worker events) when the session carries one; a session without them renders exactly as before. Kinematics render **one row per stored test segment** (test / limb / trial) when the session was segmented and say so when the whole clip was summarised as one block. A plate session that stored **COP–COM but no camera data** still renders the COP–COM block on its own; it is a low-pass estimate, explicitly not the validated gravity-line projection, and MDC is applied only for a known stance.

## Architecture and privacy

`bridge.py`: official native DLL ABI + device discovery/polling. `service.py`: one SDK-owning worker, quality checks, recording files. `analysis.py`: bounded-memory analytics. `app.py`: FastAPI + read-only live WebSocket. `static/`: dependency-free dashboard.

Loopback-only listening, same-origin checks and Host validation. No cloud upload or analytics. No authentication/encryption at rest: anyone with this Windows account or filesystem access may read recordings. Do not expose this app to a LAN/public tunnel or treat it as HIPAA-compliant patient storage.

## Tests and dependencies

Run `.venv\Scripts\python.exe -m unittest discover -s tests -v`. The suite is **1,114 tests** at the last full run (2026-10-21), all passing, including the vision report, vision-only report, plate-report v2 observations and vision-only API tests. Numerical fixtures exist ONLY in tests, never in the live app. The native initialization test uses the actual vendor DLL but does not require a connected plate. Hardware accuracy, load calibration and live timing still require a real connected unit and reference loads.

Runtime/development packages are pinned in `requirements.txt`. The `.venv` is isolated from Hermes. Vendor files remain local in `vendor/`; review Bertec's license in its PDF before any redistribution. Bertec and its SDK remain Bertec Corporation property; this dashboard is an independent local integration, not a Bertec-approved product.

## Documents checked

Official downloads and SDK: https://www.bertec.com/downloads
SDK archive: https://downloads.bertec.com/SDK/Bertec_Device_SDK_July_2026.zip
SDK revision history: https://downloads.bertec.com/SDK/Bertec_Device_SDK_July_2026.html
Official Static System manual: https://downloads.bertec.com/PDFs/Bertec%20Balance%20Advantage%20Static%20Manual.pdf
Local SDK API header: `vendor/bertecif.h` (2.57; authoritative function signatures).
Local SDK documentation: `vendor/80P-0165 Bertec Device Interface Library.pdf` (PDF 2.56; header/release notes take precedence where newer).

SDK pitfalls verified during development: ChangeMaxBufferedDataSize returns the allocated size, not zero; Stop may return -34 while asynchronous startup is in progress, so Close must wait until Stop succeeds. Closing early caused a native access violation in a reproduction test and is now guarded.
