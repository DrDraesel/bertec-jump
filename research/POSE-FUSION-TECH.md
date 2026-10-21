# Camera Pose Fusion with the Bertec BP5046 — Technical Survey and Recommended Stack

**Status:** research / decision document · 2026-10-21
**Scope:** add local, real-time camera pose tracking to the existing 1 kHz Bertec balance app; no cloud, Windows 11, Python 3.11.
**Method:** vendor docs, peer-reviewed validation studies, SDK headers shipped in this repo (`vendor/bertecif.h`, Device SDK 2.57), plus a latency benchmark run on this machine.

---

## 0. Bottom line

Build the fusion around **two synchronized 1080p60 RGB cameras, MediaPipe Pose Landmarker (Full) on CPU for v1, calibrated 2D→3D triangulation, and a Bertec-clocked LED sync marker**. Keep **RTMPose-m (rtmlib + ONNX Runtime GPU)** as the accuracy tier if MediaPipe's joint angles fail the acceptance tests. **Do not buy a depth camera for this** — Azure Kinect is retired and its body tracking is built on CUDA 11.x, which does not support this machine's RTX 5070 Ti (Blackwell); the Femto Mega is the same k4abt algorithm behind a wrapper, at 30 Hz and extra licensing/cost.

Why: single-camera MediaPipe knee angles show **7.07° MAE with a −5.12° bias** in functional tasks and **ICC 0.136 during single-leg stance** — the exact task this lab cares about [12]. Two-view triangulation does not magically fix that (MAE 9.28° static / 12.92° dynamic in one 2026 validation [13]), so the deliverable must be framed as **sway/kinematics context, not clinical joint-angle measurement**, and the sync/QC must be honest.

---

## 1. Requirements this document is optimizing for

| Requirement | Value | Consequence |
|---|---|---|
| Tasks | quiet stance (EO/EC), single-leg stance, low-speed reaching, occasional jump landing | 30 Hz is enough for sway; 60 Hz preferred; 120 Hz only for impact work |
| Measures wanted | trunk/pelvis sway, hip/knee/ankle angles, frontal-plane knee position, movement onset | 2D angles are projection-sensitive; 3D needs ≥2 views or depth |
| Plate stream | BP5046 via AM6147-E amplifier, 1000 Hz, 8 kHz hardware timestamp (0.125 ms ticks), 5000-frame buffer [1] | plate clock is the reference; never let vision block it |
| Cameras | 1–3, consumer USB (UVC), Windows | no genlock, rolling shutter, driver buffering |
| Compute | Ryzen 9 9900X (24T), 66 GB RAM, RTX 5070 Ti 16 GB, Python 3.11.15 | CPU inference is plentiful; GPU helps only RTMPose/YOLO |
| Constraints | fully local, no per-seat licences, no AGPL contamination | rules out Ultralytics for a distributed product |

---

## 2. Comparison table

Latency column: **measured** values are from this machine (9900X, MediaPipe 1.0.1, single 1000×667 image, 40 frames after warm-up, `RunningMode.VIDEO`); other values are vendor/paper figures.

