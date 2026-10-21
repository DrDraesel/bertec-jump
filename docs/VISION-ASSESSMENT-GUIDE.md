# Vision assessment — operator guide

How to run a camera-only assessment with this lab, what each view actually
measures, how long to record, and what the numbers may and may not be used
for.

This lane is a **research and training tool**. It is not a medical device, it
does not diagnose, and it does not clear anyone for sport. Every automated
value is an estimate (pose, audio activity) or a proxy (face landmarks). A
qualified clinician interprets the session; any decision remains
physician-led.

Related documents:

- `research/VISION-BALANCE-RESEARCH.md` — accuracy, reliability and validity
  evidence behind the limits quoted here.
- `research/NORMS-AND-NEURO-INTERPRETATION.md` — which published norms are
  transferable and which are not.
- `research/VISION-V2-SPEC.md` — the frozen v2 data and API contract.

---

## 1. Before the athlete arrives

1. Start the app (`Start Jump Lab.cmd`), open <http://127.0.0.1:8765> and choose
   the **Vision-only** tab when the plate is not in use. The plate is not
   required for any test in this guide.
2. Mount the camera at **hip height**, **2.5–4 m** from the athlete, with the
   whole body (head to feet) in frame for every stance. A phone on a tripod is
   enough; a laptop webcam is a last resort because of its fixed low angle.
3. Light the athlete from the front, not from a window behind them. Avoid loose
   clothing that hides the knee and ankle, and keep the background plain.
4. Check the live feed: all 33 landmarks should track, the feet must stay
   inside the frame, and the reported processing rate should be steady near
   30 fps. Fix framing before recording — a session with a foot outside the
   frame cannot be scored.
5. Confirm consent **before** recording: camera first, then — only if the
   athlete agrees — audio/speech-to-text and face analysis. All three default
   to **OFF**; audio is off unless it is both enabled and consented, and the
   kinematics are unaffected when it stays off. Transcripts are stored locally
   as text and face values are landmark proxies, never images.

### View choice

The **side or oblique (30–45°) view at hip height is the default** for a
single camera. Add a **front view** when face landmarks or frontal-plane
measures matter.

| View | Camera position | What it measures best | Limits |
| --- | --- | --- | --- |
| Side (90°) | Hip height, 2.5–4 m, athlete side-on | Sagittal knee, hip and ankle pose estimates; trunk lean; pelvic tilt; trunk AP sway; squat and sit-to-stand mechanics | No frontal-plane angle (no valgus metric exists); lateral sway not measured; the far limb is partly occluded |
| Oblique (30–45°) | Hip height, 2.5–4 m, 30–45° off the sagittal plane | A compromise: sagittal pose estimates plus some frontal information; trunk AP motion tracks well | Lateral (ML) trunk motion is weaker than AP, so do not read ML sway from one oblique camera |
| Front (0°) | Hip height, 2.5–4 m, facing the athlete | Lateral trunk sway, stance width observation, face landmarks | Sagittal pose estimates are foreshortened and unreliable; no valgus angle is computed; no depth information |
| Front, close (face) | Chest/eye height, about 1.5–2 m | Face-landmark proxies only (if face analysis is enabled and consented) | Not for body kinematics; the face box must stay in frame |

Two cameras (side + front) give the most complete picture, but each view is
analysed separately and nothing here fuses them into a 3-D reconstruction.

**Do not** move the camera between trials of the same condition. If it moves,
start a new session so the comparison is not silently invalidated.

---

## 2. Recommended vision-only battery

Run the tests in this order. Keep footwear, foot position, stance width and
visual target the same across trials, and rest 30–60 s between trials.

