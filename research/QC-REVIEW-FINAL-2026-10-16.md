# FINAL Independent QC Review — bertec-jump (2026-10-16 frozen build)

**Repo:** `C:\Users\GAMEPOWER\projects\bertec-jump`
**Reviewer:** final independent adversarial QC subagent (read-only; this report is the only file created)
**Window:** 2026-10-16, ~08:25–09:20 EDT. The live server on `:8765` was left untouched (verified still streaming the real Bertec plate before and after; every probe ran against private `create_app(data_dir=<tmp>, auto_connect=False)` + `TestClient` instances and a scratch dir outside the repo).
**Prior reviews re-checked:** `research/QC-REVIEW-2026-10-16.md` (protocol/service/reports/verify_cop/conductor) and `research/QC-REVIEW-training-2026-10-16.md` (training engine).

---

## 0. Revision under review (freeze certification)

Hashes/mtimes were captured at the start (08:25) and **re-checked after every probe run (09:20): identical**. The tree did not move during this review.

| File | SHA-256 (prefix) | Size / lines | mtime |
|---|---|---|---|
| service.py | `dde3bc444a6be164` | 101 041 B / 1933 | 08:19:39 |
| app.py | `d575b60b3dc57601` | 13 903 B / 318 | 08:10:24 |
| training.py | `6fe460616ec16d9d` | 56 957 B / 1152 | 08:19:44 |
| protocols.py | `600add02b1b202aa` | 52 328 B / 1087 | 08:10:57 |
| metrics.py | `6801ec26338d906f` | 615 lines | — |
| tools/verify_cop.py | `8078d1ecc95abbe4` | 46 771 B / 1030 | 07:55:27 |
| static/training.js | `551ecd7dda20a849` | 29 158 B / 590 | 08:15:23 |
| static/app.js | `9d1fe2c273ce3b9b` | — | — |
| static/index.html | `10f230fba716aef6` | — | — |

No git repository exists in the tree (**not** a git checkout) — hashes are the pin. `data/` was untouched (only the pre-existing 2026-09-24 session folder; no new files under `data/` after all probes).

**Suite (my own run): `PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python.exe -m unittest discover -s tests` → `Ran 581 tests in 29.811s` — `OK`, exit 0.** Per-file: analysis 18 · api 8 · bridge 11 · conductor 96 · frontend 15 · frontend_protocol 16 · frontend_training 18 · metrics 51 · protocol_api 16 · protocol_reports 18 · protocols 87 · service 22 · training 79 · training_api 21 · verify_cop 105 = **581**. Syntax check: all 1169 `.py` files in the tree parse (no SyntaxError).

---

## 1. Methodology (what was actually run)

All probes ran from a scratch directory **outside** the repo (`C:\Users\GAMEPOWER\AppData\Local\hermes\cache\scratch\qc-final`) with `-B` + `PYTHONDONTWRITEBYTECODE=1`, `.venv` Python 3.11.15, `sys.path` → repo. Probe scripts: `probe1_core.py`, `probe1b_core.py` (protocols.py invariants, detector namespacing, adapter gap flag, `_write_json_atomic` crash simulation), `probe2_training.py` / `probe2b_training.py` (engine M1–M5 + leftover L1/L2), `probe3a2_service.py` / `probe3b_training_service.py` / `probe3c_perf.py` / `probe3d_lock.py` / `probe3e_gap.py` (full API flows on private TestClient instances: H1, M1, M2/M9, training wiring, contention, decoy traversal, timings, lock contention), `probe4_verify.py` / `probe4b_verify.py` (verify_cop verdicts/exit codes/finite flags/same-second writes, conductor decoy), `probe5.js`+`probe5_front.py` (training.js state machine, option guards, escaping audit). No probe wrote inside the repo; data dirs were `TemporaryDirectory`s.

Adversarial choices: the original H1 scenario (5 s mid-trial device-clock hole), the original M1 scenario (zero/connect/disconnect while `running`), hostile rngs (`NaN`/`+inf`/out-of-pool `choice`), a crafted `session.json` pointing outside its folder, a 300-trial / 303-folder synthetic data dir for report/session-scan timings, and a feeder thread measuring ingest stalls while a report is built.

