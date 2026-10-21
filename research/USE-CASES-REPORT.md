# Bertec BP5046 — Athlete Performance Lab: Complete Use-Case Research Report

**Status:** FINAL · 2026-10-15 · compiled by Hermes (CTO-level deep dive per operator request)
**Method:** Local verified sources (Bertec Balance Advantage Static System Manual 80P-0009, SDK 2.57 `bertecif.h`, SDK docs 80P-0165, project raw data) + four parallel web research programs (official Bertec sources; 40+ organizations; athlete protocols/norms literature; vestibular protocols literature). **Every claim below traces to a source; nothing is guessed.** Unverifiable items are marked UNVERIFIED.
**Companion:** `IMPLEMENTATION-ROADMAP.md` (phased build plan for our app).

---

## 1. Executive Summary

The plate is a complete, research-grade **balance & postural-control measurement station**: 1000 Hz, digital, COP to ±0.25 mm, from **FZ + MX + MY** via `COPx = −MY/FZ, COPy = MX/FZ`.

All four requested pillars are FULLY implementable in our local app:
1. **Balance work & testing** — static posturography battery (double/single-leg, eyes open/closed, foam) with published norms, ICC and MDC values.
2. **Imbalances work & test** — sequential single-leg asymmetry testing + ASI (16% flag), weight-bearing squat load symmetry, RWS lateral deltas; ACL/CAI evidence with numbers.
3. **Vestibular** — mCTSIB (+ the newer HS-mCTSIB), Romberg quotient family, unilateral & tandem stance; interpretation matrices; rehab via COP biofeedback.
4. **Reachability** — full 8-direction Limits-of-Stability battery (RT/MVL/EPE/MXE/DCL) + training progression 25→100%, maximal-lean protocols, reference percentiles.

Plus the athlete lifecycle stack: **baseline → in-season monitoring → concussion → return-to-play**, anchored on COBALT/iBESS evidence — all physician-governed for clinical use.

**Recommended lab battery v1** (implementation order in the roadmap):
1. Double-leg quiet stance, EO/EC (3–5 × 30–60 s) — the reliability workhorse
2. Single-leg stance, EO/EC, per side (3 × 30 s) — asymmetry + injury-risk core
3. mCTSIB (4 conditions × 3 × 10 s) + HS-mCTSIB when history suggests
4. Limits of Stability, 8 directions (+ training mode)
5. Weight-Bearing Squat load symmetry (0/30/60°/90°) + RWS lateral
6. Concussion module: iBESS-style + 3×20 s EC path length (+ SCAT6/VOMS adjacent) — baseline only; RTP physician-led

**Honest scope exclusions** (equipment, not software): SOT / Motor Control Test / Adaptation Test / HS-SOT (require moving platform + visual surround — NOT possible on a static plate); gait walkway tests (Walk Across, Tandem Walk, Step Quick Turn, Step Up-Over, Forward Lunge — need the 20×60 in Functional plate); any jump/impact use (500 lb patient rating; open Bertec question).

---

## 2. Hardware & Software Reality (VERIFIED)

### 2.1 Plate identity & lineage
- Model: **BP5046** — the "**Essential Balance Plate**" (18″ × 20″, dual-balance, 500 lb class), sold inside the **Balance Advantage Essential / Portable Essential** systems and the Vision Trainer's balance option. Bertec's discontinued list: BP5046 End of Sale Jul 2023, EOL May 2024 — **but with the explicit note "BP5046 Dual is still an active and supported product"** (the dual variant continues shipping in Portable Essential). Legacy software shipped with it: Balance Check Screener/Trainer (EOL Mar 2023, upgrade path = Portable Essential). https://www.bertec.com/discontinued · https://www.bertec.com/products/portable-essential
- Institutional customers of Bertec's family (context): Nike, US Olympic Swimming, Cleveland Clinic, Mayo Clinic, Harvard; VA hospitals; NIH Clinical Center; Defense Health Agency procurement (2026).

