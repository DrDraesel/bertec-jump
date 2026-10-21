# Neuro × Bertec × RunTrack — Multi-Modal Integration Addendum

**Status:** SPEC (implementation plan) · 2026-10-15 · folds into `IMPLEMENTATION-ROADMAP.md` as Phase 6
**Concept:** three independent, honestly-framed evaluations — **Bertec** (balance/vestibular, COP), **NeuroSync** (EEG), **RunTrack** (running form) — joinable into one session picture for a physician's review. Each app keeps its **independent, medically-graded evaluation**; the association is *context for the doctor*, never silent fusion into a diagnosis.

---

## 1. Evidence anchors (verified during research — no guessing)
- **JAMA Network Open 2021** — multimodal concussion index with **handheld EEG at its core** classified athletes on injury day with high accuracy; used resting eyes-closed EEG + neurocognitive + vestibular data. => the *architecture* (resting qEEG + balance + symptoms) is published and replicable locally (as research; the commercial validated index itself is not ours to reproduce).
- **PMC4808125** — vestibular cortical processing = **alpha-band suppression over temporo-parietal regions**, significantly reduced in bilateral vestibular loss. => EEG can index the cortical side of vestibular processing.
- **PMC8490637 / J Neurophysiol 2026** — **sound-induced vestibular stimulation (SIVS)** evokes **cerebral vestibular-evoked potentials** (Na/Pa ≈ 20–30 ms; N*/P* ≈ 41–54 ms) recordable on scalp EEG — *no motion platform needed*. Lab reference setups: 32–192 ch; ours (DragonEEG 24ch) is entry-level research grade.
- **Cavanaugh 2005** — COP regularity (ApEn) changes post-concussion even when standard balance scores look normal.
- **Nature Sci Rep 2024** — SMR neurofeedback + somatosensory exercise improved balance/physical performance; supporting studies for NFB + standing postural tasks (PMC10360181). Early-stage evidence — frame training as engagement adjunct, not therapy.
- **Peterson 2003 / Cavanaugh 2006 / Goble 2016** — postural battery recovery curves & detection values (already cited in the main report).
- Foundation stance (non-negotiable): **physician-led for clinical decisions** (Amsterdam 2022/SCAT5); this stack supports, screens, tracks — it never diagnoses or clears.

## 2. What the combination ADDS vs what it still CANNOT do

**Adds:**
1. Multi-modal concussion/brain-function profile (EEG × balance × symptoms) — orthogonal signals; balance alone is ~64% sensitive.
2. Vestibular-*cortical* layer (alpha suppression + vEPs) on top of the plate's vestibulo-spinal tests.
3. Neuro-balance dual-task training (NFB while performing plate tasks).
4. A doctor-consumable multi-modal summary across the three lab apps.

**Cannot:**
- Replace VNG / vHIT / VEMP(EMG) / caloric — inner-ear mechanics & reflex pathways need their own instruments.
- Replace SOT/MCT — mechanical platform required.
- Upgrade BrainBit (4ch) into the cortical-research lane — that lane is DragonEEG-only (24ch).
- Make any diagnostic statement. Ever.

## 3. Architecture (local-only)

```
        ┌───────────────────────── Session Conductor (new, local) ─────────────────────────┐
        │  shared monotonic clock · protocol state machine · event markers (JSONL)         │
        └──────┬───────────────────────┬──────────────────────────┬────────────────────────┘
               ▼                       ▼                          ▼
      Bertec Jump Lab          NeuroSync (EEG)              RunTrack (optional day block)
      (FZ/MX/MY→COP @1kHz)     (250–1000 Hz, BrainBit/      (camera form session,
                               DragonEEG)                    independent scoring)
               │                       │                          │
               └───────── Session Manifest (JSON) ────────────────┘
                 links: trial IDs · eeg files · markers · athlete ID · session notes
                               │
                     Offline Co-Registration & Metrics
                 (resample EEG ↔ COP timebase; vEP averaging; alpha suppression;
                  postural/cortical coupling; dual-task cost)
                               │
                     Athlete Multi-Modal Summary (HTML/PDF + JSON)
                     (per-block results, each labeled with method + limits;
                      timeline vs baseline; “physician review required” sections)
```

