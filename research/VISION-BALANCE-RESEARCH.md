# What Camera-Based Pose Estimation Can Validly Measure During Standing Balance and Single-Leg Stance

**Scope.** Evidence review for a Bertec BP5046 force plate (Fz/Mx/My → COP at 1000 Hz) plus one or two cameras, covering double-leg, single-leg and mCTSIB-style stances and weight-bearing squats. This is the *scientific validity* companion to `POSE-FUSION-TECH.md` (stack, sync, latency) and `VISION-INTEGRATION-SPEC.md` (architecture). It does not repeat their engineering content.

**Evidence grades.** **A** — replicated concurrent validation or prospective clinical evidence; safe to report and act on. **B** — one good validation, small n, or validation in an adjacent task; report with the error bound attached. **C** — mechanically impossible on this hardware, or published accuracy worse than the effect being measured. Where a value has not been published, this document says so rather than estimating.

---

## 1. The measurement problem: two instruments, two different variables

A force plate measures the **centre of pressure (COP)** — the point of application of the ground reaction force. A camera measures **kinematics** — segment positions and angles. Neither measures the other's variable. The body's **centre of mass (COM)** is the controlled variable; the COP is the control variable that moves underneath it to keep it over the base of support (Winter 1995; Winter et al. 1998). This distinction is not academic: COP amplitude always exceeds COM amplitude, COP contains higher-frequency content, and the two are close to *in phase* in quiet standing (within 6 ms in Winter et al. 1998). Any pipeline that treats camera-derived COM as a substitute for COP is measuring a different thing, with different reliability.

**Consequence for reporting:** the plate answers *"how much and how fast did the control point move?"*; the camera answers *"which body segments moved, and in what coordination pattern?"* The camera's job is to explain the plate, not to replace it.

---

## 2. Force-plate metrics and which ones survive a reliability test

### 2.1 Definitions and caveats

| Metric | What it captures | Main caveat |
|---|---|---|
| Path length (mm) | Total COP excursion, summed sample-to-sample | Most sensitive to sampling rate and filtering of all COP metrics; inflated by high-frequency noise (Koltermann et al. 2019) |
| Mean velocity (mm/s), resultant and AP/ML | Rate of corrective movement | The most consistently reliable summary measure (Ruhe et al. 2010; Lin et al. 2008) |
| RMS / SD of COP (mm) | Spatial dispersion about the mean position | Less contaminated by noise than path length |
| 95% confidence ellipse area (mm²) | Postural dispersion, covariance-scaled | "Sway area" is defined differently across papers; implementations are not interchangeable (Quijoux et al. 2021) |
| Range (AP, ML) | Peak-to-peak excursion | Rests on two data points; explicitly discouraged as an outcome (Palmieri et al. 2002) |
| Mean / median power frequency (Hz) | Where sway power sits — a strategy signature, not an amount | Weak test–retest reliability (Lin et al. 2008) |
| Band-limited power (<0.5, 0.5–2, >2 Hz); sample entropy; diffusion | Strategy signature, regularity, loop structure | Band edges not standardised; entropy depends on m, r and filtering; no ICC/MDC in sources reviewed |

Empirical justification for caring about the frequency split: in 133 older adults, mean PSD energy above 2 Hz was **0.05–0.26 cm²** versus **3.67–4.23 cm²** below 0.5 Hz (Quijoux et al. 2021). Quiet-standing sway is a low-frequency phenomenon; almost nothing of interest happens above 2 Hz.

### 2.2 Reliability: what the numbers actually say

| Metric | Reliability reported | Source |
|---|---|---|
| Mean velocity | Review of 32 studies: r = 0.32–0.94, no measure superior (Ruhe 2010); within-day ICC 0.86–0.95, SEM 1.0–2.3 mm/s (Lin 2008); between-day ICC 0.85–0.92, SEM 0.32–0.68 mm/s, MDC 0.89–1.88 mm/s (Lo 2022) | Ruhe 2010; Lin 2008; Lo 2022 |
| Sway area (95% ellipse) | Within-day ICC 0.79–0.92, SEM 10.5–19.4 | Lin et al. 2008 |
| RMS distance | Within-day ICC 0.80–0.82, SEM 1.0–1.3 mm | Lin et al. 2008 |
| Mean distance | ICC 0.56–0.75 within-day; SEM 0.75–1.40 mm; MDC 2.07–3.89 mm | Lo et al. 2022 |
| Mean power frequency | ICC 0.41–0.77 within-day | Lin et al. 2008 |

**The device and the task matter as much as the metric.** On a portable low-cost plate (n = 27, two sessions 7–10 days apart), bipedal stance was poor-to-moderate across *all* parameters (**ICC 0.39–0.65**), with ellipse-area CV exceeding 60%, while single-leg total displacement and velocity reached **ICC 0.85–0.90 with CV ≈ 8%**; single-leg area and SD were weaker (ICC 0.57–0.79, CV 16–23%) (Vindiš et al. 2026). A Bertec is not that device, but the lesson generalises: **lab-specific ICC and MDC must be computed on this plate, in this lab, before any change score is reported.** The existing `metrics.py::change_exceeds_mdc` gate is the right mechanism; the constants need to come from a local test–retest study, not from the literature.

### 2.3 Trial duration, sampling and filtering

**Duration.** 20–30 s gave optimal test–retest reliability in an early study (Le Clair & Riach 1996); 30 s is the trial length in current single-leg protocols (Kozinc et al. 2025). But RMS *increases* and mean power frequency *decreases* as duration grows from 15 to 120 s, while reliability improves (Carpenter et al. 2001), and Ruhe et al. (2010) concluded that **≥90 s** is needed for acceptable reliability of most COP parameters. Practical compromise: **30 s for velocity-based screening; 60–90 s when area, RMS or frequency are the endpoint.** **Sampling.** The ISPGR standard recommends ≥25 s acquisition and ≥50 Hz sampling (Scoppa et al. 2013); path length keeps changing with sampling rate until ~300 Hz bipedal and ~600 Hz monopedal (Koltermann et al. 2019). At 1000 Hz the plate is heavily oversampled: **acquire at 1000 Hz for auditability, then filter and decimate to 100–200 Hz** — which the current pipeline already does (10 Hz zero-phase Butterworth, decimate to 100 Hz). **Filtering.** Always low-pass; Butterworth is the de facto standard, with 5 Hz (Lin et al. 2008) or 10 Hz (Ruhe et al. 2010) in published protocols. Fix one cut-off for the whole programme — changing it systematically shifts path length and velocity.