---

## 2. Re-verification table (each row = my own probe, not the repo's tests)

**No regressions. Every briefing item HOLDS.** Evidence per item:

| # | Finding (orig. review) | Result | Evidence (my probe) |
|---|---|---|---|
| H1 | Mid-trial hardware gap must end+flag the trial | **HOLDS** | 5 s device-clock hole mid-trial: status `running` → `trial_done`; pending record `flags=['stream_invalidated']` with the exact note; 50 further samples left the raw buffer at 1000→1000; `keep` → `trial_01.json` flags+note; session index carries the flag |
| H1b | Adapter gap flag `gapped_data` + count/seconds | **HOLDS** | two 0.5 s runs split by a 5 s hole → `quality_flags=['gapped_data']`, note “2 non-contiguous COP runs merged across 5.000 s of gap…”; clean single run → no flag |
| M1 | zero()/connect()/disconnect() refused while trial runs | **HOLDS** | all three 409 while `running` (explicit messages); at `trial_done` `zero`=200, `connect`=200; during a training session `zero`=409 (“A training session is active…”) |
| M2 | Atomic persistence for trial/session/in_progress/recording | **HOLDS** | `_write_json_atomic` (service.py:1015-1044) = serialize → tmp → flush → fsync → `os.replace`; nonserializable doc → TypeError, previous file intact, no `.tmp`; simulated `os.replace` crash → target intact, tmp cleaned; all 7 call sites audited (report.json 1445, in_progress 1429/1502/1696, trial JSON 1566, session.json 1651/1851); full session lifecycle left **no `*.tmp`** anywhere |
| M3 | `validate_catalog` spotter+finite invariants; `PROTOCOLS` immutable | **HOLDS** | spotter=False on single-leg / eyes-closed / foam → ValueError each; duration `inf`, `NaN`, `0`, `-3`, `True` → ValueError each; `PROTOCOLS` = MappingProxyType (item assign TypeError; no `update/clear/pop/setdefault`) |
| H2 | `dl_stance.ec_firm` spotter policy + roadmap citation | **HOLDS** | `ec_firm`: eyes closed, `requires_spotter=True`, notes quote the roadmap rail; module docstring cites the roadmap; `mctsib.c2_ec_firm=True` (parity); catalog-wide scan: **no** fall-risk condition without a spotter |
| M8 | Asymmetry annotations | **HOLDS** | practice 10 s unscored / scored 20 s; safety+notes contain “annotated 2026-10-16”, “cited 4×20 s”, “≥25 s research processing standard”, Nolff 2023 |
| M9 | requires_spotter/fall_risk in state()/trial record/session.json | **HOLDS** | `state()` keys include both (None when no condition); trial JSON and `session.json` kept entry + summary carry both |
| L1 | Free-form detector text namespaced | **HOLDS** | `diagnosis!`→`detector:diagnosis`, `step_suspected`→`detector:step_suspected` (not the built-in), `''`/`'!!!'`→`detector:unknown`, 80 chars → 40-char token; `step`/`pull` still whitelisted |
| T-M1 | Stall credit cap + `stream_gap` | **HOLDS** | 10.1 s hole credited as **0.5 s** (`time_in_zone_s=0.5`), sticky `stream_gap`; 60 fully gated samples → `valid_fraction=0.0`, `insufficient_cop` |
| T-M2 | Geometry ceilings | **HOLDS** | 5000 / 1e9 / 1e6 / 50.0001 / 1.000001 all ValueError with the bound in the message; exact ceiling accepted; live `validate_catalog()` rejects an over-ceiling spec, non-bool spotter, unknown fall_risk |
| T-M3 | Double `target_hit` ignored; `set_target` under on_demand refused | **HOLDS** | 3rd hit at the same clock → `duplicate_hit_ignored` +1, `targets_total` unchanged, `mean_transition_s=10.0` (no 0.0 fabrication); `set_target` under on_demand → RuntimeError; under `none` pacing allowed and the closed target stays `hit: False` |
| T-M4 | Per-mode spotter/fall-risk/safety | **HOLDS** | all 3 modes `requires_spotter=True`, risk low/moderate; both carried by `state()` and `summary()`; “not a medical device” in every safety string |
| T-M5 | Banned-regex catches stems | **HOLDS** | my adversarial list (walking/jumping/hopping/lunging/marching/running/jogging/sidestep/treadmill/travel/loading/lift/single-leg/tandem/gaits/impacts/…) matches; 103 catalog strings → 0 matches. Residual (by design): it stays a literal blocklist — `momentum`/`loss-of-contact` are not blocked, and both words appear **only inside negative cautions** in the module itself |
| V-M4 | verify_cop verdict floor (n≥10) | **HOLDS** | n=None/0/1/9 → sign `inconclusive`, scale `not_evaluated`, ratio None; n=10 → positive/pass; the old 686.5 N one-sample “PASS” is gone |
| V-M5 | Extreme-finite + malformed payloads | **HOLDS** | `[1e200,-1e200]` → mean 0.0 / sd None (no raise); `[1e308,1e308]` → mean None; non-dict entries ignored; `write_outputs` non-dict → TypeError before `mkdir` |
| V-M6 | Aux stats use the Fz window | **HOLDS** | 1 s window over a series whose last second is all mx=50 → mx mean **50.0** (unwindowed would be ~33) |
| V-L4 | Exit codes | **HOLDS** | good stream → exit 0 (positive/pass); negative sign → **exit 3**; thin stream → exit 0 with `inconclusive`; blocked → 2 |
| V-L5 | Same-second writes don't overwrite | **HOLDS** | three same-`ts` writes → `…Z.json`, `…Z_2.json`, `…Z_3.json`, all present |
| V-L6 | Non-finite CLI flags | **HOLDS** | `--seconds inf|nan|0`, `--weight-kg inf` → exit 2 with explicit “finite positive number” messages |
| C-M7 | Conductor decoy containment | **HOLDS** | reader rejects `../outside/secret.json`, absolute path, backslash form, `sub/../…` → `unresolved artifact`; generic reader marks outside files unresolved without stat-ing them; hostile manifest save/render → ValueError, file not written; clean render leaks no decoy |
| W-1 | Exactly one `add_sample` per hardware sample | **HOLDS** | 200 injected = engine `samples` 200 = CSV rows 200; 1.0 s `duration_s` fed 1600 stopped at 1001; gated sample row has empty COP fields, counted invalid (`valid_fraction` 0.8333 = 200/240) |
| W-2 | Mutual exclusion protocol↔training | **HOLDS** | 409 both directions; true 4-way concurrent `training_start` → exactly one 200 and three 409 |
| W-3 | `data/trainings` persistence + PROCESSING_VERSION | **HOLDS** | session.json with `processing_version='lp10-dc100-v1'` == `metrics.PROCESSING_VERSION`, safety (spotter True / 227 kg), targets, options, `zeroed_at_start`; marker removed; `stream_gap` case stored with `quality='stream_gap'` (API overlay and stored doc agree); shutdown → `quality='Application closed'`, samples 150, marker gone |
| W-4 | Plate zero/connect guards during training | **HOLDS** | zero/connect/disconnect all 409 while a training session is open |
| W-5 | Contention: stop during ingest / shutdown finalize | **HOLDS** | feeder 3000 samples + `stop` mid-stream → stop#1 200, stop#2 400, CSV rows == stored samples == 3000, concurrent state polls no errors/500s; startup race covered above |
| R-1 | Reports URL traversal | **HOLDS (URL surface)** | `../secret`, `aaaa`, truncated id, `%2e%2e%2f…`, backslash, `C:\windows` → 404 on both `/api/protocol/report/{id}` and `/report/{id}`; valid id → 200 JSON + 200 HTML with the strict CSP. (**But see NEW-2** — traversal through a stored `session.json` is open.) |
| H3 | End-to-end magnitude pin (from the first review) | **HOLDS** | `tests/test_protocol_api.py:47-50,272-299`: mean velocity/path length pinned ±2 % **and** cross-checked against the analytic 15.708 mm/s |