### 2.2 Specifications (official)
| Spec | Value |
|---|---|
| Channels | **FZ (vertical force) + MX, MY (moments)** → COP computed; **no shear FX/FY, not 6-component** (verified: SDK 6-channel features explicitly exclude non-6ch devices) |
| COP accuracy | **± 0.25 mm** (COPx/COPy) |
| Sensitivity (Fz) | 0.02 kg average |
| Output | Digital, **1000 Hz fixed** (no resampling needed) |
| Connection | USB-B (FTDI driver) |
| Dimensions | 500 × 460 × 38 mm (18 × 20 × 1.5″), 6 kg, 38 mm step height |
| Capacity | **500 lb / 227 kg patient weight** (manual safety text also prints 225 kg; treat 500 lb as binding). NOT an impact rating. |
| Bilateral | "Measures each subject foot independently" (current dual plate; verify channel structure of OUR unit at first connection — see §6) |
| Computed channels | **COG + Sway Angle** available from FZ/MX/MY (subject height input; default 1500 mm) |
| Calibration | Self-calibrates each power-up; "rarely if ever requires re-calibration" |

Sources: Static System Manual 80P-0009 (current rev v35, 2026-02, bertec.com/s/BER_StaticUserManual_v35.pdf); Vision Trainer manual (80P-0071); SDK 80P-0165 + bertecif.h.

### 2.3 The software that originally shipped with this plate (Balance Advantage catalog)
Complete catalog extracted from the official manual — assessment suite (mCTSIB, **HS-mCTSIB** [2.5], Limits of Stability, Rhythmic Weight Shift, Unilateral Stance, Weight Bearing Squat, **Romberg protocol** [2.5], iBESS [for "highly athletic patients"], optional **COBALT athlete concussion test**), training families (Quick, Weight Shifting w/ LOS 25→100% progression, Seated core, Mobility, Closed Chain with %-weight-bearing targets), clinician scoring rules (fall = max sway-velocity substitution 6°/s; STEP/PULL auto-detection via weight loss; compliance scoring formulas), reports (individual/combined/progress/performance-eval; green/red vs age norms), CSV raw export (Ctrl+F6). **Our app can re-implement all static-plate protocols; the built-in numeric norms are software-embedded/proprietary — we adopt published literature norms instead (all cited below).**

### 2.4 Our app today
`projects/bertec-jump` — local dashboard :8765, ctypes → SDK 2.57, 60 tests, verified weight chain, raw CSV+JSON, jump module locked. No live hardware session yet.

---

## 3. The Use-Case Catalog

### A. Balance work & assessment (static posturography)
**Protocols & procedure standards** (from literature synthesis; the standard that makes numbers comparable):
- **Processing standard (apply to every trial):** raw 1000 Hz → 10 Hz zero-phase low-pass → decimate to 100 Hz for path-based metrics; ≥25 s acquisition (ISPGR minimum; 90 s+ or 3–5 averaged trials for best reliability); same time-of-day, same settings across visits. (Ruhe 2010, Scoppa 2013, Quijoux 2021.)
- **Metrics (definitions):** path length L = Σ√(dx²+dy²) mm; mean velocity = L/time mm/s (the single most sensitive summary metric — Prieto 1996); 95% confidence ellipse area (Quijoux 2021 formulas); AP/ML range & RMS; ratios (Romberg, foam/firm).
- **Double-leg EO/EC:** 3–5 × 30–60 s. Sport-student norms (30 s, n=173): path 250±116 mm EO → 337±151 mm EC; 95% ellipse 125±82 → 175±125 mm²; EO→EC +34–40%. (Becker 2025.)
- **Single-leg EO/EC per side:** 3 × 30 s (20 s for squad screening), familiarization first. Reliability ICC 0.71–0.90 (velocity-type best); MDC95 velocity ≈ 6.3–9.7 mm/s. Healthy EC reference: 8.74±1.98 cm/s velocity. (Kozinc 2025; Vindis 2026; Ageberg 2003; Novaes 2022.)
- **Foam conditions (mCTSIB):** see §C.
**Training:** steadiness holds with live COP feedback (augmented feedback is the active ingredient); progress by adding foam → eyes closed → single-leg.