| Tool | 2D/3D | Accuracy evidence | Latency | Windows + Py 3.11 | License | Verdict for this lab |
|---|---|---|---|---|---|---|
| **MediaPipe Pose Landmarker – Full** | 2D + up-to-scale 3D | Knee MAE 7.07°, bias −5.12°, static RMSE 15.49°, SLS ICC 0.136 [12]; 2-view stereo MAE 9.28°/12.92° [13] | **7.2 ms** (VIDEO), **11.9 ms** (IMAGE) measured | `pip install mediapipe` — verified 1.0.1 on 3.11 | Apache-2.0 [3] | **Primary v1** — fastest to integrate, already proven in sibling app |
| MediaPipe – Heavy | 2D + up-to-scale 3D | Same family; lower landmark jitter, no published clinical gain | **23.1 ms** (VIDEO), 27.5 ms (IMAGE) measured | same | Apache-2.0 | Single-camera accuracy option; 2 cams ≈ 21 fps serial |
| MediaPipe **world landmarks** | 3D | Model card: Z “not metric but up to scale”; metric depth explicitly out of scope [3] | included above | same | Apache-2.0 | **Do not use as metric 3D** |
| **RTMPose-m** (rtmlib / MMPose) | 2D (17/26/133 kpts) | 75.8 AP COCO; 77.2 AP with GT boxes [5]; Pose2Sim (RTMPose→OpenSim) reports 2–6° joint-angle error [9] | 90+ FPS CPU i7-11700 pipeline [5]; est. 10–15 ms/frame here, <5 ms on 5070 Ti | rtmlib: no mmcv/mmdet, `pip install rtmlib onnxruntime` [6] | Apache-2.0 | **Accuracy tier** — best evidence-to-effort ratio for 2-view 3D |
| RTMPose-l / RTMW | 2D | 77.2 AP (GT boxes); whole-body 133 kpts | ~2× RTMPose-m cost | same | Apache-2.0 | Only if hands/feet detail needed |
| **YOLOv8-pose / YOLO11-pose / YOLO26-pose** | 2D | COCO pose mAP 57.2–71.6 (n→x, 640 px) [7] | 40–565 ms CPU ONNX; 1.8–12.2 ms T4 [7] | `pip install ultralytics` | **AGPL-3.0** [8] | **Avoid** — licence risk, no clinical validation, 17 kpts |
| **MoveNet** Lightning/Thunder | 2D | 17 kpts; vendor claims only (>50 / >30 FPS) [10] | fast on CPU | TF/TFLite stack; GPU on native Windows deprecated | Apache-2.0 | No advantage over MediaPipe here |
| BlazePose GHUM | 2D + GHUM 3D | Research basis of MediaPipe; same scale caveat [3] | ≈ MediaPipe | via MediaPipe | Apache-2.0 | Reference only |
| **Azure Kinect DK + k4abt** | **3D** | 32 joints; 10–60 mm joint-centre error vs Vicon; 7.2–32.3° lower-limb angle RMSE [15] | 30 Hz max (depth rate); GPU mode required | C++/C# SDK; Python only via unofficial wrappers (pyKinectAzure, unmaintained) | Microsoft EULA; **SDK retired 2024-08-17** [14] | **No** — dead platform, CUDA 11.x vs Blackwell |
| **Orbbec Femto Mega + k4abt** | **3D** | same k4abt algorithm; ToF depth error <11 mm + 0.1% [16] | 30 Hz body; RGB 1080p30 max, 4K only 25 fps [16] | Orbbec SDK + K4A Wrapper; Windows 10/11 x64 | Orbbec EULA (custom); body tracking historically licence-gated [17] | **No** for v1; consider only if depth is mandated |

**Reading the table:** the only rows with both credible clinical validation *and* a clean Windows/Python path are MediaPipe and RTMPose. The depth rows buy metric 3D but at 30 Hz, with 1–3 cm joint error and a dead/awkward SDK stack.

---

## 3. Evidence detail

### 3.1 MediaPipe Pose Landmarker
- Model bundle is a detector (224×224) plus a landmarker (256×256) producing **33 landmarks**; lite/full/heavy are 3/6/26 MB [3].
- Model card FPS on a Pixel 3: lite 44 CPU / 49 GPU, full 18 / 40, heavy 4 / 19 — i.e. heavy is a desktop-only proposition [3].
- **Critical caveat:** the card says the Z coordinate is “not metric but up to scale”, obtained by fitting GHUM to 2D annotations, and lists “applications requiring metric accurate depth” as out of scope [3]. The Tasks API docs describe world landmarks as 3D; in practice they are up-to-scale. Triangulate 2D instead.
- Clinical numbers are sobering and task-dependent: a 2026 **preprint** validation of BlazePose Heavy found dynamic knee ICC 0.982 but **MAE 7.07° with a systematic −5.12° bias**, static RMSE **15.49°**, and **ICC 0.136 for single-leg stance** [12]. Test–retest bias was not stable (ICC 0.094), so a one-off calibration offset does not fix it [12].
- Multi-view MediaPipe is better but not marker-grade: best stereo geometry (~45° separation) gave **MAE 9.28° static, 12.92° dynamic**; baselines >90° were poor (ICC <0.40) [13]. That last point directly constrains camera placement (§6).

