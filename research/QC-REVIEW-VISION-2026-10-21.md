# QC review — Bertec vision lane (v1 + v2)

**Date:** 2026-10-21 · **Mode:** read-only adversarial review · **Scope:** the five vision research docs, `vision/*.py`, `cop_com.py`, the vision paths in `service.py`/`app.py`/`static/vision.js`/`static/index.html`, and the vision tests.

**Method.** Read every file above, traced the data from worker JSONL → manager → service → stored session → report, and exercised pure functions directly (no camera, no plate, no `data/`). Suite run for this review: `pytest -q` → `1001 passed, 3 warnings, 701 subtests passed in 53.09s`; `unittest discover` → `Ran 1001 tests ... OK`. The only file written is this report.

**Bottom line.** The prose layer is unusually careful about not diagnosing, but the data path does not yet live up to it: every plate trial's pose coverage is reported as 100 %, every non-square camera distorts every angle, the audio/face half of v2 is never summarised into the report, and the one function that would turn worker flags into report counts fabricates all three flag categories.

## What is already sound

- No `normal`/`abnormal` verdicts: `_verdict` can only return three strings (`vision/vision_report.py:1043-1051`), the 14-item do-not-claim list is rendered verbatim (`vision/norms.py:379-408`), and both pages carry "not a diagnosis / no fall risk / no clearance" footers (`vision/vision_only_report.py:561-573`, `vision/vision_report.py:1191-1196`).
- No camera COP or "balance score" claim; the lead paragraph explicitly subordinates the camera to the plate (`vision/vision_report.py:1215-1219`).
- Raw audio waveforms are never written, and raw 478-point face landmarks are never persisted — only features, transcript text and four face proxies (`service.py:1806-1817`). The operator guide discloses that transcript text is stored (`docs/VISION-ASSESSMENT-GUIDE.md:227-234`).
- Session-store path traversal is guarded by an id regex plus a resolved-parent check (`vision/sessions.py:267-283`); non-finite floats become `null` (`:98-141`); report routes 404 unknown ids (`app.py:775-782`). Both printable pages escape every value and pin `default-src 'none'` CSP (`app.py:44-45`, `:783-800`); I found no XSS.
- The vision-only report route does call the full renderer, not only the section renderer (`app.py:783-787`), and `--face/--audio/--stt` do reach `_worker_command` (`vision/manager.py:306-321`).
- Manager locking, event-log locking, session-store locking, worker landmarker cleanup and `Acquisition.shutdown()`'s `self.vision.stop()` are all present and correct.

## CRITICAL

### C1 — `_absorb_flags` turns the worker's boolean flag dict into "all flags true"
`vision/vision_report.py:412-433` computes `number = _number(count)` then `+ (number if number is not None else 1.0)`, while `_number` (`:132-140`) deliberately returns `None` for any bool. The worker really emits `{"pain": true, "comprehension": false, "stop": false, "matches": [...]}` (`vision/audio_math.py:396-408`). I called `_merge_audio` with one "duele" transcript: `{'pain': 1.0, 'comprehension': 1.0, 'stop': 1.0, 'matches': 1.0}` — false categories and the `matches` list all become positive counts.

**Why it matters:** once an audio aggregate exists (see H3), the report will print "comprehension x1, stop x1" for a session where neither occurred — fabricated clinical observations. It is currently masked only because no code builds that aggregate, which makes H3's fix a trap unless this is fixed first.

**Required work:** handle booleans explicitly (count only `True`), ignore `matches`, and add a test using the real `keywords()` output shape.

### C2 — Every plate trial with ≥10 frames reports 100 % pose coverage, even when half the frames had no pose
`service.py:1997-2000` builds trial rows as `{**record['metrics'], 'frame_id': ..., 't_mono_ms': ..., 't_rel': ...}` — `pose_found` is dropped. `vision/pose_math.py:463-472` then falls back to `found += 1 if _metrics_of(row) else 0`; `_metrics_of` (`:335-342`) returns the row itself when there is no `metrics` key, and every row is non-empty because it carries `frame_id`. I ran the exact shape with 10 of 20 frames pose-less: `pose_coverage_pct: 100.0`.

**Why it matters:** the quality header and the "<80 % coverage" warning (`vision/vision_report.py:946-956`) are both defeated for plate sessions; a clinician is told the angles rest on full tracking when they do not.

