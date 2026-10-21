# Independent Adversarial QC Review — `training.py` (Phase 4 training engine)

**Repo:** `C:\Users\GAMEPOWER\projects\bertec-jump`
**Reviewer:** independent adversarial QC subagent (read-only; this report is the only file created)
**Review window:** 2026-10-16, ~07:44–07:55 EDT
**Scope (as briefed):** `training.py` + `tests/test_training.py` — the Phase 4 module that entered the build unreviewed (prior report `research/QC-REVIEW-2026-10-16.md`, §0/P0: "`training.py` slipped into the build un-reviewed … needs its own scoped review"). This report closes that gap.

---

## 0. Revision under review

| File | SHA-256 (prefix) | Size / lines | Final mtime |
|---|---|---|---|
| `training.py` | `685c07ca7fd1…` | 44,507 B / 937 | 2026-10-16 07:36:09 |
| `tests/test_training.py` | `52f85283d1f3…` | 42,381 B / 900 | 2026-10-16 07:37:34 |

Both hashes were re-checked after all probes — **unchanged during the review** (tree stable; unlike the 07:28–07:37 window the prior report had to navigate).

**Suite counts observed (my own runs, `PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests`, `.venv` Python 3.11.15, ~14.6 s):**
- **439 tests, OK** — full suite.
- `tests/test_training.py` alone: **59 tests, OK** (the "~59 tests" claim checks out exactly).
- Per-file discovery sums to 439 (18 analysis, 8 api, 11 bridge, 88 conductor, 15 frontend, 16 frontend_protocol, 48 metrics, 10 protocol_api, 18 protocol_reports, 63 protocols, 8 service, 59 training, 77 verify_cop).
- Cosmetically corrects prior report §0: the two counts observed at 07:37 were attributed the other way round (`test_training.py` = 59, `test_protocol_reports.py` = 18; total 439 either way).

---

## 1. Methodology (what was actually run)