### B. Imbalance work & test (asymmetry)
**Constraint stated honestly:** a single plate cannot split left/right load during double stance. Asymmetry is measured **sequentially (single-leg per side)** — this is the validated method, not a limitation workaround:
- **Asymmetry screen:** practice L, practice R, then scored L, scored R (4 × 20 s); **Absolute Symmetry Index ASI = |L−R| / ((L+R)/2) × 100; flag ≥ 16%** on total path length (published threshold; 10% = conventional rule of thumb). (Nolff 2023.)
- **ACL monitoring (eyes closed single-leg):** injured limb velocity 9.83±3.03 vs 8.74±1.98 cm/s controls; ML displacement and 95% area also discriminate. Monitor every 4–6 weeks toward RTP progression. (Novaes 2022.)
- **Chronic ankle instability:** AP/ML sway SD and EC velocity elevated vs controls (SMD 0.31–0.45 meta). (Xue 2024.)
- **Prospective injury flags:** EC transition test (shift double→single on cue) — AUC 0.814 for subsequent injury; EC symmetry index — OR 7.48 for non-contact LE injury in groin-pain athletes. Preseason use. (Dingenen 2016; Chaari 2022.)
- **Weight-Bearing Squat:** % load per side at neutral/30°/60°/90° — asymmetries often only appear at depth (Bertec method); use as load-symmetry companion.
- **If the unit exposes per-foot channels** (see §6): add direct double-stance L/R load asymmetry (the "Portable Essential" dual plate capability).
- **RWS lateral:** directional control + on-axis velocity L vs R — dynamic asymmetry.
- Off-plate adjuncts with published thresholds: **Y-Balance** (anterior asymmetry >4 cm = 2.5× injury risk; composite <94% = 6.5× in girls), Functional Reach (COP-validated proxy r=.71). (Plisky 2006/2021; Duncan 1990.)

### C. Vestibular / sensory integration
- **mCTSIB** (4 conditions × 3 × 10 s; COG sway velocity °/s + composite): condition 4 (foam, EC) is the vestibular probe. Interpretation matrix (Bertec manual): C1/C2 abnormal → somatosensory; C3 only → visual dependence; C4 only → vestibular processing; C3+C4 → combined. Validity: foam-EC vs SOT condition 5 sensitivity 95% / specificity 90% (Weber & Cass 1993); predicts multiple falls OR 4.21 (Nitz 2013). Reliability: ICC 0.762–0.909, but the vestibular condition is noisiest (MDC90 ≈ 1.12 °/s) — interpret change scores carefully.
- **HS-mCTSIB** (Bertec 2.5): adds metronome-paced ~20° yaw head shaking to the EC firm/foam conditions — exposes subtle vestibulopathy missed by standard mCTSIB; for athletes & high-demand individuals (Bertec blog); research variant: 0.3/0.6 Hz head-shake posturography, sens 74%/spec 71% in vestibular neuritis.
- **Romberg quotient** (EC/EO sway): healthy ≈ 2–2.5; **≤1 "postural blindness"** (vision not supporting posture). **Refined vestibular RQ** (foam-EC / firm-EC) distinguishes peripheral vestibulopathy vs somatosensory ataxia where classic Romberg cannot (Anagnostou 2025). Head-retroflexion RQ 3/1 (Norre 1988) documents vestibulospinal compensation state — advanced, optional.
- **Unilateral stance + tandem/"sharpened Romberg"** with time-held norms (El-Kashlan 1998: EC single-leg 20.7±9.5 s at 20–49 y; tandem EC 26.0±8.0 s); test-retest ICC 0.74–0.91.
- **Out of scope (state clearly):** SOT, MCT, ADT, HS-SOT — dynamic platform/visual surround required. We implement mCTSIB + HS-mCTSIB + Romberg + US as the static-plate substitutes (exactly what Bertec recommends in that lane).
- **Athlete screening stack (adjacent):** VOMS + SCAT6/mBESS + tandem gait (± COBALT — below). Note: **force-plate COP measures outperform visual error counting for acute concussion detection.**
- **Rehab evidence:** vestibular rehabilitation strongly recommended (APTA/ANPT CPG 2022); COP/VR biofeedback shows within-group gains, equal to conventional exercise (more enjoyable) — the training suite below is legitimately therapeutic and athlete-useful; don't oversell superiority.