**Required work:** carry `pose_found` into each trial row (or pass a parallel mask); assert a mixed-coverage plate trial yields ~50 %, not 100 %.

## HIGH

### H1 — All angles are computed in anisotropic normalized image coordinates
`vision/pose_math.py:129-147` (`angle_deg`) consumes `_xy` normalized coordinates directly; `frame_metrics` (`:222-332`) has no aspect argument, and `vision/worker.py:1288` calls `frame_metrics(lms)` with no frame dimensions. A physical 45° knee angle on a 16:9 frame computes as **29.33°**; a physical 10° trunk lean computes as **5.66°** (I ran both). Only square frames are correct.

**Why it matters:** every knee/hip/ankle/trunk/pelvic value in both reports depends on camera aspect ratio; changing resolution silently changes the measurement. The report compounds this by calling the output "degrees" (H2).

**Required work:** scale x by `frame_width/frame_height` (or work in pixels) before any angle; thread aspect through the worker; add non-square-frame tests.

### H2 — The report's stated uncertainty band is smaller than its own cited error, and claims a valgus change score that does not exist
`vision/vision_report.py:911-916`: "Sagittal joint angles are pose-worker estimates (degrees when a camera scale was available, unit-less image units otherwise). Markerless 2-D error is 4–7 deg in gait ... treat every angle as an estimate with a 5–10 deg uncertainty band." `:1176-1178` repeats "5–10 deg mean absolute error". The lane's own source says BlazePose knee angles have "**static RMSE 15.49°**" and "**ICC 0.136 during single-leg stance**" (`research/POSE-FUSION-TECH.md:57`), and quiet standing is the primary use case. There is also no camera scale anywhere in `pose_math.py` — the "(degrees when a camera scale was available" parenthetical describes a feature that does not exist.

The same section says "absolute frontal-plane knee valgus is not reported (change scores only)" (`:1182`), but `pose_math.frame_metrics` never computes `knee_valgus_l/r`; grep finds those keys only in the report and tests. No change score exists either.

**Why it matters:** this is the exact optimism the research doc warned against; a 15° static error can turn a real 10° asymmetry into noise while the report claims a 5–10° band.

**Required work:** state the image-plane nature and the 15.49°/ICC 0.136 evidence for quiet standing; remove the "change scores only" valgus claim until a valgus metric exists; label every angle cell "estimate".

### H3 — The vision-only report says no audio/face was recorded even when both were stored
`vision/vision_only_report.py:302-309` (`_audio_payload`) and `:396-403` (`_face_payload`) read only `summary['audio']` / `summary['face']`. `service._vision_summary` (`service.py:1875-1895`) is just `pose_math.summarize_frames`, which has no audio/face aggregates; `_append_vision_only_frame` stores them per frame only (`service.py:1806-1817`). I created a real 12-frame store session with audio and face on every frame and rendered it: the page contains "**No audio was recorded for this session**" and "**No face data was recorded for this session**" (`vision_only_report.py:351-354`, `:435-437`).

**Why it matters:** a false negative statement about recorded data in a clinical report — the opposite of the privacy-safe assumption a reader would make — and the v2 audio/face report block is effectively dead.

**Required work:** aggregate audio/face at stop from the stored rows (or read `frames.jsonl` at render), and distinguish "not recorded" from "recorded but not summarised".

### H4 — Microphone, STT and face analysis default ON, contradicting the operator guide
`config.json:16-20` has `"face": true`, `"audio": true`, `"stt": true`; `static/index.html:34-36` hard-codes `checked` on all three boxes; `static/vision.js:823-825` sends whatever those boxes say. The guide says "Audio is optional and **off unless enabled**" (`docs/VISION-ASSESSMENT-GUIDE.md:142`) and "Audio is optional and can be left off" (`:37-38`). The UI comment even claims "Defaults come from config.json" (`vision.js:818-819`), but the HTML ignores config: a `config.json` with `audio:false` still starts the mic when the operator clicks Start with the box checked.

**Why it matters:** consent. A patient can be recorded and transcribed on the first camera start without the operator making an audio decision, in a lane that explicitly handles health-adjacent data. No session field records that consent was obtained (`vision/sessions.py:324-342`).

**Required work:** default all three to off in HTML and config; honour config `false` even if the box is checked; add a per-session consent flag; mention audio/face recording in the in-app guide notes (`vision/guide.py:122-149` has none).