### 3.2 RTMPose
- RTMPose-m: **75.8 AP COCO**, 90+ FPS on an i7-11700 CPU, 430+ FPS on a GTX 1660 Ti; RTMPose-l: 77.2 AP with GT boxes [5].
- It is the default 2D estimator in Pose2Sim, whose peer-reviewed accuracy is **2–6° joint-angle error** for multi-view + OpenSim IK [9]. That number belongs to the *pipeline*, not the 2D model alone — do not quote it for a single camera.
- `rtmlib` removes the mmcv/mmdet build pain: pure ONNX Runtime/OpenCV, CPU or GPU, Apache-2.0 [6]. This is the only reason RTMPose is practical on Windows/Python 3.11.
- Weakness for this lab: no balance-specific validation, and COCO-17 has no foot landmarks (heel/toe), which matters for contact events. Pose2Sim's Halpe-26 variant adds feet.

### 3.3 YOLO-pose
- Competitive COCO numbers (YOLO26-pose 57.2–71.6 mAP50-95) and excellent GPU latency, but CPU latency is 40–565 ms [7].
- **Licence:** all Ultralytics models are AGPL-3.0 by default; internal use is tolerable, distribution or network service triggers source-release obligations [8]. For a lab product this is an avoidable legal review.
- 17 keypoints, no 3D, no clinical validation in balance.

### 3.4 MoveNet
- 17 keypoints, MobileNetV2+FPN, 192/256 px input; vendor claims >50 FPS (Lightning) and >30 FPS (Thunder) on laptops [10].
- TensorFlow/TFLite dependency on Windows is heavier than MediaPipe's wheel, GPU support on native Windows is deprecated, and accuracy is below MediaPipe Full in most independent comparisons. No reason to choose it.

### 3.5 Depth body tracking (why not)
- **Azure Kinect DK:** SDK support ended **2024-08-17** [14]. k4abt returns **32 joints** [18], runs at depth rate (30 Hz), and on Windows defaults to **DirectML**; CUDA and TensorRT modes were built against CUDA 11.x [19], which predates Blackwell (RTX 50-series). Expect CUDA/TensorRT to fail on the 5070 Ti; DirectML or CPU are the fallbacks, and CPU mode is “much slower” per the SDK docs [19].
- Accuracy is not better than good triangulation: 10–60 mm joint-centre error vs Vicon, 7.2–32.3° lower-limb angle RMSE depending on joint/task [15].
- **Joint set and frames:** k4abt is a pelvis-rooted **32-joint** skeleton — pelvis, spine navel/chest, neck, clavicles, shoulders, elbows, wrists, hands, hips, knees, ankles, feet, head, nose, eyes, ears [18]. Positions are millimetres in the **depth-camera** frame and must be transformed to colour via the device calibration; the joint definitions differ from MediaPipe/COCO, so angles are not interchangeable without an explicit mapping.
- **Running RGB alongside body tracking:** the Azure Kinect's colour camera is UVC-compatible and can be opened without the Sensor SDK, but the device is single-owner — once k4a has it open, a second process (e.g. `cv2.VideoCapture`) cannot stream from it. Capture colour from the same `k4a_capture_t` that carries the depth frame. High-resolution colour plus depth concurrently can also exceed USB3 bandwidth, so validate the exact mode combination rather than assuming 4K30 + NFOV30.
- **Femto Mega:** Orbbec's own docs state that the AKDK skeleton algorithm is only available through the **Orbbec SDK K4A Wrapper** [20] — i.e. it is k4abt again. Depth 640×576 NFOV at 30 fps, range 0.5–3.86 m; RGB maxes at 1080p30 (4K at 25 fps) [16]. Windows 10/11 supported; body tracking wants an NVIDIA GTX 1050+ [21]. Licensing is a custom Orbbec EULA, and Orbbec's body-tracking licence has historically been time-limited/paid on other products [17] — verify before purchase.
- **Conclusion:** depth buys metric Z but costs 30 Hz, 1–3 cm joint error, a dead or awkward SDK, and money. For balance, two RGB views + calibration is the better trade.