### D. Reachability / Limits of Stability ("vestibule reachability" pillar)
- **LOS 8-direction battery** (F, FR, R, BR, B, BL, L, FL; ankle strategy; practice pass first — learning affects EPE/DCL): metrics **RT (s), MVL (°/s), EPE (%LOS), MXE (%LOS), DCL (%)**. Healthy young-adult values (VSR): **RT 0.61–0.70 s; MVL 6.1–6.7 °/s; EPE 85–91%; MXE 95–96%; DCL 80–85%**; reliability ICC 0.88–0.96 (Lininger 2018). Bertec healthy lean reference: ~6.25–8° forward, 4.5° back, 8° lateral.
- **Simplified maximal-lean protocol** (no targets): 3-phase 30 s trials (quiet → max maintainable lean → hold); ICC 0.86–0.985 (Juras 2008). For environments without full LOS software.
- **Self-determined LOS area** with percentile tables by sex/height: e.g., shorter men 490±82 cm², taller men 529±83, shorter women 420±78, taller women 448±75; 50th pct 484/522/425/453 (Nolff 2023).
- **Training progression:** reach targets at **25% → 100%** of predicted LOS, pacing 1–15 s or on-demand/random, compliance scoring (Bertec formulas) — direct carryover.
- Off-plate adjuncts: Functional Reach (r=.71 with COP excursion; norms cited), Y-Balance/SEBT thresholds (above) — run adjacent to the plate battery, optionally with the plate under the stance foot for COP-strategy data.