### H5 — The worker's HTTP server opens transcripts and video to any origin
`vision/worker.py:386` and `:423` send `Access-Control-Allow-Origin: *` on `/state` and `/feed`. `/state` returns `latest.audio.stt.text` (`:355-369`), i.e. the live transcript, and `source` (`:1447`) can include `rtsp://user:password@...`. The transcript is also burned into the MJPEG overlay (`:808-811`), so it becomes pixels in any screenshot or recording of the feed.

**Why it matters:** the app's same-origin middleware protects port 8765 only; port 8791 bypasses it. Any local process — and a browser page not blocked by Private Network Access — can read the transcript and camera URL, or embed the feed, while the lane runs.

**Required work:** remove the wildcard CORS (or allow only the app origin), redact credentials from `source`, and keep transcripts out of the video overlay.

### H6 — COP–COM subtracts an unfiltered 1 kHz COP from a 0.5 Hz-filtered COM
`cop_com.py:321-322`: `diff_x = [x - cx for x, cx in zip(xs, comx)]`, where `comx = com_series(xs, ...)` is the 0.5 Hz low-pass and `xs` is the raw trial series. `service.py:2323-2324` passes the full-rate trial series. The research report is explicit: "the COM and COP must be expressed in the same horizontal coordinate frame ... and both must be **filtered identically** before subtraction. A 10 Hz low-pass on COP and a different smooth on the camera stream will manufacture a COP–COM signal that is mostly processing artefact" (`research/VISION-BALANCE-RESEARCH.md:127`). Here COP is not filtered at all, so the high-pass residual retains broadband plate noise and the reported `cop_com_velocity_mm_s` is dominated by sample-level change rather than by the 10 Hz-filtered control signal the literature describes.

**Why it matters:** the highest-value plate-only measure is filtered asymmetrically; the residual includes broadband plate noise above 0.5 Hz, so absolute RMS/velocity are not comparable to the cited literature and the velocity will scale with sampling rate.

**Required work:** filter the COP branch with the pipeline's 10 Hz zero-phase filter (or low-pass both at the same cutoff and decimate), document the chain in the payload, and validate against a synthetic known signal.

### H7 — Vision-only sessions have no per-test or per-trial segmentation
`service.py:1598` `vision_only_start(self, label='', age=None, sex=None, view=None, notes='')`; `vision/sessions.py:324-342` stores only label/age/sex/view/notes. The eight-test guide (`vision/guide.py:152-269`) tells the operator to run double-leg, single-leg L/R, tandem, squat, sit-to-stand and walk — all into one continuous session, summarised once by `summarize_frames`.

**Why it matters:** the report has one kinematics row for the whole battery. Means blend single-leg with tandem and squat; left/right cannot be compared; no per-condition table exists despite the v2 promise. This is the single largest gap between the guide and the data model.

**Required work:** store `test_id`, limb, condition and trial index per segment, and summarize/group by them; the report must render one row per condition.

### H8 — The guide's battery asks for hold time, repetitions and gait that the pipeline never computes
`vision/guide.py:184-196` tells the operator "Hold up to 30 s; the trial ends early if the lifted foot touches down." `vision/norms.py:196-217` contains a *transferable* Springer unipedal-hold band, but `pose_math.py` has no hold-time or foot-lift metric, and the report's own limitations say "Foot-lift timing is not scored from video" (`vision/vision_report.py:1182-1184`). Squat/sit-to-stand have no rep counter, and the 10 m walk has no gait metric.

**Why it matters:** the only norm in the table that legitimately transfers to a camera-only test is unreachable; the report can never produce the primary outcome of its own single-leg test, and the walk/squat rows are angle averages over unstructured movement.

**Required work:** either compute hold time from a foot-landmark/plate event and add rep/gait segmentation, or remove those tests from the guide and say why.

### H9 — COP–COM is hidden unless a camera also ran
`vision/vision_report.py:1208-1210`: `collected = _summaries(session); if not collected: return ''`. The COP–COM block is appended inside that same function (`:1229-1231`). A plate session with `cop_com` but no `vision_summary` renders no vision section at all, so the plate-only COP–COM result — the spec's "highest-value, lowest-effort addition" (`VISION-INTEGRATION-SPEC.md:140-143`) — disappears.

**Why it matters:** the best plate-only analysis is coupled to an optional camera; most plate sessions will never show it.