### 2.4 Single-leg stance: different scale, different error

Single-leg stance is a harder task, so both sway amplitude and absolute measurement error grow; the practical consequence is that **single-leg MDCs are not interchangeable with double-leg ones and must be estimated per stance** (Kozinc et al. 2025; Vindiš et al. 2026). On a portable plate the contrast was stark: single-leg velocity CV ≈8% and area CV 16–23%, against bipedal area CV >60% (Vindiš et al. 2026). Reliability in single-leg stance: ICC 0.71–0.86 over two sessions 3–5 days apart, with AP velocity best (0.85) and area weakest (0.71, MAPE 20.1%) — plus a **systematic between-session improvement** for velocity (p < 0.001) and area (p = 0.002), i.e. a familiarisation effect that will masquerade as training response (Kozinc et al. 2025). Budget a habituation session.

**Rule:** never apply double-leg MDCs to single-leg data, and never compare ICCs across stances as if they estimated the same error.

---

## 3. What pose estimation has actually been validated to measure

### 3.1 The headline result: camera-derived COP is possible — with multiple views

A multi-view OpenPose system with an 8-segment body model and inverse-dynamics COP estimation was validated against a force plate and marker-based mocap in 12 non-disabled adults across five balance tasks (hard/soft surface × eyes open/closed, plus weight shift):

COM trajectories agreed with the marker-based gold standard at **CC > 0.90 across 1-, 3-, 6- and 8-segment models**; COP agreed with the plate at **CC 0.95–0.98 with RMSE 2.2–8.5 mm in AP** using the 8-segment model; agreement **improved monotonically with segment count**; and AP outperformed ML in every task except weight shift.

Source: Nourbakhsh Sabet et al. 2026, *Ann Biomed Eng* (PMID 42542510). This is the strongest published evidence that a camera pipeline can track a plate during mCTSIB-style tasks — and it is a single study with n = 12, so it is grade **B+**, not A. It also used **multiple calibrated views**, not one webcam.

A related markerless study reported bias of "a few mm" and RMSE ≈1 cm for COM and XCoM position across balance tasks (Chaumeil et al. 2024) — consistent in magnitude.

### 3.2 Single RGB-D camera: usable at group level, not individually

An Intel RealSense D415 + MediaPipe pipeline (16-segment Zatsiorsky–Seluyanov model with de Leva adjustments) against Optotrak during **voluntary directional leans**:

Test–retest **ICC 0.84–0.86** (95% CIs as low as 0.58); bias **−6.6 mm**, SD of differences 15.1 mm, 95% LoA **−36.1 to +22.9 mm**; MAPE **10.5%** against Optotrak path length. Path lengths studied were 140–380 mm, so the LoA width is **~17–30% of the measured range**.

The authors conclude it is a screening tool, not a laboratory replacement, and explicitly note that individual-level interchangeability cannot be assumed (Sonar et al. 2026). Three limitations matter for us: the reference was a **single waist marker**, not a segmental COM; the two systems were **not electronically synchronized** and only maximum path length (not the time series) was compared; and the task was **lean, not quiet standing**.

### 3.3 Trunk and pelvis

Depth-sensor body tracking has been validated for trunk/pelvic motion, with a consistent pattern: **sagittal and AP measures are decent, frontal/ML measures are weaker**. Landmark RMSE against Qualisys across clinical motor tasks was **>20 mm (Kinect v2) and >23 mm (Azure Kinect)**, range 20–89 mm, worst at ankles/feet and in the vertical axis, best at wrists/elbows and in AP; accuracy was **lowest in quiet standing** of all tasks tested, because the movements are small relative to the noise floor (Bertram et al. 2023). The Azure Kinect's added clavicle/spine-chest joints tracked at ≈11.5 mm, and Azure was better for lower-body while Kinect v2 was better for mid/upper body (Albert et al. 2020).

For single-leg stance, Kinect has been validated against 3-D motion analysis in patients with hip disorders (Asaeda et al. 2019) — enough to support *relative* pelvic/trunk inclination comparison, not absolute clinical goniometry.

### 3.4 Joint angles and postural strategy

- **Sagittal-plane 2D pose is usable.** Single-camera OpenPose vs 3-D mocap in gait: MAE **4.0° hip, 5.6° knee, 7.4° ankle** (Stenum et al. 2021). Two-camera OpenPose gave ROM MAE **2.34°** for pelvic obliquity and **4.56–4.70°** for hip ab/adduction
- **Frontal-plane knee valgus is not usable in absolute terms.** MediaPipe vs Vicon during single-leg drop landing: absolute error **18.83–19.68°**, bias **−19.28°**, and absolute-angle correlation only r = 0.590 at 90 ms post-contact (Asaeda et al. 2024). Change from initial contact was better (r ≈ 0.55–0.60) but still weak.
- **Occlusion doubles the error.** In 2D markerless capture, the occluded-side hip and knee had limits of agreement **twice** those of the camera-side limb. Frontal-plane hip/knee bias was −4.6 to 1.6 ± 3.7–4.2° (acceptable), but the **ankle LoA was ±12° — too high for practical use** (Wade et al. 2023).
- **Strategy classification (ankle vs hip)** is conceptually sound — Horak & Nashner (1986) established the ankle/hip continuum, and quiet standing is predominantly an ankle strategy in the sagittal plane (Gage et al. 2004, 14-segment model, 120 s trials). But hip motion is not negligible: the single-inverted-pendulum model is "literally false" though functionally adequate for COM oscillations (Morasso 2019). **A validated camera-based ankle-vs-hip classifier does not exist**; what exists is a defensible proxy (ratio of hip to ankle angular excursion), which must be validated locally against COP–COM phase.