### E. Athlete lifecycle: baseline → monitoring → concussion → RTP
- **Preseason baseline:** mCTSIB + single-leg battery + SLS; per-position norms from the largest baseline datasets (mCTSIB: 604 adults 18–29 y, Goble 2019; BESS-family: 10,045 athletes 8–21 y). Baselines' value = the athlete's own comparison.
- **In-season monitoring:** pre/post-session sway deltas as readiness/fatigue markers (post-fatigue COP increases SMD 0.41–3.10; most markers recover in ~10–20 min; lumbar fatigue recovery ~15/9 min). Trend inside our local app.
- **Concussion:** 3 × 20 s EC path length carries ~64% sensitivity at ~90% specificity (Goble 2016 — comparable to BESS ~30% sens/~90% spec, McCrea 2003); mCTSIB + tandem gait + iBESS-style errors; **COBALT** (8 athlete conditions incl. head-shake; validated JNPT 2018; used at e.g. University of Kentucky studies and Banner Sports Medicine's BrainStamp youth program) is the athlete-shaped version of this battery; we can build a COBALT-EQUIVALENT protocol shape (mCTSIB + headshake conditions) but must not claim to deliver "COBALT" (trademark/proprietary scoring).
- **Governance (hard rule):** diagnosis and return-to-play are **physician-led** (Amsterdam 2022 / SCAT5 consensus). No validated force-plate clearance criterion exists — we report objective change vs baseline and MDC, never "cleared."
- **RTP monitoring:** EC single-leg per side + ASI trends + LOS; ACL/ankle thresholds above.

### F. Training library (buildable parity with Balance Advantage)
Five families: **Quick** (basic), **Weight Shifting** (F/B/L/R, LOS 25→100% progression, random targets, pacing 1–15 s/on-demand/none, compliance %), **Seated** (center holds, shifts, circles, trunk rotations, marching — core/foundation), **Closed Chain** (ankle/knee/hip/back with L/R %-weight-bearing targets), **Mobility** (a selection suitable for the 18″×20″: scoots, sit-to-stand style rises; no lunging/stepping-over — Functional-only). Accessories: **~5–6 cm medium-density foam pad**, wooden rocker board, 4″/8″/12″ blocks, foot covers; movement-gain display scaling (0.5–2×) and audio cues from BBA. Evidence: biofeedback training is at least as good as conventional exercise (trend-level superiority in RCTs) — practitioners love it for engagement.
- Add-ons worth building: steadiness-hold score, target-reach games for reachability, dual-task modes (standing + serial subtraction — proven challenge load).

### G. Not possible on this equipment (honest scope)
| Item | Why | Substitute |
|---|---|---|
| SOT / MCT / ADT / HS-SOT | dynamic platform + visual surround needed | mCTSIB + HS-mCTSIB + Romberg |
| Walk Across / Tandem Walk / Step Quick Turn / Step Up-Over / Forward Lunge | 20×60″ Functional walkway | off-plate field tests (YBT etc.) |
| Jumps / impacts of any kind | 500 lb patient rating, not impact-rated | locked (existing safety gate) |
| Fukuda stepping | no validated force-plate protocol | tandem gait (timed/dual-task) — includes the vestibular-ish stepping challenge in a validated way |
| Diagnostic claims / "clearance" | regulatory & governance | objective change vs baseline + MDC flags; physician-led |

### H. Research uses seen in the wild (proof of breadth)
BOSU seated core-stability sway (SAGES); upper-limb-movement COP displacement (Hacettepe); amputee standing stability + LOS (Hacettepe/Marquette); MS ankle-foot-orthosis static posturography (Dayton); dual-task quiet stance (Montana State/OSU, 1000 Hz fz+moments); SOT validity on Bertec CDP (ETSU). Our lab can run all of these shapes on the BP5046 with the metrics above.

---

## 4. Key Numbers Quick-Reference (adopt-able, literature-sourced)

**Norms**
- DL 30 s (sport students): path 250±116 mm EO / 337±151 EC; ellipse 125±82 → 175±125 mm²
- SL EC healthy: 8.74±1.98 cm/s velocity; knee/ankle cohorts up to 11.3±2.9 (ACL injured)
- LOS (young adults): RT 0.61–0.70 s · MVL 6.1–6.7 °/s · EPE 85–91% · MXE 95–96% · DCL 80–85%
- LOS area (cm²): 420–529 typical by sex/height (percentile tables available)
- Romberg quotient: healthy ≈ 2–2.5; ≤1 = postural blindness
- Timed stance (s, 20–49 y): SL EC 20.7±9.5; tandem EC 26.0±8.0

**Reliability / change**
- SL velocity ICC 0.71–0.90; MDC95 velocity ≈ 6.3–9.7 mm/s
- DL on portable plates ICC 0.39–0.65 → use ≥90 s or ≥3 averaged trials
- mCTSIB ICC 0.76–0.91 (foam-EC MDC90 ≈ 1.12 °/s)
- LOS ICC 0.88–0.96; learning effect on EPE/DCL (practice pass required)

**Flags**
- SLS path-length ASI ≥ 16% asymmetric; 10% conventional
- YBT: anterior asymmetry >4 cm = 2.5× risk; composite <94% = 6.5× (girls)
- EC transition AUC 0.814 (prospective injury); EC symmetry OR 7.48 (groin)
- Concussion: 3×20 s EC path length 64% sens @ ~90% spec

(Full citation trail in §7; all values verified against the sources during research.)

## 5. Who Uses This Plate (landscape)
- **Rehab/vestibular:** Helen Hayes Hospital, Northwestern Marianjoy, Madonna, Barrow Neuro, IU Health, Brooks, Bryn Mawr/Main Line, Shepherd Center Concussion Clinic, UF Health Jacksonville, FYZICAL, Mountain Health Balance Center, NeuroRobotics (Portable Essential).
- **Sports concussion programs:** Banner Sports Medicine (Bertec force plate + COBALT in BrainStamp baselines), University of Canberra UCRISE ("first in Australia" for athlete concussion assessment), University of Kentucky (COBALT RTP research), Training HAUS.
- **Research labs with BP5046 specifically:** Ohio State (MOvES), Montana State (ATTMEM), Hacettepe, University of Dayton, Marquette.
- **Institutional-scale:** VA medical centers, NIH Clinical Center, US military health (2026 procurement).
- **Market context:** quote-based pricing (no list prices); used BP5046 listings ≈ $999 (unverified); full CDP systems $186K–$250K (gov contracts); Portable Essential financing from $199/mo. Our app replaces the software side — the plate is the asset.

## 6. Open Questions & First-Connection Checklist
1. **Devices & channels on live connect** (CRITICAL): does our unit enumerate as 1 device × [FZ, MX, MY] (original single-zone) or separate **left/right forces + overall** (portable dual hardware, firmware ≥ 1103)? Our fw is 1101 → predict 3 channels, single zone; CONFIRM.
2. **COP sign conventions**: verify empirically (known off-center loads, 4-quadrant test) before any clinical-style scoring.
3. **Noise floor vs metric stability**: 0.02 kg sensitivity — validate ellipse/velocity stability across window lengths at 1000 Hz.
4. **Foam pad spec**: ~5–6 cm medium-density (viscoelastic ok) — source a lab-grade pad; mark lines to match plate lines.
5. **Norms licensing**: BBA's embedded norms are proprietary — we ship literature-based references (this report's tables) with clear provenance.