---

## 3. NEW findings (everything added since the prior reviews; severity-rated)

### NEW-1 — MEDIUM — Reports read endpoints hold the acquisition lock for the entire disk build, blocking the hardware worker past its own 500 ms staleness rule
**Where:** `service.py:1865-1866` (`protocol_sessions`), `1874-1876` (`protocol_report`), `1878-1881` (`protocol_progress`) — all wrap the whole disk scan/report assembly in `with self.lock:`. The lock is the same `RLock` the acquisition worker and `ingest()` use (`service.py:1055, 1108, 1301`).
**Probe evidence:** a 300-trial / 12-condition session folder: `build_protocol_report` = **911.8 ms cold** (freshly written files; Windows Defender scan included), **17–19 ms warm**; a 303-folder scan = **930.6 ms cold**. With a feeder thread calling `ingest()` at ~1 kHz, a report call held the lock for its full wall time — **max ingest stall 894.2 ms** (cold) and **207.7 ms** (freshly written); the worker thread's own poll/ingest path cannot run for that whole window.
**Why it matters:** 500 ms is the service's own staleness threshold (`service.py:1114-1115`). In any worker iteration where `bridge.poll()` returns no buffered samples, the stale check fires after the lock is released and calls `_invalidate('Data stream stale…')` — which since wave 4/5 **auto-ends a running protocol trial and stops+flags a training session** (H1 hooks). So opening a report (or the reports list) on a cold data set during a live capture can end/flag the very trial the report is about, and in the best case delays sample processing by up to ~0.9 s. There is no test anywhere for read-path lock hold; the suite's report tests run against tiny warm folders.
**Fix:** copy the folder/document list under the lock (fast) and do parsing/assembly outside it (stored artifacts are immutable once written), or give the readers their own lock; optionally cap report/open cost by reading `trial_XX.json` lazily.