### 3.5 What the camera cannot give

**Per-foot COP in double-leg stance** is impossible — one plate under both feet yields only the resultant COP; separating left/right requires two plates or insoles (contrast Corriveau et al. 2001, who used two AMTI plates plus three OPTOTRAK sensors). **Direct muscle activation** cannot be measured — no camera measures EMG, so "muscle response" is only inferable from kinematics plus COP–COM dynamics. **Joint kinetics** (e.g. knee abduction moment) need 3-D kinematics *and* a resolved per-limb GRF, which one plate cannot supply in single-leg stance. **Absolute ML COM from one camera** is not defensible: monocular 3-D is up-to-scale and ML is the weak axis in every validation cited above.

---

## 4. COP–COM: the control signal, and where the camera earns its place

### 4.1 What it is and why it matters

COP–COM is the algebraic difference between the COP and the COM in the horizontal plane. It is proportional and highly correlated with **horizontal COM acceleration**, and is interpreted as the "error signal" of postural control — a measure of control *effort* rather than control *outcome* (Winter et al. 1998; Lafond et al. 2004; the 2025 *Gait & Posture* short communication on interpreting sway measures). Mechanically, the COP–COM offset is what generates the ankle (sagittal) or hip ab/adductor (frontal) torque that accelerates the COM.

It separates two athletes with identical COM sway but different strategies: one swaying passively with large COP–COM excursions (high effort, low stiffness) and one stiffly holding position.

### 4.2 Reliability

In healthy elderly, single-trial COP–COM ICC was **0.79 (AP) and 0.69 (ML)**; averaging **four trials** raised this to **0.94 (AP) and 0.90 (ML)**, with minimal metrically detectable changes of **0.10 mm (AP)** and **0.16 mm (ML)** (Corriveau et al. 2000). Test–retest and inter-rater reliability in the elderly were confirmed in a follow-up (Corriveau et al. 2001). **Four trials, not one.** The MDC values are sub-millimetre, which means COP–COM is exquisitely sensitive to synchronization error, filtering mismatch and COM model error.

### 4.3 How labs compute COM, and the trap

Three methods have been compared head-to-head across quiet standing, **one-leg standing**, ankle oscillation and ankle+hip oscillation (Lafond et al. 2004):

**Kinematic (segmental)** — the reference standard, needing markers or pose estimation; **gravity-line projection (GLP)** — zero-point-to-zero-point double integration of COP, **comparable to the kinematic method** and attractive clinically because it needs only a force plate; and **COP low-pass filter (LPF)** — cutoff 0.4–0.5 Hz, MSE < 0.1 cm in standing but degrading as dynamics increase (Benda et al. 1994), and **significantly different** from the kinematic method in Lafond's comparison.

**This is the single most useful finding in this section for our lab:** COP–COM can be computed *without any camera* via GLP, and that estimate is comparable to the kinematic gold standard. But the cheap low-pass shortcut fails exactly where our interesting tests are — single-leg stance and foam/eyes-closed conditions.

**So the camera's real job is not to produce COP–COM in double-leg stance.** It is to produce the segmental COM that keeps COP–COM valid when the inverted-pendulum assumption breaks: single-leg stance, foam, and any condition with substantial hip strategy. That is a much narrower — and much more defensible — product claim than "camera measures balance."

### 4.4 Feasibility ladder for COP–COM

| Configuration | COM source | Expected error | Verdict |
|---|---|---|---|
| Plate only | GLP double integration | Comparable to kinematic (Lafond et al. 2004) | **Do this now** |
| Plate + 1 RGB camera | 2D pose + anthropometric model | COM error centimetres; ML unreliable | Internal research only |
| Plate + 2 calibrated cameras | Triangulated 8-segment model | AP COP RMSE 2.2–8.5 mm in the validating study (Nourbakhsh Sabet et al. 2026) | **Target state** |
| Plate + marker-based mocap | 14-segment kinematic | Reference standard (Gage et al. 2004) | Not our budget |

Two practical requirements that the literature makes explicit and that are easy to get wrong: the COM and COP must be expressed in the **same horizontal coordinate frame** (camera frame → plate frame via a calibrated transform), and both must be **filtered identically** before subtraction. A 10 Hz low-pass on COP and a different smooth on the camera stream will manufacture a COP–COM signal that is mostly processing artefact.

---

## 5. Single-leg stance and balance-test kinematics

### 5.1 Constructs that discriminate, and by how much

| Construct (single-leg squat unless noted) | Asymptomatic | Patellofemoral pain | Design |
|---|---|---|---|
| Contralateral pelvic drop | 7.4° ± 3.8° | 10.3° ± 4.7° (p = .003) | Cross-sectional, n = 80 (Nakagawa et al. 2012) |
| Ipsilateral trunk lean | 6.7° ± 3.0° | 9.3° ± 5.3° (p = .012) | Same |
| Hip adduction | 10.8° ± 5.6° | 14.8° ± 7.8° (p < .0001) | Same |
| Knee abduction (3-D) | 5.8° ± 3.4° | 9.2° ± 5.0° (p < .0001) | Same |
| 2-D FPPA, single-leg squat | 8.4° ± 5.1° | 16.8° ± 5.4° | Cross-sectional, n = 42 (Herrington 2014) |
| 2-D FPPA, single-leg land | 13.5° ± 5.7° | 21.7° ± 3.6° | Same |

**Measurement error sets the usable resolution.** 2-D FPPA SEM = **2.72–3.01°**, smallest detectable difference **7.54–8.93°** (within-day ICC .59–.88, between-day .72–.91) (Munro et al. 2012). A 5° between-limb difference is therefore **inside the noise** for an individual. Group means above are interpretable; individual screening at ±5° is not.

### 5.2 What the injury evidence actually supports