| # | Test | Trials × duration | What the camera scores | Needs |
| --- | --- | --- | --- | --- |
| 1 | Double-leg stance, eyes open | 2 × 30 s | Pose estimates during quiet standing | Side or oblique view; firm floor |
| 2 | Double-leg stance, eyes closed | 2 × 30 s | Pose estimates during quiet standing | Spotter within arm's reach; same view |
| 3 | Single-leg stance, left | 3 × 30 s | Pose estimates during the hold; **hold time is yours to time** | Spotter; stopwatch; side or oblique view |
| 4 | Single-leg stance, right | 3 × 30 s | As test 3 | Spotter; stopwatch; same view |
| 5 | Tandem stance | 2 × 30 s | Pose estimates during quiet standing | Spotter; side or oblique view |
| 6 | Squat | 1–2 sets × 5 reps | Knee/hip/trunk pose estimates during the movement; **reps are yours to count** | Side or oblique view; full body in frame |
| 7 | Sit-to-stand | 5 reps | Pose estimates during the movement; **reps are yours to count** | Chair; side view; the chair must be in frame |
| 8 | Walk | 2 × 10 m | Posture inside the framed section; **no gait metrics at all** | 10 m clear space; stopwatch; camera frames the middle 5–6 m |

Instructions for the camera-only tests: stand relaxed, feet hip-width, arms by
the sides, look at a fixed target at eye height, and say nothing unless asked.
Single-leg: stand on the named leg, hands on hips, the other foot off the floor
and not touching the standing leg; stop the trial if balance is lost.

Notes:

- **Hold time, reps and gait events are NOT scored by the camera.** The pose
  pipeline has no foot-lift detector, no repetition counter and no gait
  segmentation, so it cannot produce those outcomes. Time the hold or the walk
  with a stopwatch (or use the plate), count the reps yourself, and record each
  result as an **operator mark** — that mark, not the camera, is the record.
- **Single-leg: three trials per limb, not one — six trials in total** (left
  then right). Single-leg measures are the noisiest, and a familiarisation
  effect is documented — a second session is needed before a change is read as
  training response (Kozinc et al. 2025).
- **Tandem stance** is the hardest double-leg condition; keep the spotter close
  and stop the trial on a step-off rather than letting the athlete recover at
  the edge of balance.
- **Squat and sit-to-stand** are movement tests, not balance tests. Five reps is
  a screening dose, not a strength test.
- **Walk 10 m** only if the room allows it. If not, skip it rather than
  recording a partial walkway. The camera observes posture in frame; it does
  not measure speed, step length, cadence or gait events.
- Stopwatch-timed single-leg stance has a ceiling around 45 s (Springer et al.
  2007), so 30 s trials avoid the ceiling and still match current single-leg
  protocols. Keep the same timing method across visits.

### If you also run mCTSIB (plate sessions)

mCTSIB has four conditions — firm eyes open, firm eyes closed, foam eyes open,
foam eyes closed — which are CTSIB conditions 1, 2, 4 and 5 with the
visual-conflict dome dropped.

- **A foam pad is mandatory for conditions 3 and 4** (foam eyes open and foam
  eyes closed). A bare plate cannot run them; report only the firm conditions
  and say so.
- Use a medium-density viscoelastic foam, typically about 40.6 × 40.6 × 7.6 cm.
- The clinical standard is **30 s per condition, three trials averaged**; the
  instrumented literature commonly uses 10 s. Choose one and keep it fixed.
- mCTSIB is a screen, not a vestibular diagnosis: there is no sway-referenced
  platform and no moving visual surround, so it cannot isolate vestibular
  function the way SOT conditions 5–6 do.

---

## 3. How long to record, and why

Thirty seconds is the default COP acquisition window, but it is a compromise,
not a law:

