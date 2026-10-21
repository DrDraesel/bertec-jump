# Vision × Bertec — integration spec (draft v1, 2026-10-21)

Goal: while the athlete stands on the plate (double-leg, single-leg, mCTSIB-style, weight-bearing
squat), a camera observes them and a local pose model tracks joints in real time. Force/COP and
joint kinematics are recorded on one session timeline, summarised per trial, compared against
age/sex literature references, and rendered in a clinician-style report (assistive, not a diagnosis).

## Constraints (verified in the repo / machine)

- Bertec venv is **stdlib-only** (no numpy, no pip). Do NOT add heavy deps there.
- RunTrack venv already carries mediapipe 1.0.1 + opencv 5.0 + numpy 2.4 and a working pose
  pipeline (PoseLandmarker tasks API, 1-Euro filter, video mode).
- Plate service (`service.py`) owns the USB device on one acquisition thread; **camera work must
  never block or mutate acquisition**. Vision is optional and failure-isolated.
- App is FastAPI behind TrustedHost + same-origin middleware; UI is plain JS panels.
- Existing safety rails stay untouched: jump lock, 20 N COP gate, spotter metadata, trial
  guards, atomic writes, containment of stored refs.

## Architecture — sidecar pose worker

```
Bertec service (stdlib)                     Pose worker (RunTrack venv)
┌─────────────────────────────┐   spawn     ┌──────────────────────────────┐
│ Acquisition thread          │────────────▶│ capture (USB/URL/file)       │
│  bridge.poll → ingest()     │             │ MediaPipe PoseLandmarker     │
│  protocol/training sessions │◀──JSONL─────│ 1-Euro filter → metrics      │
│  VisionManager (reader)     │  stdout     │ MJPEG server :8791 /feed     │
│  snapshot()['vision']       │◀──HTTP──────│ /state (latest frame JSON)   │
└─────────────────────────────┘  proxy      └──────────────────────────────┘
```

- `vision/worker.py` runs under a **configurable interpreter** (`vision.python`, default
  `C:/Users/GAMEPOWER/projects/runtrack/.venv/Scripts/python.exe`; `sys.executable` when
  mediapipe is importable there). It prints one compact JSON line per frame
  `{frame_id, t_mono_ms, pose_found, fps, metrics{...}}` and serves the annotated MJPEG feed.
- `vision/manager.py` (stdlib) spawns/monitors the worker, reads JSONL into a bounded ring
  (≈3000 frames ≈ 2 min @25 fps) + latest-frame snapshot, restarts on crash with backoff, and
  exposes `status()/start()/stop()`. Worker failure NEVER touches acquisition.
- `service.py`: optional `self.vision` (config `vision.enabled`); during a running trial the
  service drains new vision frames and appends `vision_rows` entries with the **plate-relative**
  timestamp of arrival plus the worker's own monotonic timestamp (sync diagnostics). At
  keep/close, `vision.metrics.summarize(rows)` writes `vision_summary` into the trial record +
  session JSON.
- `app.py`: `/api/vision/status|start|stop`, `/api/vision/feed` (MJPEG proxy), `/api/vision/state`.
  Same-origin/Host middleware applies unchanged.
- UI: sidebar panel **06 Camera / vision** (enable, source usb/url/file, open/close, fps + pose
  state) and an assessment panel **Joint tracking (live)** (feed with skeleton overlay + live
  angles/trunk sway). Protocol session panel gains a small "camera linked" badge.

## Data model

Per vision frame (worker → service): `frame_id`, `t_mono_ms`, `pose_found`, `fps`,
`metrics`: `left_knee, right_knee, left_hip, right_hip, left_ankle, right_ankle,
trunk_lean_deg (signed), trunk_sway_x, trunk_sway_y, head_tilt, shoulder_tilt,
pelvic_tilt_deg, knee_valgus_l, knee_valgus_r, com_x, com_y, com_hip_x, com_hip_y,
shoulder_width_norm, hip_width_norm, body_height_norm, visibility_min`.
All in normalized image units unless a scale is available; the report labels estimates.

Per trial `vision_summary`: `frames`, `pose_coverage_pct`, `fps_mean`, `fps_p95_latency_ms`,
`sync {plate_span_s, vision_span_s, offset_ms_estimate, jitter_ms_sd, method:'software'}`,
`angles {metric: {mean, sd, min, max}}` for knee/hip/ankle/trunk/pelvic, `sway
{trunk_sway_x_range, trunk_sway_y_range, com_range_mm_est?}`, `asymmetry
{knee_valgus_delta, hip_angle_delta, weight_bearing_est?}`, `flags[]`, `notes[]`.
`com_range_mm_est` only when height_cm and a clean full-body view exist; otherwise omitted —
never fabricated.

## Sync

- Both clocks are monotonic; `vision_rows[i].t_plate_rel` is the plate time at which the frame
  was received (≤ poll interval, ~5 ms). Worker timestamp retained for latency accounting.
- Quality: report `offset_ms_estimate` (median of vision_t_mono − plate_t at receipt),
  `jitter_ms_sd`, and the method (`software`). Optional visual clap marker documented for
  frame-level alignment; no hardware genlock claimed.
- Camera fps (20–30) is far above balance sway content (<2 Hz); aliasing is not a concern for
  the reported means/ranges. State this in the report's limitations.

## Reference layer (age/sex)