- Full-suite and per-file unittest discovery runs (counts above); **no repo file touched**.
- Hash/mtime pinning before and after; only this report was created.
- **Six read-only probe scripts**, run from a scratch directory OUTSIDE the repo (`.venv/Scripts/python.exe -B`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH` → repo):
  1. **Math recomputation** — the manual's 63.3 % example driven through an *independent* path (COP zone entry for the first 19 targets at 100 Hz, instead of the author's `target_hit()` calls); 7.5/10 s hold at 100 Hz; on-demand mean transition arithmetic; none-pacing flip boundary (total = 1 vs 2); rounding discriminators (2-of-3 → 66.7; 1/6 s → 0.167); zone-edge exactness (radius, radius+5e-10, radius+2e-9); sample exactly at the switch instant; late-tick duration overshoot and the 1e-9 early-complete band; 50 kHz feed; 10 s dropout hole.
  2. **State-machine attacks** — `advance()` per pacing mode; `target_hit()` in wrong modes; double `target_hit()` at one timestamp; `set_target()` under on_demand; hostile rngs (−5 / +inf / NaN uniforms; out-of-pool `choice`); pre-time operator action on a 1000-based stream; quality gate at exactly 1.0 s and stickiness; every call after stop/auto-complete; mixed `tick`+`add_sample` on one stream; catalog injection; unbounded geometry; `stop()` semantics.
  3. **Safety/metadata sweep** — the `BANNED` regex recompiled and run against 49 travel/loading/stance words and against every catalog string the test scans (66 strings); `ast`-walk over all 302 string constants in `training.py`; import audit; "research / not therapy / not a medical device" accounting; `__all__` completeness.
- Greps: repo-wide `import training|from training|TrainingSession|TRAINING_MODES` — **`training.py` is referenced by its tests only**; no service/app/static/conductor wiring exists yet.

---

## 2. Findings (ordered by severity; **no CRITICAL, no HIGH findings**)

### M1 — Silent credit bridging across stream stalls/holes inflates time-in-zone and compliance — MEDIUM
**Where:** `training.py:806-812` (`_register_position`: `dt = t - self._last_valid_t` is credited in full to the target's `time_s` / `in_zone_s`), `training.py:817-825` (`_check_quality` counts only *explicitly fed* invalid samples), `training.py:858-877` (the invalid-sample path).
**Probe:** steadiness hold with valid samples at t = 0.0 and 0.1 inside the 8 mm zone, then the next valid sample at t = 10.1 inside → `time_in_zone_s = 10.1`, `time_in_zone_pct = 100.0`, `valid_fraction = 1.0`, `quality = None`. Three samples spanning 10.1 s score as a perfect ten-second hold. A dropped stream is invisible (no samples = no evidence), and even interleaved invalid samples don't stop the surviving valid pair from spanning the hole. This is the opposite of the repo's own convention (`metrics.py` path metrics never bridge a pair spanning an invalid sample — prior report §5).
**Fix:** decide the policy before the service wave scores anything: cap the credited `dt` at a nominal cadence multiple, or raise a `stream_gap` quality flag above it (optionally excluding the span); document that the service must *feed* invalid samples across dropouts rather than omit them.

### M2 — Unbounded geometry options can configure the quiet-stance envelope away — MEDIUM
**Where:** `training.py:303-320` and `:323-327` — `max_ml_mm` / `max_ap_mm` carry only `minimum=1.0`; `zone_radius_fraction` / `min_zone_radius_mm` only `minimum=0.0`; `validate_catalog` (`training.py:426-475`) has no geometry invariant.
**Probe:** `{'max_ml_mm': 5000.0}` → target at x = 5000 mm with a 1500 mm radius; `{'max_ap_mm': 1e9}` → y = 1e9 mm; `{'min_zone_radius_mm': 1e6}` → a COP sample 500 m from the target reports `in_zone=True`, `time_in_zone_pct=100.0`; `{'zone_radius_fraction': 50.0}` → radius 625 mm. The module's hard rule ("quiet-stance only … works inside a static ankle-strategy lean", `training.py:40-44`) and the deliberately conservative ±60/±50 ellipse are therefore one option away from being meaningless, and no catalog check stops a future preset from shipping a travel-scale target.
**Fix:** add maxima (e.g. ≤ 2× the default ellipse for `max_*_mm`, ≤ 1.0 for `zone_radius_fraction`, a finite ceiling for `min_zone_radius_mm`) plus a `validate_catalog` invariant binding geometry to the envelope.

### M3 — Duplicate `target_hit()` fabricates a 0.0 s transition; `set_target()` bypasses the on_demand guard — MEDIUM
**Where:** `training.py:766-778` (`_mark_hit`: under on_demand a hit immediately switches, and the next target inherits `presented_t = t`), `training.py:879-888` (`target_hit()` passes `self._last_t`), `training.py:902-904` (`advance()` refuses on_demand) vs `training.py:910-923` (`set_target()` does not).
**Probe:** on_demand session, sample at t = 10, then `target_hit()` twice with no clock advance → targets `front (hit_t 10.0, transition 10.0)` and `front_right (hit_t 10.0, transition 0.0)`; `mean_transition_s = 5.0` instead of 10.0. On the **default** pacing (`on_demand`), a debounced/double operator input completes a target the subject never moved to and halves the headline score. Separately, `set_target('back')` under on_demand is accepted (while `advance(by_operator=True)` raises "call target_hit() instead"), silently closing an unreached target that still counts in `targets_total` (probe: total 2, reached 0).
**Fix:** reject or debounce a second hit with no clock advance (require strictly increasing `hit_t`), and give `set_target()` the same on_demand rule as `advance()` (or document it as a deliberate override); add regression tests.

### M4 — Hold's safety text sanctions single-leg work the hard rule forbids; no machine-readable spotter metadata — MEDIUM
**Where:** `training.py:342-346` ("…a spotter stands within reach for any eyes-closed or single-leg variation") vs `training.py:40-44` ("No momentum, travel or loss-of-contact exercise of any kind is defined here…") and the other two modes' safety strings (`training.py:369-374`, `:396-401`: "Both feet stay on the plate … never travels off the plate"). No `requires_spotter`/`fall_risk` field exists on `TrainingMode` (`training.py:218-231`), and `state()`/`summary()` (`:561-583`, `:595-641`) carry no safety string or spotter flag.
**Why:** single-leg is exactly the loss-of-contact work the module claims never to define; the catalog's own prose both forbids and anticipates it (the roadmap's Phase 4 progression "foam → EC → single-leg", `research/IMPLEMENTATION-ROADMAP.md:54`, shows where the sentence came from, but the engine implements none of those variations). The prior review's M9 (spotter metadata absent from live structures) repeats at this layer: nothing a running session returns mentions a spotter.
**Fix:** reconcile the text (either implement eyes-closed/single-leg variants deliberately with spotter metadata, or state double-leg-only and move the variation note out of the engine), and surface `requires_spotter`/`fall_risk` + `mode.safety` in `state()` / session records.

### M5 — The banned-regex guard is a literal blocklist with gaps (test quality, safety rail) — MEDIUM
**Where:** `tests/test_training.py:62-64` (`BANNED`), applied at `tests/test_training.py:205-213` to mode_id/label/description/scoring/safety/sources + option names/descriptions.
**Probe:** the current catalog passes — I re-ran the exact enumeration: **66 strings, 0 matches**. But the pattern misses inflections (`walking`, `jumping`, `hopping`, `lunging`, `gaits`, `impacts` all pass) and has no entry at all for `march/marching`, `run/running`, `jog`, `treadmill`, `sidestep`, `travel`, `load/loading`, `lift`, `carry`, `foam`, `single-leg`, `tandem` — any of which appearing in future mode/option text would pass the "no travel/loading modes" test. It is also context-blind: a scan broadened to the module docstring would flag the safety caution "impact-rated hardware would be required" (`training.py:43`), i.e. the list bans words, not semantics. It scans catalog metadata only.
**Fix:** use prefix/stem forms (`walk\w*|jump\w*|hop\w*|march\w*|run\w*|jog\w*|…`), add the missing families, and pair the literal blocklist with the structural option-bound invariants from M2 so the rail is not a word list.

### L1 — Hostile rng beyond finite out-of-range values — LOW
**Where:** `training.py:745-749` (`min(max(float(interval), MIN), MAX)` is not a NaN filter), `training.py:702-706` + `:713` (`rng.choice` result used directly as a `DIRECTION_VECTORS` key).
**Probe:** NaN uniform → the session never switches again (10 s random session ends with 1 target, `status complete`); `+inf` → clamps to 15.0 (confirmed); `−5` → clamps to 1.0 (confirmed); `choice` returning `'teleport'` → `KeyError: 'teleport'` out of `start()` (a non-domain crash, not a clean error). Requires a contract-violating rng (`random.Random` never does this), hence LOW — but the module advertises clamping as a defence, and NaN defeats it.

### L2 — Pre-time operator actions fabricate `presented_t = 0.0` — LOW (caller hazard)
**Where:** `training.py:776` (`moment = t if t is not None else 0.0`), `training.py:920-922` (same for `set_target`), `training.py:785-786` (first-call fill only fires while `presented_t is None`).
**Probe:** on_demand: `start(); target_hit()` before any tick → the next target's `presented_t = 0.0`; the first sample then arrives at t = 1000.0 (a non-0-based stream) and hits it → `transition_s = 1000.0`, `mean_transition_s = 1000.0`. Harmless with 0-based streams that call `tick(0.0)` first; must be a wiring note.

### L3 — `duration_s` is a trigger, not a cap — LOW
**Where:** `training.py:788-792`. **Probe:** a 2.0 s `duration_s` session fed t = 0 then t = 10 completes on the late call with `duration_s = 10.0`, closes the target at 10.0 and counts the completing sample (`samples = 2`); completion also fires inside the 1e-9 band below the deadline (t = 2.0 − 5e-10 → complete). Documented as sample-derived, but untested and it must be reflected in the UI (show `state()['elapsed_s']`).

### L4 — Mutable catalog, import-time validation only — LOW
**Where:** `training.py:408-410` (plain dict), `:418-423` (`get_training_mode` does not re-validate), `:478` (validation at import). **Probe:** an injected `TrainingMode` with an unknown direction is returned by `get_training_mode` and accepted by `TrainingSession(...)`; it fails late with `KeyError` at `start()`. Mirrors prior report M3 for `PROTOCOLS`.

### L5 — All pinned scores are truncation-insensitive — LOW (test quality)
**Where:** `training.py:610-611, 614-615, 625-626` use `round(…, 1|3)`; the pinned test values are 63.3 / 75.0 / 33.3 / 50.0 / 100.0 (`tests/test_training.py:417, 486, 638, 655`). **Probe:** the engine correctly returns 66.7 for 2-of-3 reached and 0.167 for a 1/6 s mean transition — but no test pins a value whose second decimal is ≥ 5, so a truncating implementation would pass every existing score assertion.

---

## 3. Top must-fix

1. **M1 — Stall/gap credit policy:** cap or flag the credited span across missing data (`stream_gap`), and state the placeholder-sample contract — decide this before any score is shown to users (`training.py:806-812`).
2. **M2 — Bound the geometry:** maxima on `max_ml_mm` / `max_ap_mm` / `zone_radius_fraction` / `min_zone_radius_mm` + a `validate_catalog` invariant so the quiet-stance envelope cannot be configured away (`training.py:303-327, 426-475`).
3. **M3 — Operator-input guards:** no second `target_hit()` without a clock advance; `set_target()` must obey the same on_demand rule as `advance()` (`training.py:766-778, 879-888, 902-923`).
4. **M4 — Safety-text reconciliation + metadata:** resolve the single-leg/eyes-closed sentence against the hard rule and add `requires_spotter`/`fall_risk` surfaced by `state()`/session records (`training.py:342-346 vs 40-44`).
5. **M5 — Harden the guard test:** inflections + missing word families, and add the five missing tests in §4 (`tests/test_training.py:62-64, 205-213`).

---

## 4. Five most load-bearing MISSING test cases (adversarially chosen)

1. **Stall / long-gap credit:** valid sample inside at t0, next valid 10 s later → assert the credited-time policy. Today: 100 % in-zone, no flag (probe; `training.py:806-812`).
2. **Duration boundary on a stalled stream:** 2 s `duration_s` fed t = 0 then t = 10 → assert status, `duration_s`, credit and sample-count semantics (`training.py:788-792`).
3. **Mixed single-stream drive:** `tick()` for pacing heartbeats + `add_sample()` for the samples (the documented integration pattern), including the duplicate-t rejection — the suite uses either `drive()` or `feed()`, never both in one session.
4. **Rounding discriminator:** 2-of-3 reached → 66.7 and a mean transition of 1/6 s → 0.167 (the engine returns these; no test pins a ≥ 5 second decimal, so truncation passes today).
5. **Operator abuse paths:** double `target_hit()` at one timestamp (probe: mean 10.0 → 5.0) and `set_target()` under on_demand (skips an unreached target while `advance()` refuses).

---

## 5. Verified sound (coverage evidence — what passed)

- **Manual example (the author's headline claim):** `tests/test_training.py:468-488` pins 30 targets / 19 reached / **63.3 %** / 60.0 s, and I **reproduced it through an independent path** — COP zone entry for the first 19 targets at 100 Hz (`compliance 63.3, total 30, reached 19`); the window grid is presented_t = 0, 2, …, 58 with the 60 s completion firing before the switch due at 60, so exactly 30 targets exist. 100×19/30 = 63.33… → `round(…, 1)` = 63.3 confirmed.
- **Single-target time-in-zone:** 7.5 s of 10 s → 75.0 reproduced at 100 Hz (test uses 10 Hz); best-target `test_none_pacing_single_target_scores_time_in_zone` also 75.0.
- **On-demand mean transition:** arithmetic and 3-dp rounding verified (probe 0.2/0.3/0.05 → 0.183; 1/6 s → 0.167); `compliance_pct` correctly None under `mean_transition`.
- **None-pacing flip boundary:** total = 1 → `time_in_zone` (100.0); total = 2 → `targets_reached` (50.0) — probe-confirmed at the exact boundary.
- **Zone geometry:** radius = max(8 mm floor, 0.30 × distance) verified at difficulty 1.0 (r = 15 at 50 mm AP) and 0.25 (floor 8 mm; 12.5 mm AP target); ±60/±50 ellipse at difficulty 1.0 verified (`tests/test_training.py:698-712`); membership **inclusive with a real 1e-9 tolerance** (probe: radius + 5e-10 inside, + 2e-9 outside).
- **Pacing clamps + deterministic replay:** seeded `random.Random` reproduces identical sequences (`tests/test_training.py:525-540`); out-of-band uniforms clamp to the 1–15 s band (probe: −5 → 1.0, 20 → 15.0, +inf → 15.0; test covers 0.0/20.0); random pacing resyncs after a late tick exactly like fixed (probe: tick 7.5 → single switch, next at 9.5).
- **State-machine guards (probes + suite, all clean):** double `start()`; every call before start / after stop / after auto-complete (RuntimeError); backwards, duplicate, NaN, ±inf timestamps on both `tick` and `add_sample` (ValueError); wrong-type / wrong-mode / out-of-range directions (TypeError/ValueError with no partial state change); unknown options, wrong types, inverted pacing bounds; `advance()` without `by_operator`; `advance()` under on_demand; `stop()` idempotent from idle/running/complete and closes the open target.
- **Quality flag:** gate opens at exactly `t − t0 = 1.0` (not 0.999), fires on strictly > 50 % invalid, sticks for the rest of the session, and later clean samples do not clear it (probes; `tests/test_training.py:782-797`). None/NaN/inf COP samples count invalid without raising; `in_zone` is None before any valid sample and right after a switch; `valid_fraction` = 0.2 exact in the probe.
- **Defensive copies:** `state()`, `summary()`, `targets()`, `options` — mutating returned dicts never reaches internal state (probe + `tests/test_training.py:291-297, 851-880`).
- **Wall-clock isolation:** `now_fn` feeds only `wall_elapsed_s`; scoring is strictly timestamp-driven (probe: 900 s wall vs 1.0 s sample duration; `tests/test_training.py:882-896`).
- **Safety rails:** **no hardware/SDK/service imports** — imports are `collections.abc`, `copy`, `dataclasses`, `math`, `random`, `typing` only (ast audit); "research / not therapy / not a medical device" present in the module docstring and in **all three** mode safety strings (4× each); the guard test's exact enumeration scans **66 catalog strings with 0 matches** today; a semantic sweep for non-blocked travel/loading words found only benign contexts ("running" status, weight shifting, negative cautions).
- **Hygiene:** `__all__` complete (29/29 resolve); catalog validates at import; frozen dataclasses and tuple options reject mutation (`tests/test_training.py:215-224`); zero-target/empty-session paths return None instead of dividing by zero (`tests/test_training.py:811-821`).
- **Suite green:** 439/439, including 59/59 for this module.

---

## 6. Integration readiness — what the service/UI wiring wave must be told

(`training.py` is currently referenced by its tests only — no service/app/static wiring exists yet.)

1. **Frames and signs:** the engine's training frame is `copx_mm` = +x to the subject's right, `copy_mm` = +y anterior (`training.py:47-52`). Live-plate COP sign verification is still an open item (USE-CASES report §6 q2) — the service must map *verified* hardware COP into this frame (and stamp the mapping version) before feeding `add_sample`; do not feed raw bridge values blind.
2. **Drive contract:** one strictly increasing time stream per session. `add_sample()` is the hot path (it performs pacing switches, target hits and completion itself); `tick()` is only for clock heartbeats when samples are silent — never both for the same `t`. Prefer session-relative 0-based seconds and call `tick(0.0)` (or feed the first sample) immediately after `start()`: pre-time operator actions fabricate `presented_t = 0.0` (L2), and a non-0-based stream turns the next transition into a 1000 s outlier.
3. **Missing-data honesty:** feed invalid samples (None/non-finite) across channel dropouts — the engine cannot see a silent stall and will credit the whole gap to the last membership (M1); carry `valid_fraction` and `quality` into the session record and the UI.
4. **Quality banner:** `state()['quality']` / `summary()['quality']` = `'insufficient_cop'` (sticky; opens at 1.0 s, fires at > 50 % invalid). The UI needs a persistent banner plus a policy for whether a flagged run's compliance is displayed or kept.
5. **duration_s semantics:** completion occurs on the next call at/after the deadline; a stalled stream overshoots the configured duration (L3). Show `state()['elapsed_s']`; if the UI needs strict wall windows it must close with `stop()` on its own timer.
6. **Persistence / stamping:** `training.py` exports no version constant and writes no artifact. Define the training-session JSON schema now and stamp it the way trials are stamped with `metrics.PROCESSING_VERSION`: a training-engine version (suggest `TRAINING_VERSION` + module hash), the resolved options, the rng seed (replay is a stated design property), `targets()` records, `summary()`, `quality`, and the COP sign-mapping version.
7. **Safety surfacing:** no spotter/fall-risk metadata exists at this layer (M4) — either add it to `TrainingMode`/`state()` or hard-wire the UI to render `mode.safety`; render "research, not therapy / not a medical device" and the stop-on-dizziness line wherever a session is created or reported.
8. **Scope discipline:** drive the UI off `training_mode_ids()`; `get_training_mode` raises for seated / closed-chain / dual-task / audio-cue families until they exist (module docstring 35-38, roadmap Phase 4). And remember the guard test is a literal blocklist with gaps (M5): future game ideas must be checked against the module's hard rule, not just the regex.

---

## 7. Bottom line

The engine is small, pure-stdlib, deterministic, and its three headline claims hold under adversarial probing: the 63.3 % manual example reproduces through an independent drive path (not just the author's test), the zone math and inclusive boundary are exact, and the pacing/state-machine guards reject every malformed call I could construct. The material gaps are all about **imperfect data or imperfect operator input**: silent credit bridging across stream stalls (M1), geometry options with no ceiling that can configure the quiet-stance envelope away (M2), a duplicate `target_hit()` / unguarded `set_target()` that can fabricate a 0.0 s transition on the default pacing (M3), a safety sentence that sanctions single-leg work the hard rule forbids (M4), and a blocklist-based safety test that misses inflections and whole word families (M5). None of these invalidate the module as a research aid, but M1/M2/M3 should be settled before the service/UI wave starts scoring real sessions.

**Overall verdict: MINOR ISSUES.**

*Read-only certification: this review created only this file. All probe scripts ran from a scratch directory outside the repo with `-B` / `PYTHONDONTWRITEBYTECODE=1`; no repo file, data file or config was modified; `training.py` and `tests/test_training.py` hashes were unchanged before and after the review.*