---

## 4. Synchronization: what is actually available here

### 4.1 The plate side (verified in this repo)
`vendor/bertecif.h` (Device SDK 2.57) and `bridge.py` establish:

- Samples at **1000 Hz**; each frame carries a **64-bit hardware timestamp** that ticks at **8 kHz** (0.125 ms) on this device, plus a monotonic `frameCounter` [1].
- The SDK exposes **SYNC and AUX pins**. `SYNCPINMODE_INTCLOCK` makes one device generate a **1 kHz square wave with a reference mark every 2000 ms**; `SYNCPINMODE_EXTCLOCK`/`EXTUNCONTROLLED` lets an external clock drive acquisition; `SYNCPINMODE_START_PULSE_ON` starts the stream on an edge [1].
- Current code starts the stream with the default control struct, i.e. **sync pins unused** (`syncPinMode = 0`) [2].
- External clocking disables averaging, low-pass filtering and multi-plate sync, and resampling introduces delay [1] — so use the pins only for *marking*, not for changing the 1 kHz acquisition itself.

That reference mark every 2 s is the key asset: it gives a periodic, unambiguous anchor for camera alignment and drift measurement without touching the force path.

### 4.2 Camera side
Consumer UVC cameras cannot be externally triggered. Their frame timestamps are assigned by the driver, typically **1–3 frames after exposure** depending on buffering (*est.*), and they are **rolling shutter** (row-by-row readout, ~5–15 ms skew at 1080p). MediaPipe's `detect_for_video` requires a monotonically increasing `timestamp_ms`; feeding capture time makes the output inherit the correct anchor, but does not remove driver latency [4].

### 4.3 Sync tiers and expected residual offset

Figures marked *est.* are engineering estimates to be measured, not published constants.

| Tier | Mechanism | Expected residual offset | Jitter | When to use |
|---|---|---|---|---|
| **T0** | Host `perf_counter_ns()` at `read()` for both streams | 10–60 ms *est.*, unmeasured | 1–2 frames | Never for published data; debug only |
| **T1** | **LED marker** driven from Bertec SYNC_OUT (1 kHz + 2 s reference mark), onset detected per frame | ±0.5 frame: **±8.3 ms @60 fps, ±4.2 ms @120 fps**; sub-frame interpolation → median 2–5 ms *est.* | ±3–5 ms *est.* | **Recommended default** |
| **T2** | **Event-based**: Fz rise vs heel/toe kinematics (Pose2Sim/OpenCap style) | **10–30 ms systematic** *est.*, because “heel contact” and “Fz rise” are different physical events [11][9] | ±1 frame | Cross-check / no-marker fallback |
| **T3** | **Hardware trigger**: Bertec clock → machine-vision camera trigger; or camera clock → Bertec EXTCLOCK | **<1 ms** (one 1 kHz tick) + fixed camera latency | µs–ms | Only if buying global-shutter cameras |

Published practice: OpenCap aligned 60 Hz phone video to force plates with a heel-event algorithm, synchronizing **92.5% of 330 trials**, with r = 0.85/0.81 and normalized RMSE <0.17 vs hardware-synchronized OptiTrack [11]. Pose2Sim instead cross-correlates mean vertical keypoint speed across views and finds the lag that maximizes correlation [9]. Neither reports a millisecond residual, which is exactly why this lab should measure and report its own.

### 4.4 Recommended sync design (v1)

1. Configure the plate as **SYNC master** (`SYNCPINMODE_INTCLOCK`, 1 kHz) and drive a small **IR LED** in the camera's field of view from the SYNC line, with a brighter pulse on the 2 s reference mark. Firmware must support the mode (`bertec_CanStartDataStream` will reject it otherwise) [1].
2. Record `t_plate` from the **8 kHz device timestamp**, never from arrival time. Convert to seconds with `ticks / 8000.0` [1].
3. Record `t_frame` as `perf_counter_ns()` immediately after `cap.read()`, and store `frame_index`, `t_frame`, `t_infer_done`, and the MediaPipe `timestamp_ms` actually submitted.
4. After each session, detect LED onsets (max intensity in a small ROI), fit `t_frame = a·marker_index + b`, and solve the offset against the plate's reference-mark times. Report **median offset, IQR, max |offset|, and drift in ppm**.
5. If drift exceeds ~5 ms over 10 min, re-anchor at every 2 s mark (the marks make this free).