`vision/norms.py` holds literature reference bands by measure and age band/sex, each entry:
`{measure, unit, age_min, age_max, sex, mean, sd|range, device_note, source_url, year,
transferable: bool}`. Lookup returns the narrowest matching band; if none → `None` and the
report prints raw values only with "no published reference for this age/sex band".
Never interpolate silently; never present device-specific norms as universal.

## Report (clinician style)

New section **"Camera-derived kinematics (pose estimate)"** per session:
- Coverage + sync quality header (frames, %, fps, offset/jitter, method).
- Per-condition table: joint angles (mean ± SD), trunk sway range, pelvic tilt, knee valgus
  delta, asymmetry; COP metrics remain in their own existing sections (never blended).
- Age/sex reference comparison only where a transferable band exists; otherwise "reported only".
- Interpretation block: pattern-level observations (e.g. increased trunk sway with eyes closed,
  asymmetric single-leg strategy, excessive frontal knee motion) phrased as **screening signals**,
  plus a mandatory limitations paragraph: 2D pose estimates, no 3D reconstruction, no diagnosis,
  physician-led decisions.
- Optional local-AI narrative: OFF by default; if enabled, uses only the structured numbers.

## Safety / privacy

- Camera frames are processed locally; only derived numbers + JPEG stream stay on 127.0.0.1.
- Vision can be disabled entirely (config default `false` until the operator enables it).
- Starting/stopping vision is refused mid-trial only if it would disturb acquisition — it must
  never mutate plate state; opening a camera is independent of the plate.
- No patient identifiers in filenames; athlete label only.

## Tests (must keep existing 591 green)

- `tests/test_vision_metrics.py` — angle/sway/COM math on synthetic landmarks; summarize
  coverage/sync maths; no fabricated values when inputs missing.
- `tests/test_vision_manager.py` — fake worker script emitting JSONL; ring buffer, restart,
  crash isolation, status shape.
- `tests/test_vision_api.py` — routes with a stub manager (status/start/stop/feed headers).
- `tests/test_vision_report.py` — report section renders with/without vision_summary; norms
  lookup honoured; no third-view-style leakage into COP sections.
- Protocol integration test: with a fake manager, a kept trial stores `vision_rows` +
  `vision_summary`; without it, records are byte-identical to today's shape apart from no
  new keys.
- All tests use temp data dirs; never touch `data/`, never open the plate, never open a camera.

## Phases

1. **P0 (this wave)** — worker + manager + service wiring + UI live panel + tests.
2. **P1** — protocol/trial vision capture + summary + report section + age/sex inputs.
3. **P2** — norms layer filled from research + clinician-style interpretation templates.
4. **P3** — bike/cycling camera view (optional), NeuroSync (EEG) combo later; 3D/dual-camera
   fusion if the tech survey justifies Azure Kinect body tracking.

Acceptance for P0: suite green; live smoke with a file-based camera source produces vision
frames with valid metrics; UI shows feed + angles; plate acquisition unaffected (no stall).

---

## v1.1 — evidence update (after 3 research reports, 2026-10-21)

Sources: `VISION-BALANCE-RESEARCH.md`, `NORMS-AND-NEURO-INTERPRETATION.md`, `POSE-FUSION-TECH.md`.

**Scope corrections (binding):**
1. **No camera-derived COP.** Valid only with multi-view + inverse dynamics (AP COP CC 0.95–0.98
   multi-view; single RGB-D LoA −36…+23 mm). Camera explains *how* COP moved; the plate measures it.
2. **Add plate-only COP–COM (gravity-line projection)** — new `cop_com.py`: COM ≈ low-pass(COP),
   report COP–COM difference RMS/velocity. High value, no camera. Caveat: the simple low-pass
   shortcut is least reliable in **single-leg stance** — flag SLS results as approximate and prefer
   reporting COP metrics there; camera does not replace this.
3. **One camera for v1** (side/oblique), MediaPipe **Full** (measured 7.2 ms/frame → 60 fps headroom);
   two-camera triangulation is P2, not P0. **Reject Azure Kinect body tracking** (SDK retired
   2024-08-17; CUDA 11.x builds unusable on Blackwell/RTX 5070 Ti; 30 Hz; EULA risk).
4. **Honest camera output classes:** trunk/pelvis *relative sway* usable (±0.5–1 cm est.);
   sagittal knee/hip angles = markerless estimate 5–10° MAE (label per value);
   **frontal knee valgus = change scores only** (absolute bias −19.3°, SLS ICC 0.136);
   **no foot-lift scoring** (ankle landmarks ±12°) — use plate unload thresholds;
   **no "balance score" from camera alone**.
5. **MDC layer:** velocity MDC ≈ 28–31 % of mean, area ≈ 50 % (provisional, literature).
   Any change below MDC is reported as "no measurable change". Local MDC needs repeat testing.
6. **Norms are per-source** (Bertec ≠ NeuroCom ≠ BTrackS ≠ Zebris); sex adjustment per source,
   never global; no interpolation; missing band → raw value only. Honour the 14-point
   "do not claim" list from the norms report (no diagnosis, no individual fall-risk, no
   clearance, no device transfer, no area-as-stability, state 90 % vs 95 % ellipse, …).
7. **mCTSIB requires a foam pad** for conditions 3–4 (a bare plate cannot run them) — surface this
   in UI/report guidance; it is not an SOT substitute.
8. **Sync:** software first, report offset/jitter honestly; the plate exposes 8 kHz timestamps and
   unused SYNC/AUX pins — a Bertec-clocked LED marker (±0.5 frame) is the documented upgrade path,
   not a v1 claim.