### NEW-2 — MEDIUM — `session.json` trial references are followed without containment: a crafted stored session reads arbitrary local JSON and injects fabricated trials into the report (the M7 class, unfixed on this surface)
**Where:** `service.py:463-464` — `_load_kept_trials` builds `_read_json_document(folder / file_name)` straight from the stored session index; and `service.py:602-609` appends any unseen stored `condition_id` as a new report condition. No `..`/absolute/drive check and no resolved-containment check exists here (contrast `conductor/manifest.py:99-147` + `conductor/summary.py:119-146`, whose decoy tests pass — C-M7 above).
**Probe evidence:** decoy `data/DECOY.json` + a planted `data/protocols/20260101T010101_abcdef01/session.json` whose `kept_trials[0].file = '../../DECOY.json'`: `GET /api/protocol/report/{id}` → 200 and the report contained the decoy’s values (`n_samples=99999`, `duration_s=42.0`, `notes='DECOY-NOTE-MARKER'`, `metrics.mean_velocity_mm_s=123.456`, `processing_version='DECOY-VERSION'`), plus an extra fabricated condition `c1`; the printable page rendered the same marker and velocity. (`raw_file` strings are echoed but never read, so the exposure is the `file` reference.)
**Why it matters:** the same exchange-artifact rationale the prior review used for conductor manifests applies to stored session folders (they get copied/shared between machines); the failure is a poisoned report plus a read of any local JSON file the process can read.
**Fix:** mirror `artifact_path_violation` + resolved containment in `_load_kept_trials`; a violation should surface as an artifact note, and add the decoy regression test for this reader.

### NEW-3 — LOW — The auto-invalidation flag/reason is invisible until the trial is kept, and the invalidation reason is discarded
**Where:** `protocols.py:787-807` (`state()` has no flags/notes), `protocols.py:1044-1087` (`summary()` covers kept trials only), `service.py:1245-1247` (`_invalidate_protocol_trial` gets the flagged record back and returns it, but `_invalidate` at `1285-1297` ignores it and does not touch `connection['message']`).
**Probe evidence:** after the 5 s gap, `state()` keys = `[condition_id, condition_index, elapsed_s, fall_risk, requires_spotter, status, trial_index, trials_in_condition]` — no flags; `connection.message` still showed the pre-gap zero prompt; the dashboard (`static/protocol.js:75-85,13`) renders only “Trial finished · review” — no reason, and its summary line never shows flag counts. The flag first becomes visible **after** `keep` (API JSON `summary.flag_counts`, trial JSON, session.json, printable report — all verified). Training does the inverse and better: its `stream_gap` surfaces live via `summary.quality` and the UI banner.
**Fix:** surface the pending trial's flags/notes in `state()` (or return the record from the auto-end), and/or write the invalidation reason into `connection.message` so the operator sees why the trial ended before deciding keep/discard.