### 4.5 How to report sync quality
Write `sync_qc.json` per session:

```json
{"method":"led_sync_v1","markers":30,"median_offset_ms":3.1,"iqr_ms":1.8,
 "max_abs_ms":7.4,"drift_ppm":12,"camera_fps":59.8,"frames_dropped":2,
 "plate_gaps":0,"qc":"pass"}
```

Acceptance: **median ≤ 8 ms, max ≤ 1 frame, zero plate gaps, camera fps ≥ 0.98 × nominal**. For jump/landing analysis, require T3 or 120 fps + T1 and state the residual in the report. For quiet stance a 10 ms offset is physically negligible — do not over-engineer it there.

---

## 5. Reusable open-source work

| Repo / project | License | What to take | Verdict |
|---|---|---|---|
| **Pose2Sim** (`perfanalytics/pose2sim`) | BSD-3-Clause [22] | `synchronization.py` cross-correlation logic; calibration/triangulation conventions; OpenSim IK config; validation numbers to quote | **Take** — closest match to the offline pipeline |
| **Sports2D** (`davidpagnon/Sports2D`) | BSD-3-Clause [22] | Real-time single-camera 2D angles; filter defaults; jitter handling | **Take** as code reference for live angle math |
| **OpenCap core / processing** | Apache-2.0 code [23], but service ToS is **non-commercial academic only** [24] | 2D→triangulate→OpenSim architecture; sync-by-event paper [11] | **Concepts only** — cloud/GPU-heavy, ToS restricts service use |
| **OpenSim** | Apache-2.0 [25] | Inverse kinematics, scaled models, joint-angle definitions | **Use offline**, separate conda env (pip wheels are unreliable) |
| **MMPose / rtmlib** | Apache-2.0 [6] | RTMPose-m/l ONNX models, preprocessing, OneEuro smoothing | **Use** if the RTMPose tier is triggered |
| **MediaPipe** | Apache-2.0 [3] | Pose Landmarker `.task` bundles; VIDEO-mode tracking | **Use** for v1 |
| **NeuroKit2** | MIT [26] | Filtering, entropy, detrended fluctuation for COP/sway series | Optional, low risk |
| **Theia3D** | Commercial | Reference benchmark: 8 synchronized cameras, pelvis COM vs gravity line r = 0.89–0.99, RMSD <0.13 cm [27] | **Do not buy** — 6–8 camera minimum [28], overkill for one plate |

**Do not reuse:** OpenPose-based pipelines (non-commercial custom licence, heavy dependency), Ultralytics code (AGPL), Azure Kinect samples (dead SDK).

---

## 6. Recommended architecture for this machine

```
┌─────────────── plate process (existing service, unchanged priority) ───────────────┐
│ Bertec SDK poll loop @1 kHz → ring buffer (timestamp ticks, FZ, COP) → disk writer │
│ optional: SYNC_OUT → LED driver (1 kHz + 2 s mark)                                 │
└───────────────────────────────┬────────────────────────────────────────────────────┘
                                │ shared monotonic clock (perf_counter_ns)
┌───────────────────────────────▼────────────────────────────────────────────────────┐
│ camera process A (side/oblique)          camera process B (front/oblique)          │
│ cap.read() → t_frame → bounded queue(2) → MediaPipe VIDEO worker → landmarks+conf   │
└───────────────────────────────┬────────────────────────────────────────────────────┘
                                │ frames + timestamps (JSONL/parquet)
                     ┌──────────▼──────────┐
                     │ alignment + fusion  │  LED/event offset, 2-view triangulation,
                     │ (offline or live)   │  angles, trunk sway, QC report
                     └──────────┬──────────┘
                                ▼
                 session bundle: plate.parquet · pose.jsonl · sync_qc.json · report.html
```