**Prospective (stronger):**
- **Eyes-closed single-leg stance → ankle sprain.** 230 high-school/collegiate athletes, preseason 10-s test: adjusted RR **2.54 (95% CI 1.02–6.03)**, inter-rater **κ = 0.898** (Trojian & McKeag 2006). This is the best prospective evidence for a *static* single-leg test — and the outcome is ankle, not ACL.
- **SEBT reach → any lower-extremity injury.** 235 basketball players: composite <94% of limb length → **6.5×** injury risk; anterior asymmetry >4 cm → **2.5×**; ICC 0.82–0.87 (Plisky et al. 2006). Dynamic reach, not a plate test.
- **Trunk displacement → knee ligament injury.** 277 collegiate athletes, 25 injuries over 3 years: trunk displacement after sudden force release predicted ligament injury with 91% sensitivity / 68% specificity — **female-specific, single cohort** (Zazulak et al. 2007).
- **Knee abduction moment → ACL.** 205 female athletes; the strongest predictor in that cohort (Hewett et al. 2005). Direction credible, precision limited.

**Cross-sectional only (weak):** pelvic drop, trunk lean, hip adduction and knee valgus differences in PFP (Nakagawa 2012; Herrington 2014) cannot distinguish cause from consequence. Notably, the prospective PFP study often cited for pelvic drop actually found **hip adduction** significant (p = .007) and hip internal rotation (p = .47) and rear-foot eversion (p = .10) null (Noehren et al. 2013). **"Pelvic drop causes PFP" is not supported prospectively.** 
**The LESS cautionary tale.** Padua et al. (2015) reported 86% sensitivity / 64% specificity for an ACL-injury cut-off of 5 — in 829 elite-youth soccer players with **7 ACL injuries**, giving a **positive predictive value of 1.4%**. The largest cohort (Smith et al. 2012, n = 5,047) found **no** association: OR **1.04 (0.80–1.35)** per LESS point. Do not present LESS as a stand-alone ACL screen.

### 5.3 Test protocols and whether they fit this hardware

| Test | What it measures | Reliability | Feasible on one static plate + camera? |
|---|---|---|---|
| Timed single-leg stance (EO/EC) | Time to failure / loss of position | ICC 0.994 EO, 0.998 EC, n = 549 (Springer et al. 2007) | **Yes** — camera can auto-score foot lift and trunk loss; plate adds COP |
| mCTSIB | 4 sensory conditions, time tolerated or COP sway | Fair-to-good across 1-day/1-week/1-month retest (Goble et al. 2021); instrumented ICC 0.762–0.909, composite 0.898 (Watson & Trudelle-Jackson 2021) | **Yes, but a foam pad is mandatory** — a bare plate cannot run conditions 3–4 |
| Romberg | Feet-together EO vs EC, pass/fail | Binary; no ICC in sources reviewed | Yes, but less informative than the same conditions inside mCTSIB |
| BESS | Error count, 6 stances × 20 s | Total-score ICC 0.57 inter / 0.74 intra; MDC **9.4 / 7.3 errors** (Finnoff et al. 2009) | Technically yes; **MDC is too large for individual decisions** |
| Single-leg COP | Velocity, amplitude, area, path length | ICC 0.71–0.86; velocity best, area weakest (Kozinc et al. 2025) | Plate required; camera adds kinematics only |
| Limits of stability | Volitional COP excursion to targets | ICC 0.48–0.82 across five LOS metrics (Harter 2011) | **Yes on a fixed plate** (the plate does not move); bilateral norms only, single-leg unnormed |
| Y-Balance / SEBT | Normalised reach distance | Intra 0.85–0.91, inter 0.81–1.00 | **No** — needs a reach rig/ruler, not a plate |

**mCTSIB specifics.** Four conditions: firm EO, firm EC, foam EO, foam EC — CTSIB conditions 1, 2, 4, 5, with the visual-conflict dome dropped. Standard is 30 s per condition, three trials averaged; the instrumented version uses 10 s. Foam is medium-density viscoelastic ("Temper"), typically 40.6 × 40.6 × 7.6 cm. Scoring is time tolerated (clinical) or COP sway velocity / path length (instrumented). **Hard limit:** there is no sway-referenced platform and no moving visual surround, so mCTSIB cannot isolate vestibular function the way SOT conditions 5–6 do. It is a screen, not a vestibular diagnosis.

**Limits of stability is feasible** because LOS is volitional COP excursion — the plate stays still. What is missing is single-leg normative data; bilateral norms cannot be transferred.

---

## 6. Practical validity: placement, calibration, 2D vs 3D, lighting, frame rate

### 6.1 Camera placement

- **Frontal view** is required for pelvic obliquity and FPPA (hip/knee bias acceptable at −4.6 to 1.6 ± 3.7–4.2°, but ankle LoA ±12°; Wade et al. 2023); **sagittal view** is required for knee flexion, trunk AP lean and ankle strategy, and is the strong plane (MAE 4.0–5.6°; Stenum et al. 2021). A single camera cannot do both well — pick the plane that matches the clinical question, or use two.
- **Oblique/rear-oblique** is a compromise: one elevated rear-oblique camera with optical flow tracked trunk AP motion well but ML weakly (Yang et al. 2026). It validates *trunk trajectory*, not COM, and reports no ICC/RMSE — grade C for COM.
- **Sensor geometry interacts with view angle.** Kinect v2 tracked best frontally (0°) while Azure Kinect was best at 22.5–90°, with a significant sensor × angle interaction for sagittal hip and knee (Yeung et al. 2021). Do not assume "depth camera = view-independent."

Validated distances in the literature, for reference rather than as dose–response findings: RealSense D415 at **2.0 m, 1.1–1.2 m height, ~5° downward pitch**, 640×480, 30 Hz (Sonar et al. 2026); Azure Kinect at **2.5 m, 1.4 m height** for stationary tasks (Bertram et al. 2023); OpenPose gait cameras at **3 m, ~90 cm height**, one per plane (Wade et al. 2023). These are protocol descriptions, not optima.

### 6.2 Calibration