### NEW-4 — LOW — training.js submits hidden option fields (stale values) and hides their client-side validation
**Where:** `static/training.js:143-164` (`readNumbers`/`trainingOptions` read every `NUMBER_FIELDS` entry regardless of visibility), `419-420` (problems filtered by `visible[problem.name] === true`), `191-196` (`filterOptions` keeps any schema name).
**Probe (node):** pacing switched to `on_demand` with a stale `pacing_s='99'` in the now-hidden fixed-interval field → client problems `[]`, request payload would carry `pacing_s: 99` → the server returns 400 “option 'pacing_s' must be <= 15.0” for a control the user cannot see. No state is corrupted (the engine refuses the start), but the error message references an invisible field.
**Fix:** drop (or validate) option values whose row is not visible for the chosen pacing before sending.

### NEW-5 — LOW — `zone_radius_fraction = 1.0` (the accepted maximum) lets the zone reach the plate centre, contradicting the documented rationale
**Where:** `training.py:189, 402-407` — docstring/description say “at most 1.0, so a target zone can never swallow the plate centre”.
**Probe:** fraction 1.0, target at (0, 50) → `radius_mm = 50.0`; a centre sample (0,0) reports `in_zone=True` (distance 50 ≤ 50 + 1e-9, inclusive). Fix: cap the fraction strictly below 1.0 or correct the wording (e.g. “at most 1.0 **inclusive**: at exactly 1.0 the zone boundary passes through the plate centre”).

### NEW-6 — LOW — Training crash marker is written once; CSV tail is buffered
**Where:** `service.py:1744` (`_training_write_progress` called only at start; protocol refreshes on keep) and `1406-1407` (`flush` only every 1000 rows / at stop).
**Probe:** mid-session marker keeps `samples: 0`, `engine_status: 'running'` while the CSV grows; a hard crash therefore under-reports progress and can lose up to ~999 unflushed rows (same cadence as recordings — informational, but the protocol’s marker is refreshed while training’s is not).

### Known LOW leftovers from the prior reviews (unchanged, **not** regressions; listed for completeness)
- `training.py` hostile rng: `NaN` uniform still parks the session (probe: 10 s `duration_s` session ended with **1** target — a session without `duration_s` would never switch again); out-of-pool `rng.choice` still raises `KeyError: 'teleport'` out of `start()`; `+inf`/`−5` still clamp correctly.
- `training.py` L2: `target_hit()` before the first timestamp still presents the next target at `presented_t = 0.0` (a non-0-based stream would mint a huge transition; service feeds device times that are already large, and the first sample defines `t0`).
- `training.py` L4: `TRAINING_MODES` is a plain mutable dict and `validate_catalog()` accepts no candidate catalog (unlike `protocols.validate_catalog(catalog=None)`), so an alternative catalog cannot be pre-validated.
- `service.py` training `summary.flags` does not include the service-derived `stream_gap` (quality does); a consumer reading only `flags` would miss it (quality is the documented single field, so this is a consistency nit).

---

## 4. Top must-fix list

1. **NEW-1 — Take report assembly off the acquisition lock** (`service.py:1865-1881`). Measured 208–895 ms of ingest stall on a 300-trial report and ~0.9 s for a ~300-session scan — both exceed the service’s own 500 ms stale-invalidation threshold, which (post-H1-wave) can auto-end a live trial/training session. Read what you need under the lock; parse outside.
2. **NEW-2 — Contain `kept_trials[].file` reads to the session folder** (`service.py:463-464`, mirroring the conductor M7 fix). A crafted `session.json` currently reads arbitrary local JSON and injects fabricated trials/metrics into the stored report and printable page.
3. **NEW-3 — Show the operator *why* a trial auto-ended** (state()/UI and/or `connection.message`). The flag and note are recorded correctly, but the live dashboard reports only “Trial finished · review” and the reason string is discarded.
4. (Ride-along lows, cheap) NEW-4 hidden-field submission in `training.js`; NEW-5 docstring vs `zone_radius_fraction=1.0`; refresh the training in-progress marker during the session (NEW-6).