### 6.1 One camera or two?
**Two.** One camera gives 2D angles only in the plane perpendicular to the optical axis; frontal-plane knee motion and out-of-plane trunk sway are invisible or biased. But place them **~60° apart, not 90°**: the 2026 stereo validation found ~45° optimal and >90° poor (ICC <0.40) [13]. A front-left + front-right pair at ±30° from the plate normal is a better compromise than pure front+side. If a third camera is available, put it at 90° to the side for sagittal angles.

### 6.2 Which model, where
- **v1:** MediaPipe **Full**, `RunningMode.VIDEO`, one process per camera, CPU. Measured **7.2 ms/frame** here → two cameras at 60 fps need ~14.4 ms/frame of CPU, which fits easily on 24 threads. Full is 3.2× faster than Heavy and the accuracy difference is not documented as clinically meaningful.
- **Escalation:** if knee-angle MAE vs goniometer exceeds 8°, or landmark jitter at the knee exceeds ~1 cm at 3 m, switch the 2D stage to **RTMPose-m via rtmlib on the 5070 Ti** (CUDA 12.x onnxruntime-gpu), keeping the same triangulation. Do not run two different models on the two views — keypoint definitions must match.
- **MediaPipe's Python wheel is effectively CPU-only**: GPU delegates are exposed for Android/iOS/C++/web, not in the Python Tasks API. Plan CPU inference and keep the GPU free for RTMPose or nothing. Pin MediaPipe to ~4 threads per camera; TFLite/XNNPack scales with cores and two unconstrained instances will fight the plate process for scheduling.

### 6.3 Frame-rate targets
| Task | Camera | Inference | Rationale |
|---|---|---|---|
| Quiet stance | 60 fps | 30–60 Hz | sway power is <1 Hz; 30 Hz is sufficient, 60 helps velocity |
| Single-leg stance / reaching | 60 fps | 60 Hz | faster transient, occlusion changes |
| Jump landing | 120 fps (if camera supports) | 60 Hz | impact peak is 10–30 ms wide *est.*; 60 fps smears it |
| Sync marker | — | — | 2 s reference mark, ≥30 markers/session |

### 6.4 Failure modes and isolation
| Failure | Detection | Mitigation |
|---|---|---|
| Camera unplug / driver reset | `cap.read()` False, or fps → 0 | mark gap, exponential reconnect, never raise into plate loop |
| Dropped frames | `frame_index` discontinuity vs `t_frame` | log per-frame; interpolate landmarks only for display, never for metrics |
| Inference stall | queue depth > 2 | drop oldest frame; capture thread never blocks |
| GIL / CPU contention | plate poll jitter | separate processes, plate process affinity to 2–4 cores, no GPU work in it |
| USB bandwidth | camera opens at lower fps | plate on its own USB controller; cameras on another; MJPEG 1080p60 |
| Rolling-shutter skew | vertical marker timing | acceptable for sway; note it for landing; global-shutter camera for T3 |
| Clock drift | marker regression residual | re-anchor every 2 s mark; report ppm |

**Hard rule:** the plate acquisition path must not import cv2/mediapipe, must not share a process with inference, and must keep its existing 5000-frame buffer and `-4` overflow abort [2].

---

## 7. Honest accuracy expectations (single RGB, balance context)

| Measure | Realistic expectation | Why |
|---|---|---|
| Sagittal knee/hip angle, one lateral camera | **5–10° MAE**, systematic bias 3–6° | 7.07° MAE / −5.12° bias measured for BlazePose Heavy [12]; 2–6° only with multi-view + IK [9] |
| Frontal-plane knee (valgus) | **Poor; do not report clinically** | 2D projection ambiguity; SLS ICC 0.136 [12] |
| Ankle angle | Worse than knee (foot landmarks noisy, shoes) | MediaPipe foot landmarks are the least reliable |
| Trunk/pelvis sway (relative) | Usable after 2–5 Hz low-pass; ±0.5–1 cm *est.* absolute | landmark jitter ~2–5 px → ~4–9 mm at 3 m/1080p |
| COM from landmarks | ±2–5 cm *est.*; COP–COM difference unreliable | segment mass estimates + landmark bias |
| COP / GRF from camera | **Not measurable** | requires the plate; camera adds kinematics only |
| Depth camera joint angles | 7–32° RMSE depending on joint [15] | not better than 2-view triangulation |