- **Sync approach v1 (offline):** both streams recorded with arrival timestamps on a shared monotonic clock; event markers written by the conductor into every stream; co-registration = resample EEG to 1 kHz (or COP to EEG rate) using marker anchors. v1 accuracy target ±10 ms; marker-based alignment + known cue latencies.
- **Sync approach v2 (live):** OSC/websocket cue-bus between apps for real-time NFB×balance interplay. Research milestone, not v1.
- **File contract:** `sessions/<date_id>/manifest.json` + per-app raw artifacts, all under `projects/` local disk. No cloud, no external accounts.

## 4. Combined protocols (v1 set)

| Protocol | Apps used | Output | Evidence posture |
|---|---|---|---|
| Multi-Modal Baseline | EEG (rest EC/EO 5–10 min) + Bertec (DL/SL + mCTSIB) + symptom form | locally-computed multi-modal profile: band powers, sway metrics, asymmetry, symptom total; baseline stored | research replication of published architecture; not the commercial index |
| Vestibular-Cortical Module | DragonEEG + tone-pip stimulus (SIVS) + Bertec mCTSIB | averaged vEPs (Na/Pa, N*/P*), alpha-suppression index; paired with mCTSIB conditions | research-grade; entry-level channel count; needs pilot validation on-device |
| Neuro-Balance Training | NeuroSync NFB (SMR/mu up-train) + Bertec training tasks | EEG regulation scores + COP compliance/steadiness; dual-task cost | training adjunct (early evidence); engagement-focused |
| Athlete Day Block (optional) | RunTrack form session pre/post + balance/EEG snapshots | RunTrack's independent form report + pre/post balance & EEG deltas | each block independently graded; summary associates, never fuses |

**SIVS mini-spec (research):** 105 dB, 500 Hz tone pips (~500 ms ISI jittered, 100–200 trials, ear/transducer per equipment available — bone-conduction preferred where possible), eyes-closed seated; analysis: FCz & posterior montages; averaging epochs −50→300 ms; compare vs matched control tones; log cVEMP-position electrodes only if future EMG rig exists. Pilot feasibility on DragonEEG must be demonstrated before any claim.

## 5. Doctor-facing association (“helpful to the doctor” requirements)
1. **One summary export** = per-app blocks, clearly separated, each with: method, what was measured, reference bands used (with provenance), change vs baseline + MDC flags, explicit limitations panel, and "requires physician review" markers.
2. **No cross-app causal claims** — association is side-by-side context (e.g., “balance asymmetry flagged L; EEG baseline unchanged; form review shows pelvic drop R” — the physician connects, not the software).
3. **Timeline view** across all sessions per athlete/patient.
4. **Standard clinical-style formatting** for the summary (one-page print, UTF-8, local PDF), plus each app's native export untouched (independence preserved).

## 6. Phasing (roadmap Phase 6 breakdown)
- **6A (now–no hardware needed):** session conductor skeleton + manifest schema + summary report skeleton (join existing exports into one HTML/PDF); unit tests with synthetic fixtures.
- **6B (after Phase 1 metrics):** offline co-registration utilities + marker plumbing; NeuroSync export hooks.
- **6C (device day):** pilot the Vestibular-Cortical module on DragonEEG (feasibility, artifact assessment); refine.
- **6D:** Neuro-balance training mode (live cue-bus v2); dual-task metrics.
- **6E:** full Athlete Multi-Modal Summary per §5; validation notes + limitations page.

## 7. Sources
JAMA Netw Open 2021 (jamanetwork.com/journals/jamanetworkopen/fullarticle/2776443) · PMC4808125 · PMC8490637 (VestEP review) · J Neurophysiol 2026 (jn.00116.2026) · Cavanaugh 2005 (BJSM ApEn) · Peterson 2003 (PMID 12855925) · Goble 2016 · Nature Sci Rep 2024 (s41598-024-74980-7) · PMC10360181 (NFB + standing postural control). Full citations trail: `USE-CASES-REPORT.md` §7.