**No CRITICAL or HIGH findings.**

---

## 5. Verified sound (additional coverage evidence, all probe-backed)

- **Suite & tree:** 581/581 tests OK in my own run (29.8 s, exit 0); 1169/1169 `.py` files parse; hashes stable across the whole review; `data/` untouched; `:8765` still streaming the real plate, never restarted or written to.
- **Protocol engine safety rails:** every fall-risk condition (single-leg / eyes-closed / foam) requires a spotter, both in the shipped catalog and under injected bad catalogs; `PROTOCOLS` is read-only; duration invariants reject bool/inf/NaN/≤0; knee-angle/stance consistency enforced.
- **H1 end-to-end chain:** gap → auto-end → flag+note on the pending record → trial JSON → session index → session.json → report; the raw buffer stops at the pre-event samples; the metrics adapter additionally flags merged runs with the run count and total gap seconds; a clean run carries no flag.
- **Training engine:** 63.3 %/zone/hold semantics unchanged; credit cap and sticky flags; geometry ceilings enforced at option, session and catalog level; duplicate-hit debounce; on-demand `set_target`/`advance` refusal; defensive copies; wall-clock isolation; `not a medical device` in every mode safety string; deterministic seeded replay.
- **Training service:** exactly one engine sample per hardware sample; gated COP written as empty fields, counted invalid; auto-complete stops the CSV; stream-gap ends at the last valid sample and stores `quality='stream_gap'`; no post-gap rows; CSV rows == engine samples under concurrent stop; true concurrent double-start serializes to one 200/three 409; bad starts leave no stray folders (400s); shutdown finalizes with `quality='Application closed'`.
- **Reports/verify/conductor:** URL traversal 404s on both routes; HTML page escapes all stored values (`html.escape` on every interpolation — the decoy’s fields render as text, not markup, and page CSP is `default-src 'none'`); verify_cop verdict floor, finite handling, same-second suffixes, exit codes 0/2/3; conductor decoy containment incl. backslash and inside-landing `..` forms; conductor `save` atomic.
- **Frontend:** no `innerHTML`/`insertAdjacentHTML`/`document.write`/`eval` in any `static/*.js` — all dynamic strings go through `textContent`/canvas `fillText`; `controls()` mirrors the API guards exactly (on_demand→“Mark reached”, fixed/none→“Next target”, single-target and complete sessions→stop only); quality banners render both flags plus a conservative fallback; the client option mirror enforces the server’s ceilings.

---

## 6. Bottom line

Every previously-fixed finding from both prior reviews re-verifies **HOLDS** with fresh probes — no regressions. The wave-4/5 work is materially complete: the silent mid-trial gap is gone end-to-end, plate commands are guarded during trials *and* training, persistence is atomic, the spotter catalog is self-enforcing, the detector namespace is closed, and the verify_cop/conductor tools now fail honestly at every boundary I could construct. The two material new issues are both on the **reports read path** (a lock-held disk build that can stall the acquisition worker past its own staleness rule, and uncontained artifact references in a crafted `session.json`), plus a visibility gap in how an auto-invalidated trial is explained to the operator. None of them affect the correctness of the live sample pipeline or the math, and none is CRITICAL or HIGH.

**Overall verdict: MINOR ISSUES** (fix NEW-1 and NEW-2 before relying on the reports UI during live capture or on shared/copied session folders).

*Read-only certification: the only file created in the repo is this report. All probe scripts ran from a scratch directory outside the repo with `-B`/`PYTHONDONTWRITEBYTECODE=1` against private `auto_connect=False` app instances; the `:8765` service was only read via one `/api/status` GET per check; no repo source, data or config file was modified (hashes re-verified identical before/after).*