## 7. Sources
**Bertec official:** Static System Manual 80P-0009 v35 (bertec.com/s/BER_StaticUserManual_v35.pdf; download rev 2024-02 at downloads.bertec.com) · Vision Trainer manual 80P-0071 · SDK 80P-0165 + July 2026 SDK (downloads.bertec.com/SDK) · bertec.com/products, /performance, /products/portable-essential, /products/vision-advantage, /products/hmd, /discontinued, HS-mCTSIB blog, BBA 2.5 release notes (80P-0318).
**Research (selected):** Prieto 1996 (PMID 9214811) · Ruhe 2010 (20947353) · Scoppa 2013 (22889928) · Quijoux 2021 (PMC8623280) · Becker 2025 (PMC12023527) · Goble 2019/2021 (PMC6519013, PMC8604651) · Kozinc 2025 · Vindis 2026 (42390291) · Ageberg 2003 (PMC166283) · Novaes 2022 (PMC9040202) · Nolff 2023 · Xue 2024 (36872589) · Dingenen 2016 (JOSPT) · Chaari 2022 (36115130) · Lininger 2018 (PMC6159502) · Juras 2008 · Duncan 1990 (2229941) · Plisky 2006/2021 · Weber & Cass 1993 · Anagnostou 2025 (PMC12248627) · Norre 1988 (3489458) · Sci Reports 2021 (PMC8476591) · McCrea 2003 · Goble 2016 · Cavanaugh 2006 · COBALT validation (PMID 29864102) · Penedo 2026 (fatigue meta).
**Organizations:** per-org URLs in the research archive (delegation cache; e.g. nm.org, helenhayeshospital.org, barrowneuro.org, iuhealth.org, bannerhealth.com, canberra.edu.au, uky scholars, sages.org, jospt, hcacettepe).
**Archive note:** full raw findings: `AppData\Local\hermes\cache\delegation\subagent-summary-*.txt` (4 files, 2026-10-15).