**Required work:** render the COP–COM block independently of `collected`.

### H10 — "Trunk sway range" is a frame-count window and a high-pass residual, so it changes with camera fps
`vision/pose_math.py:63-65`: `SWAY_BASELINE_FRAMES = 150` — "At 60 fps this is 2.5 s". `sway_metrics` (`:366-402`) subtracts that rolling mean, and `summarize_frames` (`:487-506`) then reports the max-min of the residual as `trunk_sway_x_range`. At 30 fps the baseline is 5 s, at 120 fps 1.25 s; the same physical sway yields different ranges. The slow component is removed by construction, so this is short-term relative sway, not total sway.

**Why it matters:** the report's per-condition "Trunk sway X/Y range" (`vision/vision_report.py:897-898`) is not comparable across camera settings or fps caps, yet it is presented as a physical range.

**Required work:** use a time-based baseline (seconds × measured fps), report the residual and total range separately, and state the window in the report.

## MEDIUM

### M1 — Single-leg MDC constants are applied to every COP velocity/area measure, and two MDC engines disagree
`vision/norms.py:488-505` maps any measure containing "cop" + "velocity" to `cop_velocity` and any "cop" + "area" to `cop_area`; `_MDC['cop_velocity']['basis']` says "Single-leg 30 s total COP velocity" (`:316-319`). `vision/vision_report.py:1143-1154` then prints "COP velocity about 30 % of baseline and COP area about 50 %" with no stance qualifier. Separately, `metrics.MDC_TABLE` uses an absolute 9.0 mm/s for single-leg velocity (`metrics.py:410-421`), while 30 % of the 35 mm/s reference is 10.5 mm/s — two thresholds in the same report.

**Required work:** key MDC by measure *and* stance/condition, refuse when the stance is unknown, and unify or cross-reference the two tables.

### M2 — The COP–COM heading asserts "gravity-line projection" for a method the body says is not GLP
`vision/vision_report.py:1012` renders `<h3>COP–COM (gravity-line projection)</h3>`, while the caveat directly below says "This is not the zero-point-to-zero-point double-integration projection" (`:1004-1011`). `cop_com.METHOD` is `'gravity-line projection (low-pass COP)'` (`cop_com.py:111`), and the module docstring itself says "Do not describe it as the validated GLP elsewhere in prose."

**Required work:** rename the heading and `METHOD` to "low-pass COP–COM estimate"; keep GLP as the upgrade path only.

### M3 — The required `sync` block is never produced; the report's "sync honesty" line silently disappears
`VISION-INTEGRATION-SPEC.md:59-63` requires `sync {plate_span_s, vision_span_s, offset_ms_estimate, jitter_ms_sd, method:'software'}` in `vision_summary`. `pose_math.summarize_frames` has no such key (`:455-525`), and `vision_report._meta_list` only prints the line if `summary.get('sync')` is a dict (`:830-851`). So the quality header never states that sync was not measured.

**Required work:** compute spans/offset/jitter from `t_rel` vs plate time, or print "camera–plate sync not measured (software alignment only)".

### M4 — `VisionManager.status()` omits the v2 keys, so the live event log drops most events
`vision/manager.py:176-192` returns state/error/fps/pose_found/frames/latest/source/port/interpreter — no `audio`, `face`, `events` or `model`, contrary to `research/VISION-V2-SPEC.md` §2. `static/vision.js:509-514` therefore reads `vision.events` (absent) and only the latest frame's `events`; at a 2 Hz poll against 60 fps, `pose_lost`, `audio_pain_phrase` and `audio_stop_phrase` are usually never shown live.

**Required work:** add the promised keys to `status()` (last 100 events, model block) and have the UI merge them.

### M5 — Audio event labels state facts the detector cannot establish
`vision/vision_report.py:88` renders `audio_pain_phrase` as "**Pain or discomfort phrase heard**"; `:89` renders `audio_unknown_phrase` (which is a *confusion* phrase such as "again" or "no entiendo") as "**Unrecognised phrase heard**". `_event_counts_text` (`:671-674`) humanises the operator mark type `pain` as bare "Pain", so a tally line reads "Pain 3" with no "(operator mark)" qualifier. Spanish `para`/`pare` are ordinary prepositions but are stop phrases (`vision/audio_math.py:95`, `:104`).

