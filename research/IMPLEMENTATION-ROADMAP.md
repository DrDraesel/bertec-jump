# Bertec Athlete Lab — Implementation Roadmap (app: `projects\bertec-jump`)

**Status:** PLAN (post-research) · 2026-10-15 · companion to `USE-CASES-REPORT.md`
**Goal:** turn the existing weight-capture dashboard into a complete local **athlete balance & postural-control station** matching (and where useful exceeding) the Balance Advantage static feature set — using only the official SDK, our own math, and literature-sourced references.

**Principles:** local-only & private (same as RunTrack/NeuroSync) · every hardware sample processed, raw FZ/MX/MY + COP stored per trial · no synthetic data outside tests · safety rails non-negotiable · not a medical device; research/coaching aid with physician-governed clinical use.

---

## Phase 0 — Hardware bring-up (blocks everything else)
1. Live connect; enumerate devices/channels; record the firmware-reported channel list.
   - Expected: 1 device × [FZ, MX, MY]; possibility of L/R+overall (portable dual hardware, fw ≥1103) → if present, capture it; extend bridge (Frame struct currently `device * 1`).
2. Implement COP: `COPx = −MY/FZ`, `COPy = MX/FZ` — then **empirically verify signs** (4-quadrant known loads; left/right, front/back mapping vs plate orientation; document).
3. Verify 1000 Hz cadence + timestamp behavior (known prior: AM6147-E fw1101 = SDK-generated ms ticks; keep raw ticks + hz in metadata).
4. Extend raw CSV: add cop_x_mm, cop_y_mm, subject height (for optional SDK COG/sway-angle channels), device channel set.
5. First real-session smoke test: quiet standing 60 s + deliberate weight shifts; confirm plausibility vs expectations.
**DoD:** live COP trace matches physical behavior; raw file round-trips; tests updated with fixtures (fixtures in tests only).

## Phase 1 — Metrics engine (pure local math)
Processing standard (fixed, documented, versioned):
- 1000 Hz raw → reject nonfinite/gaps → 10 Hz zero-phase low-pass → decimate to 100 Hz for path-based metrics; store both.
- ≥25 s minimum acquisition; protocol defaults 30–60 s.
Metric panel (per trial + per condition summary):
- Path length L (mm), mean velocity (mm/s) total/AP/ML, 95% confidence ellipse area (mm², Quijoux formula), AP/ML range (mm), AP/ML RMS (mm), mean position (COP alignment), time-in-zone / steadiness %, condition ratios (Romberg RQ = EC/EO; foam/firm), **ASI %** for sequential L/R, load % L/R (if channels available), sway from LOS runs: RT/MVL/EPE/MXE/DCL per direction + composites.
- Change flags: MDC-based (values from report §4; per-metric table in code, cited).
- Unit tests: golden fixtures vs published formulas; property tests (zero-motion synthetic → zero sway; known ellipse → expected area).
**DoD:** metric suite passes vs hand-computed fixtures; no dependency beyond stdlib + numpy (venv addition).

## Phase 2 — Protocol runner (guided test battery)
Runner framework: protocol = ordered conditions × trials with per-trial timer, live COP cursor, foot-placement guide (plate line overlay in UI), automatic quality checks (movement artifacts, fade/fatigue flags), operator trial control (keep / discard / repeat — parity with BBA ctrl-del flow), notes field, session store.
Protocols order of delivery:
1. **DL quiet stance EO/EC** (30 s × 3) — with live steadiness score
2. **SL stance EO/EC, both sides** (30 s × 3; 20 s screening mode)
3. **mCTSIB** (EC firm, EO firm wait order per manual: EO-firm, EC-firm, EO-foam, EC-foam; 10 s × 3 each; foam prompt/confirm) — interpret matrix in results
4. **Weight Bearing Squat** (0/30/60/90°; hold-to-capture; % load per side — needs per-foot channels for true L/R; else note limitation)
5. **Asymmetry screen** (practice L/R + scored L/R, ASI 16% flag auto)
6. **RWS** (AP & lateral; paces 1/2/3 s; durations 18/12/6 s; directional control on-axis plot)
7. **LOS 8-direction** (practice pass, then 4 trials/direction configurable; RT/MVL/EPE/MXE/DCL; composite + per-direction scores; live target UI)
8. **iBESS-style** (8 × 20 s conditions; error counter keys 1/2 for operator; COP tracing per trial)
9. **HS-mCTSIB** (head-shake conditions; metronome yaw pacing; error count)
10. **Romberg protocol** (feet together / semi-tandem / tandem × EO/EC, 30 s; time-held + sway)
Safety rails per protocol (spotter text for EC/foam/single-leg; vestibular caution/dizziness stop; trial abort).
**DoD:** each protocol runs end-to-end with guided UI; data + reports per series; test suite extended (simulated device feeds).

## Phase 3 — Scoring, reports & data export
- Reports: individual test report, **progress-over-time** (chart per metric, baseline bands), session summary, athlete progress note (BBA parity), printable HTML → PDF.
- Reference bands: literature tables from the use-case report (provenance-labeled; NO proprietary BBA norms).
- Raw export: CSV per trial (raw + computed), JSON session summary, bulk export (BBA Ctrl+F6 parity).
- Baseline management: mark a session as "baseline"; every later session shows change vs baseline + MDC flags.
**DoD:** printable report from a real session; export validated.