Consequences for reporting: label camera-derived angles as **“markerless estimate, ±X° (method)”**, never as clinical goniometry; do not compute a “balance score” from camera alone; use camera to explain *how* the plate's COP moved, not to replace it.

---

## 8. Implementation phases and acceptance tests

**Phase 0 — sync harness (no product code).** LED on SYNC_OUT, one camera, 60 s capture. Acceptance: `sync_qc.json` with median ≤ 8 ms, max ≤ 1 frame, drift < 20 ppm.

**Phase 1 — single-camera kinematics (2 weeks).** MediaPipe Full, side/oblique view, 60 fps. Acceptance: 10 min soak with zero plate gaps; ≥98% frames inferred; knee angle SD during quiet stance < 2°; SLS landmark confidence > 0.7 for hip/knee/ankle on the stance leg.

**Phase 2 — two-camera triangulation.** Calibrate with a checkerboard or wand; verify reprojection error < 1 px. Acceptance: 3D knee angle vs goniometer MAE ≤ 8° on 5 subjects × 3 tasks; test–retest ICC ≥ 0.75 for trunk RMS sway.

**Phase 3 — optional dynamic work.** 120 fps camera + T3 trigger if landing impact timing matters; otherwise keep 60 fps and report the residual.

---

## 9. Risks / things that will not work as advertised

- **Azure Kinect:** hardware discontinued, SDK retired 2024-08-17 [14]; k4abt CUDA/TensorRT builds are CUDA 11.x-era and will not use a Blackwell GPU [19]. Python bindings are unofficial. Do not start here.
- **Femto Mega:** works on Windows, but body tracking is k4abt through the K4A wrapper [20], capped at 30 Hz, with a custom EULA and a history of licence-gated body tracking on Orbbec products [17]. Budget and legal review before purchase.
- **MediaPipe 3D world landmarks:** up-to-scale per the model card [3]; treating them as metres will silently corrupt any 3D angle.
- **Ultralytics:** AGPL-3.0 [8]. Fine for internal experiments, a problem for a shipped app.
- **MMPose full stack:** mmcv/mmdet on Windows + Python 3.11 is a build project; use rtmlib instead [6].
- **OpenSim:** conda-only in practice; pin a separate environment, do not put it in the plate venv.
- **OpenCap service:** code is Apache-2.0, but the hosted service terms are non-commercial academic use [24] — do not build a product on the service.

---

## 10. Sources