Multi-view 3-D requires intrinsics **and** extrinsics. The validated markerless systems use a calibration object (checkerboard/charuco) and report reprojection error; OpenCap uses two phones plus a calibration recording and OpenSim scaling (Uhlrich et al. 2023). Two requirements follow: **verify reprojection error <1 px before trusting 3-D angles**, and **establish the camera-to-plate transform** (the plate's axes are the COP reference frame; the camera frame is arbitrary). Monocular 3-D pose outputs are **up-to-scale** and must not be treated as metres — a failure mode that silently corrupts any 3-D angle.

### 6.3 2D vs 3D: what each buys

| | Monocular 2D | Stereo/depth | Multi-view triangulation |
|---|---|---|---|
| In-plane angles | Good in sagittal (4–6°), poor frontal knee | Moderate | Best (2–5°) |
| Depth/out-of-plane | None | Metric depth, 20–89 mm landmark error | Metric after calibration |
| Occlusion | Fails on far-side limb | Fails similarly | Recovers via other views |
| COM | Not defensible | Approximate | Validated to mm-cm in one study |
| Cost/complexity | Lowest | Low | Calibration + sync effort |

The overall verdict from the markerless review literature is blunt: temporospatial measures are broadly equivalent to marker-based, but **joint centre locations and joint angles are not yet sufficiently accurate for clinical applications**, and accuracy has never been checked against a true gold standard such as bi-planar videoradiography (Wade et al. 2022).

### 6.4 Lighting, clothing, occlusion

**Clothing changes the model:** rolling shorts up and adding pelvic marker bands changed markerless segment-length estimates by up to **4 cm — roughly 10% of thigh length** (Theia3D; PMC9699317), so standardise attire or accept 10% scale noise. **Occlusion doubles error** on the far-side hip and knee in 2D (Wade et al. 2023), and single-leg stance is occlusion-heavy by construction — the swing leg crosses the stance leg in side view, and the pelvis rotates in front view. **Lighting is largely untested:** the RealSense validation ran under controlled indoor lighting and states that variable lighting was not evaluated (Sonar et al. 2026), so treat lighting control as a protocol requirement. **Ankle/foot landmarks are the weakest** in every system reviewed (20–89 mm RMSE; ±12° LoA), so foot-lift and inversion scoring from camera is not viable — use a plate unload threshold instead.

### 6.5 Frame rate

Quiet-standing COP power is concentrated below 0.5 Hz with almost nothing above 2 Hz (Quijoux et al. 2021). A 30 fps camera therefore has a Nyquist margin of roughly 7× over the signal of interest — **30 fps is sufficient for sway amplitude and velocity**, which is why every validated depth system runs at 30 Hz. (The Nyquist argument is inference; the 30 Hz validation is empirical.)

Go to **60 fps** for: marker tracking stability under motion blur, foot-lift/contact event timing, and any weight-bearing squat or transition. Go to **120 fps** only if landing impact timing matters. Camera frame rate is not the binding constraint on balance accuracy — **landmark jitter is**, and it does not improve linearly with frame rate.

---

## 7. Published and open protocols combining a plate with video

| System / study | What it is | Validation | Usable for us? |
|---|---|---|---|
| Nourbakhsh Sabet et al. 2026 | Multi-view OpenPose + 8-segment model + inverse-dynamics COP | 12 adults, 5 balance tasks; AP COP CC 0.95–0.98, RMSE 2.2–8.5 mm; ML weaker | **Yes — closest template**; replicate with 2 cameras |
| Sonar et al. 2026 | Single RealSense D415 + MediaPipe, 16-segment COM, 30 Hz | 10 adults, directional leans; ICC 0.84–0.86, MAPE 10.5% | Yes, as a screening-grade single-camera tier |
| Chaumeil et al. 2024 | Markerless COM/XCoM vs marker-based | RMSE ≈1 cm; 95% CI <2 cm except one task | Reference for expected error |
| Corriveau et al. 2000/2001 | 2 AMTI plates + 3 OPTOTRAK sensors → COP–COM | ICC 0.79 AP single trial → 0.94 with 4 trials | **Yes** — the method template for COP–COM |
| Yang et al. 2026 | One elevated rear-oblique camera, Lucas–Kanade optical flow | Trunk AP tracked mocap closely; ML weaker; no ICC reported | Exploratory only |
| OpenCap (Uhlrich et al. 2023) | 2 iPhones + OpenSim, open source | Kinematic RMSE 2.0–10.2°, GRF 1.5–11.1% BW in 10 adults — **gait, squat, chair rise, drop jump only** | Useful for squats; **not validated for quiet stance or COP** |
**Open datasets:** BioCV (15 participants, 9 synchronised 200 Hz cameras + mocap + plate; walking/running/jumping/hopping) is for benchmarking pipelines, not balance. **The genuine gap:** no published open protocol validates markerless **single-leg-stance** kinematics (contralateral pelvic drop, trunk lean) against a plate. That is not a reason to avoid it — it is the reason a local validation study would be publishable and would differentiate this lab.

---

## 8. Metrics table

| Metric | What it captures | Camera view needed | Evidence level | Key source |
|---|---|---|---|---|
| COP mean velocity (resultant/AP/ML) | Rate of corrective movement | None (plate) | **A** | Ruhe 2010; Lo 2022 |
| COP path length | Total excursion | None | **A** (with fixed preprocessing) | Koltermann 2019 |
| COP RMS / SD | Spatial dispersion | None | **A** | Lin 2008 |
| 95% ellipse area | Postural dispersion | None | **A/B** (device-dependent) | Lin 2008; Vindiš 2026 |
| COP–COM (GLP from plate) | Control effort / error signal | None | **A** (method validity) | Lafond 2004; Corriveau 2000 |
| COP–COM (kinematic COM) | Same, valid when pendulum breaks | 2 calibrated views | **B+** | Nourbakhsh Sabet 2026 |
| COM displacement (AP) | Body sway | Multi-view | **B+** | Nourbakhsh Sabet 2026 |
| COM displacement (ML) | Lateral sway | Multi-view | **B** (weaker) | Same |
| Trunk inclination / lean angle | Postural orientation, hip strategy | Sagittal or frontal | **B** | Bertram 2023; Asaeda 2019 |
| Pelvic obliquity / drop | Frontal-plane hip control (Trendelenburg) | Frontal/rear | **B** | Asaeda 2019 |
| Sagittal knee/hip angle | Squat/strategy kinematics | Sagittal | **B** (4–7° MAE) | Stenum 2021 |
| Frontal-plane knee valgus (absolute) | Knee alignment | Frontal | **C** | Asaeda 2024 (−19° bias) |
| 2-D FPPA (change score) | Dynamic valgus, within-subject | Frontal | **B** | Munro 2012 |
| Ankle/foot angle, foot lift | Ankle strategy, BESS errors | Sagittal (close) | **C** | Wade 2023 (±12° LoA) |
| Ankle-vs-hip strategy index | Coordination mode | Sagittal + frontal | **C** (no validated classifier) | Horak & Nashner 1986 (construct only) |
| Per-foot COP / weight symmetry | L/R loading | — | **C** (impossible, one plate) | Mechanical constraint |
| Knee abduction moment | ACL-risk kinetics | — | **C** (needs per-limb GRF) | Mechanical constraint |
| Timed SLS, eyes closed | Static postural competence | Any (auto-scoring) | **A** (prospective) | Trojian & McKeag 2006 |
| mCTSIB condition ratios | Sensory reweighting screen | Any + foam pad | **B** | Goble 2021; Watson 2021 |
| Limits of stability | Volitional excursion | Any | **B** | J Athl Train 2011 |

---

## 9. What this lab can implement now vs later

### Now — defensible with current hardware and current evidence

1. **Lock the plate pipeline and publish its MDCs.** 30 s trials, 10 Hz zero-phase Butterworth, 100 Hz analysis, mean velocity as the primary outcome. Run a 20-person, 2-session test–retest and compute local ICC/SEM/MDC per metric and per stance. Until that exists, `change_exceeds_mdc` is using borrowed constants.
2. **Compute COP–COM from the plate alone via GLP double integration.** No camera, validated as comparable to the kinematic method (Lafond et al. 2004), four trials averaged (Corriveau et al. 2000). This is the highest-value, lowest-effort addition in the entire report.
3. **Add a foam pad and run mCTSIB properly.** Cheap consumable; unlocks conditions 3–4 and the Romberg/foam ratios already implemented in `metrics.py`.
4. **One 60 fps camera, side or oblique, for trunk/pelvic kinematics during single-leg stance.** Report *relative* sway and *within-athlete change*, with a stated ±5–10° uncertainty band. Use it to explain the plate, never to replace it.
5. **Frontal camera for FPPA and pelvic obliquity, reported against the SDD (7.5–8.9°), plus camera auto-scoring of timed single-leg stance** (foot lift, trunk loss, arm touch). Flag anything below the SDD as "within measurement error"; the timed test is the one static measure with prospective injury evidence.
6. **Standardise protocol variables:** attire, footwear, camera distance/height, lighting, trial count, habituation session. Each of these has a documented effect larger than the effects we are trying to detect.

### Later — needs a local validation study first

1. **Two-camera 8-segment COM + inverse-dynamics COP**, replicating Nourbakhsh Sabet et al. 2026. Requires checkerboard calibration, reprojection error <1 px, hardware or LED sync ≤1 frame, and a same-frame comparison against the plate. Success criterion: AP COP CC > 0.90 and RMSE < 10 mm in quiet standing; if it fails, report and stop.
2. **COP–COM from camera in single-leg stance and foam**, where GLP/LPF assumptions break, plus an **ankle-vs-hip strategy index** validated against COP–COM phase and the plate's AP/ML power ratio. This is the one place where camera-derived COM is scientifically necessary rather than decorative.
3. **Weight-bearing squat kinematics** with plate force symmetry — but note that per-limb symmetry is not available from one plate, so this is limited to total force and COM excursion.
4. **Depth camera (Azure/Orbbec)** only if a specific joint not visible in 2D is required. Both are 30 Hz, both are weaker than triangulation for angles, Azure hardware/SDK is discontinued, and Orbbec has **no published degree- or mm-level validation for balance or single-leg stance**. Do not start here.

### Not possible on this setup — say so explicitly in any report

Per-foot COP or L/R symmetry in double-leg stance (one plate); direct muscle activation (no EMG); joint moments in single-leg stance without per-limb GRF; sway-referenced platform or moving visual surround (SOT 5–6); platform-translation perturbations; absolute knee valgus from a single camera; reliable ML COM from a single camera; vestibular diagnosis from mCTSIB.

---

## 10. Where the evidence is genuinely thin

State these as unknowns rather than filling them in:

1. **No published validation of markerless single-leg-stance pelvic drop or trunk lean against a plate.** The closest is Kinect vs 3-D mocap in hip patients (Asaeda et al. 2019) — relative agreement, not criterion validity for CPD.
2. **No published Orbbec validation** for balance or single-leg stance at degree/mm level, and **no published C7 or hip-midpoint COM proxy validation** for standing balance.
3. **Optical-flow COM** is validated for trunk trajectory only, in one study, without ICC/RMSE.
4. **Lighting effects** on pose-estimation balance metrics are essentially unstudied; the one system that mentions it explicitly did not test it.
5. **Frame-rate dose–response** does not exist (30 fps is convention plus Nyquist reasoning), and **test–retest reliability of camera-derived COM/COP metrics** — as opposed to concurrent validity — is largely unreported, which matters because monitoring needs reliability more than validity.

---

## References

- Albert JA, Owolabi V, Gebel A, et al. Evaluation of the Microsoft Azure Kinect for use in biomechanical analysis. *Sensors.* 2020;20(18):5104. https://www.mdpi.com/1424-8220/20/18/5104
- Asaeda M, Onishi T, Ito H, et al. Reliability and validity of knee valgus angle calculation at single-leg drop landing by posture estimation using machine learning. *Heliyon.* 2024. https://pmc.ncbi.nlm.nih.gov/articles/PMC11399566
- Asaeda M, et al. Validity of motion analysis using the Kinect system to evaluate single leg stance in patients with hip disorders. *Gait Posture.* 2019. https://pubmed.ncbi.nlm.nih.gov/29665566
- Benda BJ, Riley PO, Krebs DE. Biomechanical relationship between center of gravity and center of pressure during standing. *IEEE Trans Rehabil Eng.* 1994;2:3–10. https://ieeexplore.ieee.org/document/296348
- Bertram J, et al. Accuracy and repeatability of the Microsoft Azure Kinect for clinical measurement of motor function. *PLoS One.* 2023;18(1):e0279697. https://pmc.ncbi.nlm.nih.gov/articles/PMC9879399
- Carpenter MG, Frank JS, Winter DA, Peysar GW. Sampling duration effects on centre of pressure summary measures. *Gait Posture.* 2001;13(1):35–40. https://pubmed.ncbi.nlm.nih.gov/11166552
- Chaumeil A, et al. Markerless estimation of COM and XCoM during balance tasks. *J Biomech.* 2024;165:112018. https://doi.org/10.1016/j.jbiomech.2024.112018
- Corriveau H, Hébert R, Prince F, Raîche M. Intrasession reliability of the "center of pressure minus center of mass" variable of postural control in the healthy elderly. *Arch Phys Med Rehabil.* 2000. https://pubmed.ncbi.nlm.nih.gov/10638875
- Corriveau H, Hébert R, Prince F, Raîche M. Postural control in the elderly: test–retest and interrater reliability of the COP–COM variable. *Arch Phys Med Rehabil.* 2001;82:80–85. https://archives-pmr.org/article/S0003-9993(01)53407-3/pdf
- Finnoff JT, Peterson VJ, Hollman JH, Smith J. Intrarater and interrater reliability of the Balance Error Scoring System (BESS). *PM&R.* 2009;1(1):50–55. https://doi.org/10.1016/j.pmrj.2008.06.002
- Gage WH, Winter DA, Frank JS, Adkin AL. Kinematic and kinetic validity of the inverted pendulum model in quiet standing. *Gait Posture.* 2004;19(2):124–132. https://pubmed.ncbi.nlm.nih.gov/15013500
- Goble DJ, Conner NO, Nolff MR, et al. Test–retest reliability of the BTrackS mCTSIB protocol across multiple time durations. *Med Devices (Auckl).* 2021;14:355–361. https://pmc.ncbi.nlm.nih.gov/articles/PMC8604651
- Herrington L. Knee valgus angle during single leg squat and landing in patellofemoral pain patients and controls. *The Knee.* 2014;21(2):514–517. https://pubmed.ncbi.nlm.nih.gov/24380805
- Hewett TE, Myer GD, Ford KR, et al. Biomechanical measures of neuromuscular control and valgus loading of the knee predict ACL injury risk in female athletes. *Am J Sports Med.* 2005;33(4):492–501. https://pubmed.ncbi.nlm.nih.gov/15722287
- Horak FB, Nashner LM. Central programming of postural movements: adaptation to altered support-surface configurations. *J Neurophysiol.* 1986;55(6):1369–1381. https://pubmed.ncbi.nlm.nih.gov/3734861
- Harter RA. Validity and reliability of limits-of-stability testing: a comparison of 2 postural stability evaluation devices. *J Athl Train.* 2011;46(6):600–606. https://pmc.ncbi.nlm.nih.gov/articles/PMC3418936
- Koltermann JJ, Gerber M, Beck H, et al. Validation of different filters for center of pressure measurements by a cross-section study. *Technologies.* 2019;7(4):68. https://www.mdpi.com/2227-7080/7/4/68
- Kozinc Ž, Kuliqi A, Obal I. Reliability of postural sway measures during single-leg stance: conventional and complementary error metrics. *Sport Sci Health.* 2025;21:3173–3179. https://link.springer.com/article/10.1007/s11332-025-01527-2
- Lafond D, Duarte M, Prince F. Comparison of three methods to estimate the center of mass during balance assessment. *J Biomech.* 2004;37(9):1421–1426. https://bmclab.pesquisa.ufabc.edu.br/pubs/jb03.pdf
- Le Clair K, Riach C. Postural stability measures: what to measure and for how long. *Clin Biomech.* 1996;11(3):176–178. https://www.sciencedirect.com/science/article/abs/pii/0268003395000275
- Lin D, Seol H, Nussbaum MA, Madigan ML. Reliability of COP-based postural sway measures and age-related differences. *Gait Posture.* 2008;28(3):337–342. https://stacks.cdc.gov/view/cdc/186877/cdc_186877_DS1.pdf
- Lo PY, Su BL, You YL, et al. Measuring the reliability of postural sway measurements for a static standing task: the effect of age. *Front Physiol.* 2022;13:850707. https://pmc.ncbi.nlm.nih.gov/articles/PMC9138610
- Morasso PG. Quiet standing: the single inverted pendulum model is not so bad after all. *PLoS One.* 2019;14(3):e0213870. https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0213870
- Munro A, Herrington L, Carolan M. Reliability of 2-dimensional video assessment of frontal-plane dynamic knee valgus during common athletic screening tasks. *J Sport Rehabil.* 2012;21(1):7–12. https://pubmed.ncbi.nlm.nih.gov/22104115
- Nakagawa TH, Moriya ET, Maciel CD, Serrão FV. Trunk, pelvis, hip, and knee kinematics, hip strength, and gluteal muscle activation during a single-leg squat in males and females with and without patellofemoral pain syndrome. *J Orthop Sports Phys Ther.* 2012;42(6):491–501. https://www.jospt.org/doi/10.2519/jospt.2012.3987
- Noehren B, Hamill J, Davis I. Prospective evidence for a hip etiology in patellofemoral pain. *Med Sci Sports Exerc.* 2013;45(6):1120–1124. https://pubmed.ncbi.nlm.nih.gov/23274607
- Nourbakhsh Sabet D, Tamimi H, Vette AH, Nazarahari M. Inverse dynamics meets markerless motion capture during standing: concurrent validation of center of mass and center of pressure estimations. *Ann Biomed Eng.* 2026. https://pubmed.ncbi.nlm.nih.gov/42542510
- Padua DA, DiStefano LJ, Beutler AI, et al. The Landing Error Scoring System as a screening tool for an ACL injury-prevention program in elite-youth soccer athletes. *J Athl Train.* 2015;50(6):589–595. https://pmc.ncbi.nlm.nih.gov/articles/PMC4527442
- Palmieri RM, Ingersoll CD, Stone MB, Krause BA. Center-of-pressure parameters used in the assessment of postural control. *J Sport Rehabil.* 2002;11(1):51–66. https://doi.org/10.1123/jsr.11.1.51
- Plisky PJ, Rauh MJ, Kaminski TW, Underwood FB. Star Excursion Balance Test as a predictor of lower extremity injury in high school basketball players. *J Orthop Sports Phys Ther.* 2006;36(12):911–919. https://pubmed.ncbi.nlm.nih.gov/17193868
- Quijoux F, Nicolaï A, Chairi I, et al. A review of center of pressure (COP) variables to quantify standing balance in elderly people: algorithms and open-access code. *Physiol Rep.* 2021;9(22):e15067. https://pmc.ncbi.nlm.nih.gov/articles/PMC8623280
- Ruhe A, Fejer R, Walker B. The test–retest reliability of centre of pressure measures in bipedal static task conditions — a systematic review. *Gait Posture.* 2010;32(4):436–445. https://pubmed.ncbi.nlm.nih.gov/20947353
- Scoppa F, Capra R, Gallamini M, Shiffer R. Clinical stabilometry standardization: basic definitions — acquisition interval — sampling frequency. *Gait Posture.* 2013;37(2):290–292. https://pubmed.ncbi.nlm.nih.gov/22889928
- Smith HC, Johnson RJ, Shultz SJ, et al. A prospective evaluation of the Landing Error Scoring System (LESS) as a screening tool for anterior cruciate ligament injury risk. *Am J Sports Med.* 2012;40(3):521–526. https://pmc.ncbi.nlm.nih.gov/articles/PMC6488213
- Sonar VG, et al. Development, reliability, and validity assessment of a portable 3D camera-based system for quantifying postural sway and balance. *Sensors.* 2026;26(13):3987. https://pmc.ncbi.nlm.nih.gov/articles/PMC13363847
- Springer BA, Marin R, Cyhan T, et al. Normative values for the unipedal stance test with eyes open and closed. *J Geriatr Phys Ther.* 2007;30(1):8–15. https://geriatrictoolkit.missouri.edu/balance/Normative_Values_for_the_Unipedal_Stance_Test_Springer-JGPT.pdf
- Stenum J, Rossi C, Roemmich RT. Two-dimensional video-based analysis of human gait using pose estimation. *PLoS Comput Biol.* 2021;17(4):e1008935. https://journals.plos.org/ploscompbiol/article?id=10.1371/journal.pcbi.1008935
- Trojian TH, McKeag DB. Single leg balance test to identify risk of ankle sprains. *Br J Sports Med.* 2006;40(7):610–613. https://pmc.ncbi.nlm.nih.gov/articles/PMC2564306
- Uhlrich SD, Falisse A, Kidziński Ł, et al. OpenCap: 3D human movement dynamics from smartphone videos. *PLoS Comput Biol.* 2023;19(10):e1011462. https://journals.plos.org/ploscompbiol/article?id=10.1371/journal.pcbi.1011462
- Vindiš M, Kozinc Ž, Nedović N. Test–retest reliability of postural sway measures using a portable low-cost force plate in healthy adults. *Physiother Res Int.* 2026;31:e70270. https://pmc.ncbi.nlm.nih.gov/articles/PMC13326530
- Wade L, Needham L, Evans M, et al. Examination of 2D frontal and sagittal markerless motion capture: implications for markerless applications. *PLoS One.* 2023;18(11):e0293917. https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0293917
- Wade L, Needham L, McGuigan P, et al. Applications and limitations of current markerless motion capture methods for clinical gait biomechanics. *PeerJ.* 2022;10:e12995. https://pmc.ncbi.nlm.nih.gov/articles/PMC8884063
- Watson S, Trudelle-Jackson E. Test–retest reliability and minimal detectable change of the instrumented mCTSIB in healthy older adults. *J Geriatr Phys Ther.* 2021;44(4):183–188. https://pubmed.ncbi.nlm.nih.gov/32618856
- Winter DA. Human balance and posture control during standing and walking. *Gait Posture.* 1995;3(4):193–214. https://doi.org/10.1016/0966-6362(96)82849-9
- Winter DA, Patla AE, Prince F, et al. Stiffness control of balance in quiet standing. *J Neurophysiol.* 1998;80(3):1211–1221. https://journals.physiology.org/doi/10.1152/jn.1998.80.3.1211
- Yang Y, Chang E, Yoo B. Video-based trunk motion tracking during standing balance. *Sci Rep.* 2026. https://www.nature.com/articles/s41598-026-68104-6
- Yeung LF, Yang Z, Cheng KC, et al. Effects of camera viewing angles on tracking kinematic gait patterns using Azure Kinect, Kinect v2 and Orbbec Astra Pro v2. *Gait Posture.* 2021;87:19–26. https://pubmed.ncbi.nlm.nih.gov/33878509
- Zazulak BT, Hewett TE, Reeves NP, Goldberg B, Cholewicki J. Deficits in neuromuscular control of the trunk predict knee injury risk. *Am J Sports Med.* 2007;35(7):1123–1130. https://journals.sagepub.com/doi/abs/10.1177/0363546507301585
- Markerless motion capture: what clinician-scientists need to know right now. 2022. https://pmc.ncbi.nlm.nih.gov/articles/PMC9699317
- Interpreting common standing postural sway measures. *Gait Posture.* 2025. https://www.sciencedirect.com/science/article/pii/S0966636225008112