## Phase 4 — Training module (biofeedback)
- **Steadiness hold** (stay-in-zone % score), **target reach** (8-dir + random; LOS difficulty 25→100%), **weight-shift pacing** (1–15 s; on-demand/random/no-pacing; compliance scoring per BBA formulas: targets-reached %; single-target time-in-target %; on-demand mean transition), **seated core block**, **closed-chain** (L/R %-weight-bearing targets), audio cues, display gain.
- Progression logic: foam → EC → single-leg; targets nearer→farther.
**DoD:** 2 training games usable in session; compliance score reproduces BBA examples (63.3% test case from manual).

## Phase 5 — Athlete module
- Athlete profiles (local, anonymous-ID friendly), battery presets: **Season Baseline** (DL+SL+mCTSIB+SLS), **Weekly Monitor** (DL+SL focused), **Post-Concussion (physician-led)** (mCTSIB+iBESS-style+3×20 s EC+tandem), **RTP Tracking** (SL EC + ASI + LOS trends).
- Asymmetry dashboard (L/R series + 16% flag), return-to-performance charts, notes.
**DoD:** full season arc demonstrable on recorded data.

## Phase 6 — Multi-Modal Neuro Integration (Bertec × NeuroSync × RunTrack)
Full spec: `research/NEURO-BERTEC-INTEGRATION.md`. Summary: a local **Session Conductor** + Session Manifest links the three independent evaluations (plate COP / EEG / running form) into one doctor-facing **Athlete Multi-Modal Summary** — side-by-side context, never silent fusion; each app keeps its own medical grading; physician-led clinical governance throughout.
- 6A (no hardware needed): conductor skeleton + manifest schema + summary report skeleton from existing exports.
- 6B: offline EEG↔COP co-registration (marker anchors, ±10 ms v1 target) + NeuroSync export hooks.
- 6C (device day): Vestibular-Cortical pilot (tone-pip SIVS vEPs + alpha suppression on DragonEEG, paired with mCTSIB) — research-grade feasibility first.
- 6D: neuro-balance dual-task training (NFB × plate tasks), live cue-bus v2.
- 6E: full multi-modal summary (per-block methods/limits panels, timeline, MDC flags, "requires physician review" markers).
Evidence anchors: JAMA 2021 EEG-core multimodal index; alpha-suppression vestibular marker (PMC4808125); SIVS vEPs (PMC8490637; JNP 2026); NFB×balance (SciRep 2024). Honest constraints: no VNG/vHIT/VEMP/SOT replacement; DragonEEG-only for cortical lane; no diagnostic claims.

---

## Cross-cutting
- **Testing:** extend existing 60-test suite; simulated-device feeds for every protocol; golden metric fixtures; UI smoke captures (existing browser_check pattern).
- **Docs:** update project README (protocols, processing standard, citations), keep the safety language (research aid; physician-led for clinical decisions).
- **Hardware shopping list (small):** ~5–6 cm medium-density foam pad (mark matched lines), wooden rocker board, 4″/8″/12″ blocks, foot covers; optional second USB hub.
- **Risks:** channels/dual unknown (Phase 0 resolves); norms licensing (literature-only, cited); foam spec sourcing; session fatigue (battery kept ≤ 25–30 min for the full v1 set).

## Definition of Done (project)
A locally-run station where an operator can: run the full v1 battery → view scored trials with reference bands → store/compare sessions & baselines → print/export → run biofeedback training — with raw data integrity, cited references, and explicit safety/governance rails. No cloud. No fabricated data. Ever.

---

## Progress log

- **2026-10-16 — Phase 1 metrics engine delivered:** `metrics.py` — COP conversion, 10 Hz zero-phase low-pass, decimation to 100 Hz, path length/velocity/range/RMS, Quijoux 95 % ellipse, Romberg/foam ratios, ASI and MDC change flags; `PROCESSING_VERSION` stamps stored trials (41 tests).
- **2026-10-16 — Phase 2 protocol engine delivered:** `protocols.py` — protocol catalog and the session/trial runner (condition sequencing, trial keep/discard/repeat, quality detectors) (63 tests).
- **2026-10-16 — Phase 6A skeleton delivered:** `conductor/` — session manifest plus the multi-modal summary skeleton built from existing exports, no hardware needed (88 tests).
- **2026-10-16 — Verification tooling delivered:** `tools/verify_cop.py` — read-only operator sign/scale verification against the live plate, worked from empty/loaded deltas (77 tests).
- **2026-10-16 — Integration wave in progress:** service/API/UI wiring — protocol endpoints, COP channels (`mx_n`, `my_n`, `copx_mm`, `copy_mm`) in snapshot/trace, dashboard surfaces (full suite 336 green at this point).
- **2026-10-16 — Pending:** operator zero/scale check on the first live unit (AM6147-E, serial 00000000-S017620, fw1101) before any absolute COP value is trusted.