| Evidence | Consequence for this lab |
| --- | --- |
| ISPGR standard recommends **at least 25 s** acquisition and at least 50 Hz sampling (Scoppa et al. 2013) | 30 s satisfies the acquisition floor with margin |
| **20–30 s** gave optimal test–retest reliability in an early duration study (Le Clair & Riach 1996) | 30 s is the default screening length |
| RMS **increases** and mean power frequency **decreases** as duration grows from 15 to 120 s, while reliability improves (Carpenter et al. 2001) | A longer trial is not the same measure as a short one; do not compare across durations |
| A systematic review concluded **≥90 s** is needed for acceptable reliability of most COP parameters (Ruhe et al. 2010) | For area, RMS or frequency endpoints, use 60–90 s and say so |
| Single-leg protocols use **30 s** trials; ICC 0.71–0.86 (Kozinc et al. 2025) | Keep 30 s for single-leg velocity screening; never apply double-leg MDC to single-leg data |
| COP–COM needed **four trials** to reach ICC 0.94 (AP) / 0.90 (ML) in healthy elderly (Corriveau et al. 2000) | If COP–COM is the endpoint, collect four trials, not one |

Rules of thumb:

- **Velocity screening:** 30 s.
- **Area, RMS or frequency:** 60–90 s, fixed across the programme.
- **Never compare path lengths recorded over different durations** without
  converting to velocity.
- **Never present a change smaller than the MDC** as improvement or decline;
  borrowed MDC constants are provisional until this lab runs its own
  test–retest study. The app keys MDC by stance: the published COP constants
  are **single-leg** values (9.0 mm/s absolute for velocity from Vindis 2026,
  ~50 % of baseline for area), and they are *not* applied to double-leg trials.
- **COP–COM here is a low-pass estimate, not the validated gravity-line
  projection (GLP).** The report says so; GLP (zero-point-to-zero-point double
  integration) is the upgrade path, not something already computed.

Camera-side recording length follows the same window. A 30 s trial at 30 fps is
about 900 frames; the report quotes the frames, coverage and **mean processing
rate** it actually received. That rate counts frames the pose worker processed
(capped by its frame-rate limit); it is not the camera's capture rate, and the
reported inference+encode latency excludes camera capture, so neither is a
measure of camera exposure age.

---

## 4. Running audio

Audio, speech-to-text and face analysis are **off by default**. They start only
when the operator enables them for the session *and* the athlete has consented;
leave them off and the kinematics are unaffected. When audio is on:

1. Use the built-in microphone or a USB microphone on the same computer.
   Keep it within about 1–2 m of the athlete and away from the camera fan.
2. Record in a quiet room. Air conditioning, crowds and traffic make the
   speech-activity flag useless and the transcript worse.
3. Tell the athlete, before recording, what the microphone is for and that
   they may decline. If they agree, give them a simple script:
   *"If anything hurts, say 'this hurts'; if you need to stop, say 'stop'. You
   do not have to say anything else."* Do not coach them to produce a keyword.
4. Speech-to-text is **local machine transcription**. It may mishear words,
   switch language, drop phrases or invent punctuation. Transcript excerpts in
   the report are a record of what the recogniser produced, not a quotation and
   not clinical evidence.
5. Keyword matches ("pain", "stop", "no entiendo", …) are **pattern matches on
   that transcript**. They mean a word appeared; they do not mean the athlete
   was in pain, did not understand, or wanted to stop. The report labels them
   "keyword match: … (local STT, may be wrong)" and shows the matched phrase.
   The operator mark is the record of what actually happened.
6. Audio levels are reported in dBFS (0 dBFS is the digital ceiling). A peak
   near 0 dBFS means clipping: lower the input gain and re-record if the trial
   matters.

Face landmarks are the same kind of optional extra: off by default, enabled only
with consent, computed as four landmark-geometry proxies (mouth opening, brow
raise, brow furrow, eye squint) on a 0–1 scale. They are not an emotion
measurement and not a pain measure.

If audio capture is unavailable, the report says so and the kinematics are
unaffected. Never fabricate a transcript or infer speech from an empty audio
block.

---

## 5. Operator marks

While a session runs, the operator can attach three kinds of mark:

| Mark | Use it when | What it is not |
| --- | --- | --- |
| **Pain** | The athlete says something hurts, or points to a painful area | Not a pain measurement, not a diagnosis, not a severity score |
| **Did not understand** | The athlete asked for the instruction again, or clearly did not follow it | Not a comprehension or cognitive test |
| **Note** | Anything else worth keeping: a stumble, a cough, a phone ringing, a change of footwear | Not a measurement |

Marks are timestamped relative to the session start and appear in the report
next to the automatic observations. Write them at the time; do not reconstruct
them from memory afterwards.

---

## 6. What the system can and cannot infer

**It can describe:**

- Pose-estimated sagittal joint positions and angles (mean, SD, min, max) and
  trunk/pelvic angles — labelled as estimates, in the pose worker's own
  image-plane units. Markerless 2-D knee angles show a static RMSE of **15.49°**
  and an ICC of **0.136 during single-leg stance**, so quiet standing is the
  *worst* case for this measurement, not the best: differences below about 15°
  are noise.
- Trunk-sway ranges in the pose worker's own coordinate units (short-term
  relative sway, not total sway).
- Frame counts, pose coverage, mean processing rate and recording duration.
- Whether the microphone picked up speech-like activity, how loud it was, a
  local transcript and keyword matches (only when audio was enabled and
  consented).
- Face-landmark geometry proxies (mouth opening, brow raise/furrow, eye squint)
  on a 0–1 scale from a front view (only when face analysis was enabled and
  consented).
- Operator marks and worker events (pose lost, low visibility, loud audio, …).

**It cannot infer:**

- A diagnosis of any kind, including vestibular loss, concussion, neuropathy or
  Parkinsonism.
- Pain, emotion, fear of falling, fatigue or effort from face or voice.
- Fall risk, injury risk or readiness to return to sport.
- Centre of pressure, a balance score, or a sway-referenced/vestibular
  isolation.
- Per-limb loading, joint moments or muscle activation.
- Any frontal-plane knee valgus angle — no valgus metric exists, not even as a
  change score.
- Hold time, repetition counts or gait events — no foot-lift, rep or gait
  segmentation exists in the camera pipeline.
- Reliable foot-lift timing — ankle and foot landmarks are the least accurate.
- Reliable lateral (ML) trunk motion from a single oblique camera.

If a question needs any of the above, it is a clinical question for a
clinician, not a report field.

---

## 7. Data and privacy

- Vision-only sessions are stored **locally** under
  `data/vision_sessions/<session-id>/`: `session.json` (metadata, summary,
  events), `frames.jsonl` (one line per frame) and `report.html`.
- Nothing is uploaded. There is no cloud path, no analytics and no telemetry.
- There is **no encryption at rest and no authentication**: anyone with access
  to this Windows account or the filesystem can read the derived session data.
  Treat the folder as identifiable health-adjacent data.
- **Camera, audio, speech-to-text and face analysis are off by default.** Each
  starts only when the operator enables it for the session after the athlete
  consents; enabling them is the consent decision, so do not pre-enable them
  "for convenience".
- Transcript excerpts are stored **text**, not audio: raw waveforms are never
  written. If the athlete does not consent to audio, leave the microphone off.
- Face analysis stores four landmark proxies per frame, never the face image or
  the raw landmark mesh; if the athlete does not consent, leave it off.
- Deleting the session folder deletes the session. Copy a report before
  deleting if it is needed for records.
- Do not expose port 8765 to a LAN, the internet or a tunnel. The app binds to
  loopback only and is not a HIPAA-compliant storage system.
- The camera feed itself is not stored by this app; only derived frame metrics,
  audio aggregates/transcript text and face proxies are written.

---

## 8. Pose model choice

| Backend | Licence | Status here |
| --- | --- | --- |
| **MediaPipe Pose Landmarker, Full** | Apache-2.0 | **Default.** Measured at about 7.2 ms/frame on this workstation, 33 landmarks, runs on CPU |
| YOLO-pose (Ultralytics) | AGPL-3.0 | Same accuracy class for single-person pose; the licence is the reason it is not the default |
| RTMPose-m | Apache-2.0 | Documented accuracy-escalation path if a task needs more precise landmarks |