**Required work:** label these "keyword match: pain (local STT, may be wrong)", show the matched phrase, drop bare "Pain" from counts, and either remove `para`/`pare` or require imperative context.

### M6 — Frame time and event time use different anchors; trial marks have no time at all
`service.py:1785-1788` anchors `t_rel` on the first *frame's* `t_mono_ms`; `sessions.py:440` anchors event `t_rel` on the session's wall-clock start. A mark at 2.5 s after Start can align with frame `t_rel` ≈ 0.5 s if the camera took 2 s to deliver. For plate trials, `service.py:1763-1766` stores `record['t_rel'] = None`, so marks have no time, contradicting the guide's "Marks are timestamped relative to the session start" (`docs/VISION-ASSESSMENT-GUIDE.md:180`).

**Required work:** use one clock for both, and compute the trial mark's `t_rel` at mark time.

### M7 — A latent reference row compares a camera weight-bearing proxy to a per-limb force corridor
`vision/vision_report.py:106-107` lists `('asymmetry','weight_bearing_est')` against `weight_bearing_left_pct`, whose only entry is a 43–57 % **per-limb force** corridor marked `transferable=True` (`norms.py:224-233`) while its own note says "A single plate yields only the resultant COP, so this is NOT measurable here; a camera weight-bearing estimate is not a substitute for per-limb force." `_verdict` would print "within published reference band" (`:1049-1051`).

**Required work:** remove that candidate or force it to "not comparable"; do not let a camera proxy receive a force-based band verdict.

### M8 — `vision.enabled` is dead, audio is app-lifetime rather than session-scoped, and no consent is stored
`config.json:8` sets `"enabled": false`, and `VISION-INTEGRATION-SPEC.md:101` says vision "can be disabled entirely", but nothing in `service.py`, `app.py` or `manager.py` reads `enabled` (grep finds no `enabled` use). `AudioMonitor` starts in `worker.run()` whenever `--audio` was passed (`worker.py:1188-1192`), not when a session starts; there is no way to stop the mic without stopping the camera, and no session field records consent (`sessions.py:324-342`).

**Required work:** honour `enabled` as a master switch, start/stop audio with the session (or add a mic toggle), and store a consent flag.

### M9 — Plate reports drop `vision_rows` and `vision_events`
`service.py:846-857` copies only `condition`, `vision_summary` and `cop_com` into the report payload; `_attach_vision_to_record` stores `vision_rows` and `vision_events` on the trial (`:1897-1912`). The plate report's "Session observations" block (`vision_report.py:677-784`) therefore never sees frame events or operator marks, contrary to `VISION-V2-SPEC.md` §7.

**Required work:** carry both lists into `_report_vision_document` (bounded) so marks/events render.

### M10 — The UI cannot display the worker's flags, never sees "face not visible", and downgrades warnings
`static/vision.js:146-155` `sttFlags` only accepts an Array or a String; the worker sends a dict (`audio_math.py:382-409`), so the panel always shows "flags —". `face.visible` is always null because `face_to_list` emits only `[x,y,z]` (`worker.py:698-707`) and `face_metrics` therefore has no visibility (`face_math.py:159-185`), so `vision.js:611` never fires. Worker severities `notice`/`warning` are not in `SEVERITIES = ['high','medium','low','info']` (`vision.js:9`, `:282-287`), so a pain-phrase event is styled `info`.

**Required work:** parse dict flags, pass visibility, and map notice/warning onto the UI scale.

### M11 — Worker lifecycle: one pose exception is permanent, URL sources stall silently, and a killed service leaves the mic running
`worker.py:1260-1262` calls `app.set_error(...)` on any pose-detection exception; the loop then parks at `:1216-1222` until the process is restarted, and manager `auto_restart` defaults to false (`manager.py:150`). URL captures never reopen — only `kind == "usb"` does (`worker.py:609-621`), so a dropped RTSP stream leaves the worker "running" with a frozen frame. `emit()` swallows `BrokenPipeError` (`:164-168`) and the worker's stdin is `DEVNULL` (`manager.py:375`); if the service is force-killed, the child keeps the camera and microphone open with no parent.

**Required work:** treat a pose exception as a frame failure, add URL stall detection and a parent-liveness check (stdin/heartbeat) that exits the worker.