1. Bertec Device SDK 2.57, `bertecif.h` and `ExternalClockingFZReaderExample.cpp`, shipped in `vendor/` (2026) — 8 kHz timestamp, SYNC/AUX pin modes, 1 kHz + 2 s reference mark. Local file.
2. `bridge.py`, this repo (2026) — 1000 Hz stream, `timestamp_hz=8000`, `StreamControl` defaults (sync pins unused), 5000-frame buffer.
3. Google, *BlazePose GHUM 3D model card* (2021) — https://storage.googleapis.com/mediapipe-assets/Model%20Card%20BlazePose%20GHUM%203D.pdf
4. Google, *Pose landmark detection guide (Python)* (2026) — https://developers.google.com/edge/mediapipe/solutions/vision/pose_landmarker/python
5. Jiang et al., *RTMPose: Real-Time Multi-Person Pose Estimation based on MMPose*, arXiv:2303.07399 (2023) — https://arxiv.org/abs/2303.07399
6. `rtmlib` (Apache-2.0) — https://github.com/Tau-J/rtmlib ; MMPose model zoo — https://github.com/open-mmlab/mmpose/blob/main/projects/rtmpose/README.md
7. Ultralytics, *Pose Estimation* docs (YOLO26-pose metrics) (2026) — https://docs.ultralytics.com/tasks/pose/
8. Ultralytics licence (AGPL-3.0) — https://www.ultralytics.com/license
9. Pagnon et al., *Pose2Sim: Part 2 — Accuracy*, Sensors 22(7):2712 (2022) — https://www.mdpi.com/1424-8220/22/7/2712 ; synchronization docs — https://perfanalytics.github.io/pose2sim/
10. Google, *MoveNet.SinglePose model card* (2021) — https://storage.googleapis.com/movenet/MoveNet.SinglePose%20Model%20Card.pdf
11. Pavas Vivas et al., *Synchronization of OpenCap with Force Platforms: Validation of an Event-Based Algorithm*, Sensors 26(2):360 (2026) — https://doi.org/10.3390/s26020360
12. Yue et al., *Validation of Single-Camera MediaPipe BlazePose for Knee Joint Angle Measurement*, JMIR preprint 102399 (2026) — https://preprints.jmir.org/preprint/102399
13. Samani et al., *Optimizing camera placement in multi-view markerless motion capture*, BMC Research Notes (2026) — https://doi.org/10.1186/s13104-026-07886-4
14. Microsoft, *Azure Kinect SDK lifecycle* — retirement 2024-08-17 — https://learn.microsoft.com/en-us/lifecycle/products/azure-kinect-sdk
15. *How the Processing Mode Influences Azure Kinect Body Tracking Results*, Sensors 23(2):878 (2023) — https://www.mdpi.com/1424-8220/23/2/878 (citing Albert et al. 10–60 mm; Ma et al. 7.2–32.3°)
16. Orbbec, *Femto Mega Hardware Specifications* (2025) — https://doc.orbbec.com/documentation/Orbbec%20Femto%20Mega%20Documentation/Femto%20Mega%20Hardware%20Specifications
17. Orbbec community, *Regarding Body Tracking license* (2020) — https://3dclub.orbbec3d.com/t/regarding-to-body-tracking-license/2856
18. Microsoft, *Azure Kinect body tracking joints* (32-joint enum) — https://learn.microsoft.com/en-us/previous-versions/azure/kinect-dk/body-joints
19. Microsoft, *Body Tracking SDK download / change log* (v1.1.0 CUDA 11.1; v1.1.2 last) — https://learn.microsoft.com/en-us/azure/kinect-dk/body-sdk-download
20. Orbbec, *Orbbec SDK and K4A Wrapper (Femto Mega)* (2025) — https://doc.orbbec.com/documentation/Orbbec%20Femto%20Mega%20Documentation/Orbbec%20SDK%20and%20K4A%20Wrapper%20(Femto%20Mega)
21. Orbbec, *System Requirements (Femto Mega)* (2025) — https://doc.orbbec.com/documentation/Orbbec%20Femto%20Mega%20Documentation/System%20Requirements%20(Femto%20Mega)
22. Pose2Sim / Sports2D licences (BSD-3-Clause) — https://github.com/perfanalytics/pose2sim/blob/main/LICENSE
23. OpenCap Core (Apache-2.0) — https://github.com/opencap-org/opencap-core
24. OpenCap terms (non-commercial academic use) — https://www.opencap.ai/terms-conditions
25. OpenSim Core (Apache-2.0) — https://github.com/opensim-org/opensim-core
26. NeuroKit2 (MIT) — https://github.com/neuropsychology/NeuroKit
27. Wijekoon et al., *Concurrent validity and test–retest reliability of markerless motion capture for static postural control*, J Biomech 204:113371 (2026) — https://doi.org/10.1016/j.jbiomech.2026.113371
28. Theia3D camera requirements (minimum 6, recommend 8) — https://docs.theiamarkerless.com/theia3d-documentation/camera-system-requirements

**Local measurements (this document):** MediaPipe 1.0.1 in `projects/runtrack/.venv` (Python 3.11.15), 40 frames after 5 warm-up, 1000×667 source: Full 7.19 ms VIDEO / 11.93 ms IMAGE; Heavy 23.05 ms VIDEO / 27.52 ms IMAGE; 720p identical (7.28 / 23.14 ms), confirming cost scales with detector + 256×256 ROI, not capture resolution. Caveat: a repeated static image exercises the tracker path, so real footage with re-detection every N frames will average somewhat higher; IMAGE-mode figures are the worst case.