No claim is made that one backend is clinically superior. The backend and model
name are recorded with the frames; do not compare sessions recorded with
different backends as if they were the same instrument.

---

## 9. Session quality checklist

Before you stop a session, confirm:

- [ ] All landmarks tracked for the whole trial (coverage comfortably above
      80 %).
- [ ] Head and both feet inside the frame at the lowest and highest point of
      the movement.
- [ ] Camera did not move during the condition.
- [ ] Same footwear, stance width and target as the comparison session.
- [ ] Trial length matches the endpoint (30 s velocity screen; 60–90 s for
      area/RMS/frequency).
- [ ] Spotter present for eyes-closed, single-leg and tandem trials.
- [ ] Hold time / reps / walk time counted by the operator and written as an
      operator mark (the camera does not score them).
- [ ] Operator marks written at the time, with a note that explains them.
- [ ] Audio and face analysis consented, or both left off (they are off by
      default).

A session that fails a check is not "bad data" to be deleted silently — record
why it failed in a note, and do not compare it against a clean session.

---

## 10. References

- Carpenter MG, Frank JS, Winter DA, Peysar GW. Sampling duration effects on
  centre of pressure summary measures. *Gait Posture.* 2001;13(1):35–40.
  <https://pubmed.ncbi.nlm.nih.gov/11166552>
- Corriveau H, Hébert R, Prince F, Raîche M. Intrasession reliability of the
  "center of pressure minus center of mass" variable of postural control in the
  healthy elderly. *Arch Phys Med Rehabil.* 2000.
  <https://pubmed.ncbi.nlm.nih.gov/10638875>
- Goble DJ, Conner NO, Nolff MR, et al. Test–retest reliability of the BTrackS
  mCTSIB protocol across multiple time durations. *Med Devices (Auckl).*
  2021;14:355–361. <https://pmc.ncbi.nlm.nih.gov/articles/PMC8604651>
- Kozinc Ž, Kuliqi A, Obal I. Reliability of postural sway measures during
  single-leg stance: conventional and complementary error metrics. *Sport Sci
  Health.* 2025;21:3173–3179.
  <https://link.springer.com/article/10.1007/s11332-025-01527-2>
- Le Clair K, Riach C. Postural stability measures: what to measure and for how
  long. *Clin Biomech.* 1996;11(3):176–178.
  <https://www.sciencedirect.com/science/article/abs/pii/0268003395000275>
- Ruhe A, Fejer R, Walker B. The test–retest reliability of centre of pressure
  measures in bipedal static task conditions — a systematic review. *Gait
  Posture.* 2010;32(4):436–445.
  <https://pubmed.ncbi.nlm.nih.gov/20947353>
- Scoppa F, Capra R, Gallamini M, Shiffer R. Clinical stabilometry
  standardization: basic definitions — acquisition interval — sampling
  frequency. *Gait Posture.* 2013;37(2):290–292.
  <https://pubmed.ncbi.nlm.nih.gov/22889928>
- Springer BA, Marin R, Cyhan T, et al. Normative values for the unipedal
  stance test with eyes open and closed. *J Geriatr Phys Ther.* 2007;30(1):8–15.
  <https://geriatrictoolkit.missouri.edu/balance/Normative_Values_for_the_Unipedal_Stance_Test_Springer-JGPT.pdf>
- Watson S, Trudelle-Jackson E. Test–retest reliability and minimal detectable
  change of the instrumented mCTSIB in healthy older adults. *J Geriatr Phys
  Ther.* 2021;44(4):183–188. <https://pubmed.ncbi.nlm.nih.gov/32618856>
- Yang Y, Chang E, Yoo B. Video-based trunk motion tracking during standing
  balance. *Sci Rep.* 2026. <https://www.nature.com/articles/s41598-026-68104-6>
