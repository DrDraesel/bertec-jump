# Vision v2 — live assessment, audio/face context, vision-only mode (frozen contract)

Built on the delivered v1 vision lane (MediaPipe Full sidecar → JSONL + MJPEG → plate service).
Research base: `VISION-BALANCE-RESEARCH.md`, `NORMS-AND-NEURO-INTERPRETATION.md`, `POSE-FUSION-TECH.md`.

## 1. Worker (`vision/worker.py`) — additive
- **Yellow skeleton**: all 33 pose landmarks + every pose connection drawn in yellow (#FFD400),
  joints filled, thicker lines, readable on dark and light backgrounds. Default on; `--skeleton-color`.
- **Face (optional, front camera)**: `--face --face-model <face_landmarker.task>`; emits
  `face: {visible, mouth_open, brow_raise, brow_furrow, eye_squint}` as **expression proxies**
  (normalized landmark geometry, no emotion/pain claim).
- **Audio (optional)**: `--audio [--audio-device NAME] [--stt]`; 16 kHz mono via sounddevice;
  per 0.5 s window: `dbfs`, `pitch_hz` (autocorrelation 60–400 Hz), `speech` (activity), and
  `stt: {state, text, flags[]}` when faster-whisper is importable (lazy load, never blocks the
  pose loop; model auto = base). Keyword flags ES/EN: pain/discomfort ("pain", "hurts", "duele",
  "dolor", "molestia"), comprehension ("no entiendo", "no entendí", "no comprendo", "confused",
  "what?", "again"), stop ("stop", "para", "pare", "basta").
- **Events**: every frame may carry `events: [{event, severity, detail}]` — pose_lost/regained,
  low_visibility, high_trunk_sway (baseline-relative), audio_loud (dBFS threshold), audio_pain_phrase,
  audio_unknown_phrase, audio_stop_phrase, face_pain_proxy (furrow+raise), stt_unavailable.
- **Frame JSONL (frozen additive shape)**: `{frame_id, t_mono_ms, pose_found, fps, model:{backend,name},
  metrics:{...v1 keys...}, audio:{...}|null, face:{...}|null, events:[...]}`.
- Honesty rules: every audio/face value is observational; when a component is unavailable the key is
  null and a status line says why. No fabricated transcripts, no emotion labels.

## 2. Manager (`vision/manager.py`) — additive
`status()` adds `audio`, `face`, `events` (last 100), `model`. `drain_frames()` unchanged.

## 3. Guide (`vision/guide.py`, pure stdlib) — new
`guide(mode='vision-only'|'with-plate', views=('side',) | ('front',) | ('side','front'))` → dict:
`tests: [{id, name, duration_s, trials, instructions, camera, needs}]`, `camera_setup`,
`notes`, `sources` (URLs from the research docs). Battery (vision-only): double-leg EO 30 s ×2,
double-leg EC 30 s ×2 (spotter), single-leg L/R 30 s ×3, tandem 30 s ×2, squat 5 reps, sit-to-stand
5 reps, walk 10 m if space. With plate: mCTSIB (foam required for C3/C4), SLS, WBS, asymmetry.
Camera guidance: **side/oblique 30–45° at hip height is the default single-camera view**; add front
for face/frontal; state distances (2.5–4 m) and lighting.

## 4. Vision-only sessions (`vision/sessions.py`, stdlib) — new
Store under `data/vision_sessions/<id>/`: `session.json` (label, started/ended, mode, view, age/sex,
notes, summary, event_counts), `frames.jsonl` (one line per frame: t_rel + metrics + audio + face +
events), `report.html`. API: start/stop/list/get/report. Works with the plate disconnected.

## 5. Service/API — additive
- `snapshot()['vision']` gains audio/face/events/model via manager status.
- Vision-only session accumulation in the acquisition loop (frames + events), summary via
  `VP.summarize_frames` + event counts; never touches plate state; stop/start refused while a
  protocol trial runs only if it would disturb it (vision-only is allowed when the plate is idle).
- `POST /api/vision/event {type, note?}` — operator marks ("pain", "did_not_understand", "note").
- `GET /api/vision/guide?mode=&views=`.
- `POST /api/vision/session/start|stop`, `GET /api/vision/sessions`, `GET /api/vision/session/{id}`,
  `GET /api/vision/session/{id}/report`.

## 6. UI — two tabs
Top-level tabs **Plate lab** (existing page) and **Vision-only** (new): guide panel (recommended
tests, durations, camera placement), start/stop vision-only session (label, age, sex, view),
yellow-skeleton feed, live metrics, **audio panel** (level meter, pitch, speech, transcript, flags),
**face proxies** (front camera only), **observation log**, operator mark buttons (pain / didn't
understand / note). Existing plate UI untouched.

## 7. Reports
- Vision-only printable report: quality header, kinematics, audio/face observations (labelled
  observational), operator marks, limitations + do-not-claim list.
- Plate report keeps its vision section; adds audio/face/events rows when present.

## 8. Non-negotiable medical honesty
No diagnosis, no pain/emotion detection claim, no fall-risk or clearance, no camera COP, no
"balance score", transcripts are local STT output that may be wrong. Every automated inference is
labelled (estimate/proxy/observational). Physician-led language everywhere.

## 9. Model-choice answer (for the operator)
Backend = **MediaPipe Pose Landmarker Full** (7.2 ms/frame measured locally). YOLO-pose (Ultralytics)
is AGPL-3.0 and in the same accuracy class for single-person pose; RTMPose-m (Apache-2.0) is the
documented accuracy escalation if needed later. No unverified claim of superiority in UI or docs.