### M12 — `protocol_start` does not refuse while a vision-only session is recording
`service.py:1581-1596` refuses a vision-only start when a plate session/trial owns the camera, with the reason "one camera cannot serve both" (`:1589-1590`). The mirror guard is missing: `protocol_start` (`:2364-2370`) checks only an existing protocol/training session, not `vision_only_id`. Frames then go to the trial (`_vision_capture_active` is true), and the vision-only session silently stops accumulating while still showing "recording".

**Required work:** add the symmetric guard (and the same for `training_start`).

## MEDIUM (test quality)

### M13 (test) — Report fixtures encode the wrong flag shape and hide C1
`tests/test_vision_only_report.py:59` uses `'flags': ['pain']`; `tests/test_vision_report_v2.py:49` the same; `:281` uses `'flags': {'pain': 2}`. None uses the worker's boolean dict. This is why C1 passes the suite.

### M14 (test) — No test uses a non-square frame
`tests/test_vision_pose_math.py:69-83` builds all angle fixtures on an isotropic coordinate grid (e.g. `[0.40,0.50] ... [0.50,0.70]`); no test passes frame width/height. The aspect bug (H1) is invisible by construction.

### M15 (test) — No test drops pose in a plate trial; one test asserts the wrong 100 %
`tests/test_vision_service.py:232-240` only asserts frame counts; `tests/test_vision_events_api.py:297` asserts `summary['pose_coverage_pct'] == 100.0` on an all-pose fixture. Nothing exercises a mixed-coverage plate trial, so C2 is unguarded.

### M16 (test) — The MDC transfer is asserted as correct
`tests/test_vision_norms.py:209-212` asserts `mdc_for('single_leg_cop_velocity') == 0.30` and `mdc_for('single_leg_cop_area') == 0.50` — pinning the single-leg-to-everything mapping in M1 rather than testing that a double-leg measure refuses it.

### M17 (test) — No end-to-end test from stored session to rendered report with audio/face
There is no test that creates a session with per-frame audio/face, stops it, and asserts the report contains a transcript or a face count. Such a test would have caught H3 immediately; the current report tests stop at hand-built aggregate dicts.

## LOW

### L1 — Suite count is quoted inconsistently
`README.md:5` and `:88` say "994 tests"; `VISION-INTEGRATION-SPEC.md:106` says "must keep existing 591 green"; the actual collected/passing count today is **1001** (both pytest and unittest). Update the README and drop the stale spec number.

### L2 — The with-plate SLS test says 3 trials and "three trials per side"
`vision/guide.py:306-313`: `'trials': 3` but "(SLS) ... Hold up to 30 s; three trials per side, left then right." That is six trials; the UI shows three. Correct the count or split the test by side.

### L3 — "Mean camera rate" is the processing rate, and "pose latency" is inference+encode only
`worker.py:1337-1342` computes fps from processed frames (capped by `--fps-limit`, default 60), not from the camera. `vision_report.py:812` labels it "Mean camera rate"; `:827` labels `latency_ms` "Pose latency", but that timer starts before detection and ends after JPEG encode (`worker.py:1246`, `:1354`), excluding capture/driver age. Rename both or measure the camera timestamp.

### T5 — The UI flag test uses shapes the worker never sends
`tests/test_vision_ui_v2.py:368-379` asserts `sttFlags` on `flags: ['clipped','near_field']` and `flags: 'clipped, near'`; the worker sends `{'pain': bool, 'comprehension': bool, 'stop': bool, 'matches': [...]}`. The test passes while the panel is permanently empty.
## Verdict

The vision lane is honest in its prose and careful in its plumbing — no diagnosis, no camera COP, no raw audio or face mesh, guarded paths, escaped reports, a green 1001-test suite. But it is **not safe to use tomorrow with a camera plugged in**. The numbers a clinician would actually read are compromised in three independent ways: plate-trial pose coverage is hard-coded to 100 %, every angle is aspect-distorted on a normal 16:9 camera, and the audio/face evidence the v2 report promises is either absent or, once wired, fabricated by `_absorb_flags`. The COP–COM block is filtered asymmetrically and hidden unless a camera ran, and the battery has no per-test segmentation, so the report cannot say which test produced which row. Fix C1/C2/H1/H3/H6 first; they are the difference between "research aid" and "plausible-looking wrong numbers".

**Counts:** CRITICAL 2 · HIGH 10 · MEDIUM 17 · LOW 4 (33 findings).
