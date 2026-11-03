"""Guided static-plate test-battery definitions and a deterministic session runner.

RESEARCH / COMPLIANCE AID - NOT A MEDICAL DEVICE.
This module defines the guided protocol catalog for the Bertec BP5046 static
balance plate and a dependency-free session runner that walks an operator
through a chosen battery: it tracks the current condition/trial, rolls raw
samples into the active trial, and mirrors the clinical keep / discard /
repeat-trial flow of the original software. It makes no diagnostic or
clearance claims; clinical interpretation and return-to-play decisions are
physician-led (research/USE-CASES-REPORT.md sections E/G).

Sources (not paraphrased away):
- Bertec Balance Advantage Static System User Manual 80P-0009: protocol
  shapes, mCTSIB condition order and interpretation matrix (C1/C2
  somatosensory, C3 visual, C4 vestibular, C3+C4 combined), Weight Bearing
  Squat at 0/30/60/90 deg knee flexion, and the STEP/PULL weight-loss
  auto-detection flow whose flag is always reviewed by the operator and never
  auto-discards a trial.
- research/USE-CASES-REPORT.md (repo, verified research 2026-10-15):
  durations (DL 30 s x 3, SL 30 s x 3 with a 20 s squad-screening option,
  mCTSIB 10 s x 3, Asymmetry screen practice L/R then scored L/R, ASI 16 %
  flag), foam pad spec (~5-6 cm medium density), the sequential single-plate
  method for L/R asymmetry, and the explicit scope exclusions.
- research/IMPLEMENTATION-ROADMAP.md Phase 2: runner parity (per-trial timer,
  operator keep / discard / repeat control, notes field) and its per-protocol
  safety rail 'spotter text for EC/foam/single-leg; vestibular
  caution/dizziness stop; trial abort' (Phase 2 'Safety rails per protocol',
  roadmap line 42). That rail - not the use-case report, which documents no
  spotter policy - is the origin of the eyes-closed / foam / single-leg
  spotter rule enforced by validate_catalog() below and applied to every such
  condition in this catalog.

Out of scope by equipment design (stated here, not silently dropped): every
gait / walkway protocol (Walk Across, Tandem Walk, Step Quick Turn, Step
Up-Over, Forward Lunge) requires the 20 x 60 in Functional walkway, and no
jump or impact protocol of any kind belongs on this 500 lb patient-rated
plate, which is not impact-rated - jumps stay locked behind the existing
safety gate (USE-CASES-REPORT sections F/G).

Timing model: progress is derived from the sample timestamps handed to
add_sample (state()['elapsed_s'] = last sample time - first sample time inside
the current trial), never from wall clock, so runs are deterministic and
replayable. Timestamps must strictly increase within one trial; each trial
restarts its own local clock. A running trial ends itself once that timestamp
span reaches the condition's duration_s + AUTO_END_GRACE_S (see add_sample),
which bounds the sample buffer at a little over the target instead of letting a
forgotten trial grow without limit; the auto-ended trial is flagged 'overrun'
and still waits for the operator's keep / discard decision. This module never
discovers or approves hardware, imports nothing beyond the standard library,
and stays independent of analysis/metrics modules - a caller injects its own
compute_metrics.

Safety awareness: every protocol carries a fall_risk level and a safety
string, and every condition carries requires_spotter. Eyes-closed, foam and
single-limb conditions are the fall-risk carriers and MUST set
requires_spotter=True - validate_catalog() raises otherwise, so a catalog edit
can never silently ship an unspotted fall-risk condition. state() and every
trial record repeat the condition's requires_spotter flag and the protocol's
fall_risk so no live consumer loses the safety requirement; the runner refuses
nonsensical input but the operator (with a spotter where required) owns the
clinical safety of the session.
"""
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import math
import re
import types
from typing import Dict, List, Mapping, Optional, Tuple

__all__ = [
    'Condition', 'Protocol', 'PROTOCOLS',
    'get_protocol', 'protocol_ids', 'validate_catalog',
    'default_compute_metrics', 'default_weight_loss_detector',
    'detector_result_to_flag', 'SessionRunner',
    'DETECTOR_FLAGS', 'STATUSES', 'FALL_RISK_LEVELS',
    'EYES_STATES', 'SURFACES', 'STANCES',
    'DEFAULT_STEP_BASELINE_S', 'DEFAULT_STEP_DROP_RATIO',
    'DEFAULT_STEP_MIN_DURATION_S',
    'SHORT_TRIAL_TOLERANCE_S', 'SHORT_TRIAL_MIN_TARGET_S',
    'AUTO_END_GRACE_S', 'OVERRUN_FLAG',
    'QUALITY_FLAGS_KEY', 'QUALITY_NOTE_KEY',
]

SOURCE_MANUAL = 'Bertec Balance Advantage Static System User Manual 80P-0009'
SOURCE_REPORT = 'research/USE-CASES-REPORT.md (repo, verified 2026-10-15)'
SOURCE_ROADMAP = 'research/IMPLEMENTATION-ROADMAP.md (Phase 2)'

STATUSES = ('idle', 'ready', 'running', 'trial_done', 'complete')
FALL_RISK_LEVELS = ('low', 'moderate', 'high')
EYES_STATES = ('open', 'closed')
SURFACES = ('firm', 'foam')
STANCES = ('double', 'single_left', 'single_right', 'neutral_squat')

# Detector result -> trial flag. The manual's automatic weight-loss detection
# distinguishes a step (foot moved / stepped off the plate) from a pull
# (support grabbed), and both are operator-reviewed, never auto-discarding.
# This dict is the ONLY whitelist: 'step'/'pull' map to the built-in flags,
# and detector_result_to_flag namespaces every other (custom) detector result
# as 'detector:<token>' (L1), so free-form detector text can never be minted
# into trial records, summaries or reports as a built-in flag.
DETECTOR_FLAGS = {'step': 'step_suspected', 'pull': 'pull_suspected'}

# Default weight-loss detector thresholds (documented defaults, all
# configurable through SessionRunner keyword arguments).
DEFAULT_STEP_BASELINE_S = 1.5      # baseline window = first 1.5 s of the trial
DEFAULT_STEP_DROP_RATIO = 0.5      # weight loss = |fz| below 50 % of baseline
DEFAULT_STEP_MIN_DURATION_S = 0.5  # must persist continuously for >= 0.5 s

# L2: a finished trial whose sample-timestamp duration is more than this many
# seconds short of its condition's target duration is flagged 'short_trial'.
# One second absorbs the sample-interval/rounding difference between the
# target and the recorded span (a full 30 s feed at 100 Hz records 29.99 s) so
# an on-target trial is never flagged, while a deliberately rounded-off
# attempt (e.g. 3 s inside a 30 s condition) always is.
SHORT_TRIAL_TOLERANCE_S = 1.0

# Only timed balance conditions (target >= this many seconds) are checked for
# the short_trial flag. The sub-5 s conditions in this catalog are the
# weight-bearing-squat hold-to-capture blocks, whose own notes describe them as
# a short ~2-3 s hold: a shorter hold there is captured as-is by the operator
# (and reported with its real duration), not treated as a protocol deviation.
SHORT_TRIAL_MIN_TARGET_S = 5.0

# F1: a running trial ends itself once its sample-timestamp duration reaches
# the condition's target duration_s plus this grace window. Before this rule a
# trial only ended on the operator's End control or a stream invalidation, so
# a forgotten trial buffered forever (~1 GB/h at 1 kHz). The grace absorbs
# sample-arrival latency and the rounding sample (a full 30 s feed at 100 Hz
# records 29.99 s), so an on-target trial is never cut short at target-1
# sample; the buffer is bounded at target + grace instead of growing without
# limit. Configurable per runner (auto_end_grace_s); 0 ends exactly at target.
AUTO_END_GRACE_S = 1.0

# Quality flag set on a trial record whose end was forced by the duration rule
# (not the operator's End control and not a stream invalidation). The trial is
# still 'pending': the operator reviews the flag and keeps or discards it.
OVERRUN_FLAG = 'overrun'

# Optional keys a compute_metrics callable may use to report data-quality
# problems it had to work around (the service metrics adapter marks a trial
# whose metrics skipped or merged non-contiguous COP runs with
# QUALITY_FLAGS_KEY=['gapped_data'] plus a QUALITY_NOTE_KEY text carrying the
# dropped-run count and gap seconds). SessionRunner.end_trial merges both into
# the trial's flags/notes, so a compromised trial can never report as clean.
QUALITY_FLAGS_KEY = 'quality_flags'
QUALITY_NOTE_KEY = 'quality_note'


@dataclass(frozen=True)
class Condition:
    """One condition (a stance x eyes x surface block) of a protocol."""

    id: str
    label: str
    duration_s: float
    trials: int
    eyes: str                 # EYES_STATES
    surface: str              # SURFACES
    stance: str               # STANCES
    requires_spotter: bool
    notes: str = ''
    knee_flexion_deg: Optional[float] = None   # neutral_squat conditions only
    scored: bool = True                        # False = practice / unscored


@dataclass(frozen=True)
class Protocol:
    """A named, ordered battery of conditions for the static plate."""

    id: str
    label: str
    conditions: Tuple[Condition, ...]
    fall_risk: str            # FALL_RISK_LEVELS
    safety: str
    sources: str
    notes: str = ''

    @property
    def requires_spotter(self) -> bool:
        """True when any condition of this protocol requires a spotter."""
        return any(c.requires_spotter for c in self.conditions)

    @property
    def total_trials(self) -> int:
        """Total number of trials the full protocol asks the operator to run."""
        return sum(c.trials for c in self.conditions)


_DL_STANCE = Protocol(
    id='dl_stance',
    label='Double-Leg Quiet Stance (Eyes Open / Eyes Closed)',
    conditions=(
        Condition(
            id='eo_firm',
            label='Double-leg stance, eyes open, firm surface',
            duration_s=30.0, trials=3, eyes='open', surface='firm',
            stance='double', requires_spotter=False,
            notes='Reliability workhorse of the battery; subject stands where '
                  'the feet are marked on the plate, gaze on a fixed point.',
        ),
        Condition(
            id='ec_firm',
            label='Double-leg stance, eyes closed, firm surface',
            duration_s=30.0, trials=3, eyes='closed', surface='firm',
            stance='double', requires_spotter=True,
            notes='Sway is expected to grow versus eyes open (+34-40 % path '
                  'length in the cited sport-student norms). Eyes closed is a '
                  'spotter condition (the roadmap rail "spotter text for '
                  'EC/foam/single-leg", ' + SOURCE_ROADMAP + '; the same '
                  'eyes-closed policy as mCTSIB C2): the spotter stands '
                  'within arm\'s reach and the operator stops the trial on '
                  'any stumbling or reaching for support.',
        ),
    ),
    fall_risk='low',
    safety='Clear the floor around the plate; a spotter is required for the '
           'eyes-closed condition (operator within arm\'s reach for eyes '
           'open); terminate any eyes-closed trial immediately if the subject '
           'opens their eyes to recover or reaches for support.',
    sources='{0}: static double-leg quiet stance. {1} section 3A.'.format(
        SOURCE_MANUAL, SOURCE_REPORT),
    notes='The literature recommends 3-5 x 30-60 s; this catalog fixes 30 s '
          'x 3 as the lab default (battery kept within a ~25-30 min session).',
)

_SL_STANCE = Protocol(
    id='sl_stance',
    label='Single-Leg Stance (Eyes Open / Eyes Closed, per side)',
    conditions=(
        Condition(
            id='eo_left',
            label='Single-leg stance, eyes open, left leg',
            duration_s=30.0, trials=3, eyes='open', surface='firm',
            stance='single_left', requires_spotter=True,
            notes='Subject may choose transfer leg; record which foot was '
                  'lifted in the trial notes.',
        ),
        Condition(
            id='ec_left',
            label='Single-leg stance, eyes closed, left leg',
            duration_s=30.0, trials=3, eyes='closed', surface='firm',
            stance='single_left', requires_spotter=True,
            notes='Highest fall-risk condition family; spotter stands at the '
                  'transfer side and counters any loss of balance.',
        ),
        Condition(
            id='eo_right',
            label='Single-leg stance, eyes open, right leg',
            duration_s=30.0, trials=3, eyes='open', surface='firm',
            stance='single_right', requires_spotter=True,
            notes='Subject stands on the right leg (left foot lifted); record '
                  'which foot was lifted in the trial notes. Spotter stands '
                  'at the transfer side within arm\'s reach - the same rule '
                  'as the left-leg conditions.',
        ),
        Condition(
            id='ec_right',
            label='Single-leg stance, eyes closed, right leg',
            duration_s=30.0, trials=3, eyes='closed', surface='firm',
            stance='single_right', requires_spotter=True,
            notes='Highest fall-risk condition family; spotter stands at the '
                  'transfer side and counters any loss of balance - the same '
                  'rule as the left-leg eyes-closed condition.',
        ),
    ),
    fall_risk='high',
    safety='A spotter is required for every single-leg condition - including '
           'eyes open. Hand rail or chair within reach; terminate the trial '
           'on any touch-down of the lifted foot or uncontrolled sway.',
    sources='{0}: unilateral stance assessment. {1} section 3A (3 x 30 s per '
            'side and eyes state).'.format(SOURCE_MANUAL, SOURCE_REPORT),
    notes='A screening mode of 20 s (one trial per side) is available for '
          'squad screening per the report; the spotter requirement and all '
          'safety rules still apply. Familiarization precedes scoring.',
)

_MCTSIB = Protocol(
    id='mctsib',
    label='Modified Clinical Test of Sensory Interaction on Balance',
    conditions=(
        Condition(
            id='c1_eo_firm',
            label='Condition 1 - eyes open, firm surface',
            duration_s=10.0, trials=3, eyes='open', surface='firm',
            stance='double', requires_spotter=False,
            notes='C1 = somatosensory reference condition. Abnormal C1 and/or '
                  'C2 point to a somatosensory contribution.',
        ),
        Condition(
            id='c2_ec_firm',
            label='Condition 2 - eyes closed, firm surface',
            duration_s=10.0, trials=3, eyes='closed', surface='firm',
            stance='double', requires_spotter=True,
            notes='C2 = somatosensory condition (vision removed). Abnormal '
                  'C1 and/or C2 point to a somatosensory contribution.',
        ),
        Condition(
            id='c3_eo_foam',
            label='Condition 3 - eyes open, foam surface',
            duration_s=10.0, trials=3, eyes='open', surface='foam',
            stance='double', requires_spotter=True,
            notes='C3 = visual condition. Foam pad (~5-6 cm medium density) '
                  'is a separate pad placed by the operator; mark pad lines '
                  'to match the plate lines. Abnormal C3 only suggests '
                  'visual dependence.',
        ),
        Condition(
            id='c4_ec_foam',
            label='Condition 4 - eyes closed, foam surface',
            duration_s=10.0, trials=3, eyes='closed', surface='foam',
            stance='double', requires_spotter=True,
            notes='C4 = vestibular probe. Abnormal C4 points to vestibular '
                  'processing; abnormal C3 + C4 suggests a combined pattern.',
        ),
    ),
    fall_risk='moderate',
    safety='Spotter required for every eyes-closed and foam condition (C2, '
           'C3, C4); foam pad edge taped down or checked for slippage before '
           'each trial; stop on dizziness, nausea or visual discomfort.',
    sources='{0}: mCTSIB conditions, order and interpretation matrix '
            '(C1/C2 somatosensory, C3 visual, C4 vestibular). {1} section 3C '
            '(4 conditions x 3 x 10 s; foam-EC is the vestibular probe).'
            .format(SOURCE_MANUAL, SOURCE_REPORT),
    notes='Interpretation matrix (manual): C1 and/or C2 abnormal -> '
          'somatosensory; C3 only abnormal -> visual dependence; C4 only '
          'abnormal -> vestibular processing; C3 + C4 abnormal -> combined. '
          'The vestibular condition is the noisiest (MDC90 approx 1.12 deg/s) '
          '- interpret change scores carefully. Static-plate substitutes only: '
          'SOT / MCT / ADT need a moving platform and visual surround and are '
          'not possible here.',
)

_WBS = Protocol(
    id='wbs',
    label='Weight Bearing Squat (0 / 30 / 60 / 90 deg knee flexion)',
    conditions=(
        Condition(
            id='neutral_0',
            label='Weight bearing squat - neutral (0 deg knee flexion)',
            duration_s=3.0, trials=1, eyes='open', surface='firm',
            stance='neutral_squat', requires_spotter=False,
            knee_flexion_deg=0.0,
            notes='Operator confirms the position (goniometer / visual); '
                  'capture is a short ~2-3 s hold, once.',
        ),
        Condition(
            id='flexion_30',
            label='Weight bearing squat - 30 deg knee flexion',
            duration_s=3.0, trials=1, eyes='open', surface='firm',
            stance='neutral_squat', requires_spotter=False,
            knee_flexion_deg=30.0,
            notes='Operator confirms the position; short ~2-3 s hold, once.',
        ),
        Condition(
            id='flexion_60',
            label='Weight bearing squat - 60 deg knee flexion',
            duration_s=3.0, trials=1, eyes='open', surface='firm',
            stance='neutral_squat', requires_spotter=False,
            knee_flexion_deg=60.0,
            notes='Operator confirms the position; short ~2-3 s hold, once.',
        ),
        Condition(
            id='flexion_90',
            label='Weight bearing squat - 90 deg knee flexion',
            duration_s=3.0, trials=1, eyes='open', surface='firm',
            stance='neutral_squat', requires_spotter=False,
            knee_flexion_deg=90.0,
            notes='Operator confirms the position; short ~2-3 s hold, once. '
                  'Stop short of pain or loss of balance.',
        ),
    ),
    fall_risk='low',
    safety='Operator stays nearby (a dedicated spotter is not required); '
           'support surface within reach; body weight only - no added load; '
           'stop on knee, hip or back pain.',
    sources='{0}: Weight Bearing Squat method and 0/30/60/90 deg knee '
            'flexion levels. {1} section 3B (asymmetries often only appear '
            'at depth).'.format(SOURCE_MANUAL, SOURCE_REPORT),
    notes='Load symmetry is the clinical point. A true left/right load split '
          'needs per-foot channels; on a single-zone plate use the sequential '
          'single-leg method (asymmetry_screen) for sided asymmetry and treat '
          'WBS as a load-tolerance / comfort companion.',
)

_ASYMMETRY_SCREEN = Protocol(
    id='asymmetry_screen',
    label='Sequential Single-Leg Asymmetry Screen',
    conditions=(
        Condition(
            id='practice_left',
            label='Practice - left leg (unscored)',
            duration_s=10.0, trials=1, eyes='open', surface='firm',
            stance='single_left', requires_spotter=True, scored=False,
            notes='Unscored familiarization trial. A single plate cannot '
                  'split left/right load during double stance, so asymmetry '
                  'is measured sequentially (single-leg per side) - the '
                  'validated single-plate method, not a workaround. '
                  'Presented at 10 s; see the protocol notes and sources for '
                  'the annotated deviation from the cited 4×20 s design.',
        ),
        Condition(
            id='practice_right',
            label='Practice - right leg (unscored)',
            duration_s=10.0, trials=1, eyes='open', surface='firm',
            stance='single_right', requires_spotter=True, scored=False,
            notes='Unscored familiarization trial; keeps the learning effect '
                  'out of the scored trials. Presented at 10 s (same '
                  'annotated shortening as practice_left); spotter rule as '
                  'for every single-leg condition.',
        ),
        Condition(
            id='scored_left',
            label='Scored - left leg',
            duration_s=20.0, trials=1, eyes='open', surface='firm',
            stance='single_left', requires_spotter=True,
            notes='Scored trial. ASI = |L-R| / ((L+R)/2) x 100 on total path '
                  'length; the published flag threshold is >= 16 % (10 % is '
                  'the conventional rule of thumb).',
        ),
        Condition(
            id='scored_right',
            label='Scored - right leg',
            duration_s=20.0, trials=1, eyes='open', surface='firm',
            stance='single_right', requires_spotter=True,
            notes='Scored trial; ASI rule as per scored_left.',
        ),
    ),
    fall_risk='high',
    safety='A spotter is required for every condition (all single-limb '
           'stance). Hand rail or chair within reach; the practice trials are '
           'also the operator\'s balance check before scoring begins.',
    sources='{0}: sequential single-leg screening method. {1} section 3B '
            '(practice L, practice R, then scored L, scored R; ASI >= 16 % '
            'flag). Duration deviation annotated 2026-10-16: practice trials '
            'ship at 10 s rather than the cited 4×20 s (screening use); the '
            '16 % flag threshold is applied by the metrics module '
            '(metrics.ASI_FLAG_PCT) and the 20 s scored window is below the '
            '>=25 s research processing standard, so ASI here is a screening '
            'aid, not a research-grade sway comparison.'
            .format(SOURCE_MANUAL, SOURCE_REPORT),
    notes='Sequence: practice left (10 s, unscored), practice right (10 s, '
          'unscored), scored left (20 s), scored right (20 s). The eyes '
          'condition is not fixed by our sources: this catalog defaults to '
          'eyes open, and any eyes-closed variant (used in ACL monitoring '
          'literature) must be annotated via trial_notes and held constant '
          'for every visit before comparing ASI values. Duration deviation '
          '(annotated 2026-10-16): practice trials are presented at 10 s '
          '(shortened from the cited 4×20 s protocol for screening use); '
          'scored trials follow the cited 4×20 s design (Nolff 2023). Note: '
          'scored 20 s < the ≥25 s research processing standard — ASI here '
          'is a screening aid, not a research-grade sway comparison.',
)


_PROTOCOLS: Dict[str, Protocol] = {
    proto.id: proto for proto in
    (_DL_STANCE, _SL_STANCE, _MCTSIB, _WBS, _ASYMMETRY_SCREEN)
}

# The catalog is public read-only data: MappingProxyType raises TypeError on
# item assignment (and exposes no update/clear/pop), so an unvalidated entry
# can never be injected into the live catalog at runtime while iteration,
# len(), 'in' and lookup keep working exactly as before for every caller.
PROTOCOLS: Mapping[str, Protocol] = types.MappingProxyType(_PROTOCOLS)


def protocol_ids() -> Tuple[str, ...]:
    """Catalog order (stable): -> ('dl_stance', 'sl_stance', 'mctsib', ...)."""
    return tuple(PROTOCOLS)


def get_protocol(protocol_id: str) -> Protocol:
    """Look up a protocol by id; ValueError on an unknown id."""
    try:
        return PROTOCOLS[protocol_id]
    except (KeyError, TypeError):
        raise ValueError('unknown protocol id: %r' % (protocol_id,)) from None


def validate_catalog(catalog: Optional[Mapping[str, Protocol]] = None) -> None:
    """Raise ValueError if a catalog violates its own invariants.

    Validates the shipped PROTOCOLS by default; pass another mapping (tests
    do) to check a candidate catalog before it ships. Invariants:
    - key == protocol.id and non-empty; at least one condition; fall_risk in
      FALL_RISK_LEVELS; non-empty safety text; unique non-empty condition ids;
    - duration_s finite and > 0 (inf/NaN are rejected, not just <= 0);
    - trials >= 1, eyes/surface/stance known, requires_spotter is a bool;
    - knee_flexion_deg present exactly on neutral_squat conditions, never
      negative;
    - safety invariant: any condition with a single-leg stance, closed eyes or
      a foam surface MUST set requires_spotter=True (the roadmap Phase 2 rail
      'spotter text for EC/foam/single-leg'), so a future catalog edit can
      never silently ship an unspotted fall-risk condition.
    """
    source = PROTOCOLS if catalog is None else catalog
    if not source:
        raise ValueError('protocol catalog is empty')
    for key, proto in source.items():
        if key != proto.id or not proto.id:
            raise ValueError('catalog key/id mismatch: %r' % (key,))
        if not proto.conditions:
            raise ValueError('protocol %r has no conditions' % (key,))
        if proto.fall_risk not in FALL_RISK_LEVELS:
            raise ValueError('protocol %r has invalid fall_risk' % (key,))
        if not proto.safety.strip():
            raise ValueError('protocol %r has no safety text' % (key,))
        seen = set()
        for cond in proto.conditions:
            if not cond.id or cond.id in seen:
                raise ValueError('protocol %r has a duplicate/empty condition id'
                                 % (key,))
            seen.add(cond.id)
            duration = cond.duration_s
            if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                    or not math.isfinite(float(duration)) or float(duration) <= 0):
                raise ValueError('condition %r duration must be finite and > 0'
                                 % (cond.id,))
            if cond.trials < 1:
                raise ValueError('condition %r has no trials' % (cond.id,))
            if cond.eyes not in EYES_STATES:
                raise ValueError('condition %r has invalid eyes' % (cond.id,))
            if cond.surface not in SURFACES:
                raise ValueError('condition %r has invalid surface' % (cond.id,))
            if cond.stance not in STANCES:
                raise ValueError('condition %r has invalid stance' % (cond.id,))
            if not isinstance(cond.requires_spotter, bool):
                raise ValueError('condition %r spotter flag is not bool'
                                 % (cond.id,))
            fall_risk_condition = (cond.stance.startswith('single')
                                   or cond.eyes == 'closed'
                                   or cond.surface == 'foam')
            if fall_risk_condition and cond.requires_spotter is not True:
                raise ValueError(
                    'condition %r is a fall-risk condition (single-leg / eyes '
                    'closed / foam) and must set requires_spotter=True'
                    % (cond.id,))
            squat = cond.stance == 'neutral_squat'
            if squat != (cond.knee_flexion_deg is not None):
                raise ValueError('condition %r knee angle / stance mismatch'
                                 % (cond.id,))
            if cond.knee_flexion_deg is not None and cond.knee_flexion_deg < 0:
                raise ValueError('condition %r has negative knee angle'
                                 % (cond.id,))
    return None


validate_catalog()   # fail fast on a malformed catalog at import time


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_finite_number(value) -> bool:
    return _is_number(value) and math.isfinite(float(value))


def _mean(values: List[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _range(values: List[float]) -> Optional[float]:
    return max(values) - min(values) if values else None


def _append_flag(flags: List[str], value) -> None:
    """Append one flag name to flags (string-normalised, unique, blank ignored)."""
    if value is None:
        return
    text = str(value).strip()
    if text and text not in flags:
        flags.append(text)


def _join_text(first: str, second: str) -> str:
    """Two note fragments joined by one space, either side possibly empty."""
    first, second = first.strip(), second.strip()
    if not first:
        return second
    if not second:
        return first
    return '%s %s' % (first, second)


def default_compute_metrics(t_list, fz_list, copx_mm_list, copy_mm_list) -> dict:
    """Default trial descriptor set: per-channel mean and range (None-safe).

    Deliberately tiny so this module stands alone: the full metric engine
    (path length, mean velocity, 95 % ellipse, AP/ML range, ASI, ...) lives in
    its own module and is injected via SessionRunner(compute_metrics=...). None
    entries (missing / unfilled channels) and non-finite values are skipped;
    a channel with no usable values reports None rather than 0.
    """
    fz = [float(v) for v in fz_list if _is_finite_number(v)]
    copx = [float(v) for v in copx_mm_list if _is_finite_number(v)]
    copy = [float(v) for v in copy_mm_list if _is_finite_number(v)]
    return {
        'mean_fz_n': _mean(fz),
        'mean_copx_mm': _mean(copx),
        'mean_copy_mm': _mean(copy),
        'range_fz_n': _range(fz),
        'range_copx_mm': _range(copx),
        'range_copy_mm': _range(copy),
    }


# L1: a custom detector's return value is surfaced as a NAMESPACED flag,
# 'detector:<token>', so free-form detector text can never masquerade as a
# built-in flag (before this, a detector returning 'diagnosis' emitted
# 'diagnosis_suspected' into trial records, summaries and reports). The token
# is the lowercased result reduced to [a-z0-9_] (runs of anything else become
# a single '_', leading/trailing '_' are trimmed, capped at 40 characters); a
# result that reduces to nothing ('' / '!' / whitespace) becomes
# 'detector:unknown'. Case-fold + sanitize runs BEFORE the cap so the token
# is stable and encoding-safe (ASCII only).
_DETECTOR_FLAG_PREFIX = 'detector:'
_DETECTOR_TOKEN_UNKNOWN = 'unknown'
_DETECTOR_TOKEN_MAX_LEN = 40
_DETECTOR_TOKEN_SEPARATOR = re.compile(r'[^a-z0-9_]+')


def _detector_flag_token(text: str) -> str:
    """Reduce detector result text to a lowercase [a-z0-9_] token (L1)."""
    token = _DETECTOR_TOKEN_SEPARATOR.sub('_', text.lower()).strip('_')
    token = token[:_DETECTOR_TOKEN_MAX_LEN].rstrip('_')
    return token or _DETECTOR_TOKEN_UNKNOWN


def detector_result_to_flag(result) -> Optional[str]:
    """Map a weight_loss_detector return value to a trial flag or None.

    None -> no flag; 'step' -> 'step_suspected'; 'pull' -> 'pull_suspected'
    (the DETECTOR_FLAGS whitelist, exact match on the stripped text). Every
    other value is NAMESPACED (L1): 'detector:<token>' where <token> is the
    result lowercased and reduced to [a-z0-9_] - runs of other characters
    become '_', outer '_' are trimmed, the token is capped at 40 characters -
    and a result that reduces to nothing ('' / '!' / whitespace) becomes
    'detector:unknown'. So a custom detector returning 'diagnosis!' surfaces
    as 'detector:diagnosis' and one returning a ready-looking name like
    'step_suspected' surfaces as 'detector:step_suspected', NOT as the
    built-in flag: only the whitelist can mint a built-in flag name. Flags
    (built-in or namespaced) are operator-reviewed and never auto-discard.
    """
    if result is None:
        return None
    text = str(result).strip()
    if text in DETECTOR_FLAGS:
        return DETECTOR_FLAGS[text]
    return _DETECTOR_FLAG_PREFIX + _detector_flag_token(text)


def _low_run_span(samples, first: int, last: int) -> float:
    """Covered time span of the low-force run samples[first..last] inclusive.

    A sample stands for the interval that ended at its timestamp, so the span
    extends one sample interval beyond the last low sample (or back to the
    sample before the first low one when available).
    """
    span = samples[last][0] - samples[first][0]
    if first > 0:
        span += samples[first][0] - samples[first - 1][0]
    elif last + 1 < len(samples):
        span += samples[last + 1][0] - samples[last][0]
    return span


def default_weight_loss_detector(samples, baseline_s=DEFAULT_STEP_BASELINE_S,
                                 drop_ratio=DEFAULT_STEP_DROP_RATIO,
                                 min_duration_s=DEFAULT_STEP_MIN_DURATION_S):
    """Detect a sustained loss of body weight -> 'step' (or None).

    samples: sequence of (t, fz_n, copx_mm, copy_mm) tuples for one trial.
    Baseline = mean |fz| over the first baseline_s seconds of the trial; body
    weight loss = |fz| below drop_ratio * baseline persisting continuously for
    at least min_duration_s. This mirrors the manual's automatic STEP/PULL
    flow: the caller turns 'step' into the 'step_suspected' trial flag, and the
    flag NEVER auto-discards - the operator decides keep or discard. Duration
    comparisons carry a 1e-9 tolerance (float safety). Returns None when no
    judgement is possible (empty trial, no baseline window, unloaded plate).
    'pull' is reserved for a custom detector (e.g. harness/support pulls).
    """
    if not samples:
        return None
    t0 = samples[0][0]
    baseline_values = [abs(sample[1]) for sample in samples
                       if sample[0] - t0 <= baseline_s + 1e-9
                       and _is_finite_number(sample[1])]
    if not baseline_values:
        return None
    baseline = sum(baseline_values) / len(baseline_values)
    if baseline <= 0.0:
        return None
    threshold = baseline * drop_ratio
    run_start = None
    for index, (t, fz, *_rest) in enumerate(samples):
        low = _is_finite_number(fz) and abs(fz) < threshold
        if low and run_start is None:
            run_start = index
        elif not low and run_start is not None:
            if _low_run_span(samples, run_start, index - 1) >= min_duration_s - 1e-9:
                return 'step'
            run_start = None
    if run_start is not None:
        if _low_run_span(samples, run_start, len(samples) - 1) >= min_duration_s - 1e-9:
            return 'step'
    return None


class SessionRunner:
    """Drive one protocol, condition by condition and trial by trial.

    State machine (state()['status']):
        idle --start()--> running (first trial of condition 1)
        running --end_trial()--> trial_done
        trial_done --keep_last() / discard_last()--> ready | complete
        trial_done --repeat_trial()--> ready (same trial, re-run before accept)
        ready --begin_trial()--> running
        ready / trial_done --skip_condition()--> ready | complete
    'ready' is the between-trials pause for instructions, foot placement and
    setup; the protocol description's shorthand '... -> complete' covers it.

    Keep / discard / repeat mirrors the clinical trial control: after
    end_trial() the finished trial is 'pending' until the operator resolves it
    with keep_last() (accepted into the condition record) or discard_last()
    (dropped, never counted), and repeat_trial() re-runs the same trial index
    before anything is accepted. Detection flags - including the weight-loss
    'step_suspected' flag - never auto-discard a trial.

    Args:
        protocol: Protocol object or catalog id string.
        compute_metrics: callable(t_list, fz_list, copx_mm_list, copy_mm_list)
            -> dict, stored per trial under record['metrics']. Defaults to
            default_compute_metrics (keeps this module standalone).
        weight_loss_detector: callable(samples) -> None | 'step' | 'pull',
            samples = list of (t, fz_n, copx_mm, copy_mm) tuples of the whole
            trial, called once at end_trial(). Defaults to
            default_weight_loss_detector with the thresholds below.
        step_baseline_s / step_drop_ratio / step_min_duration_s: thresholds
            of the built-in detector (defaults 1.5 s, 0.5, 0.5 s; documented
            in the module constants). Ignored when a custom detector is given.
        auto_end_grace_s: seconds past the condition's target duration_s at
            which the running trial is ended automatically (default
            AUTO_END_GRACE_S, 1.0 s; 0 ends exactly at target). The automatic
            end sets OVERRUN_FLAG on the trial and leaves it pending for the
            operator's keep / discard decision - it never accepts a trial.

    Timing: deterministic from sample timestamps (see module docstring).
    All returned dicts are defensive copies; nothing in a returned structure
    aliases the runner's internal state.
    """

    def __init__(self, protocol, compute_metrics=None, weight_loss_detector=None,
                 step_baseline_s=DEFAULT_STEP_BASELINE_S,
                 step_drop_ratio=DEFAULT_STEP_DROP_RATIO,
                 step_min_duration_s=DEFAULT_STEP_MIN_DURATION_S,
                 auto_end_grace_s=AUTO_END_GRACE_S):
        if isinstance(protocol, str):
            protocol = get_protocol(protocol)
        if not isinstance(protocol, Protocol):
            raise TypeError('protocol must be a Protocol or a catalog id string')
        if compute_metrics is not None and not callable(compute_metrics):
            raise TypeError('compute_metrics must be callable')
        if weight_loss_detector is not None and not callable(weight_loss_detector):
            raise TypeError('weight_loss_detector must be callable')
        if not _is_finite_number(auto_end_grace_s) or auto_end_grace_s < 0:
            raise ValueError('auto_end_grace_s must be a finite number >= 0')
        if weight_loss_detector is None:
            if not _is_finite_number(step_baseline_s) or step_baseline_s <= 0:
                raise ValueError('step_baseline_s must be a positive number')
            if not _is_finite_number(step_drop_ratio) or not 0 < step_drop_ratio < 1:
                raise ValueError('step_drop_ratio must be in (0, 1)')
            if not _is_finite_number(step_min_duration_s) or step_min_duration_s < 0:
                raise ValueError('step_min_duration_s must be >= 0')

            def weight_loss_detector(samples, _b=step_baseline_s,
                                     _r=step_drop_ratio,
                                     _m=step_min_duration_s):
                return default_weight_loss_detector(samples, _b, _r, _m)

        self.protocol = protocol
        self._compute_metrics = compute_metrics or default_compute_metrics
        self._detector = weight_loss_detector
        self._auto_end_grace_s = float(auto_end_grace_s)
        self._status = 'idle'
        self._condition_index = 0
        self._trial_index = 0
        self._t: List[float] = []
        self._fz: List[float] = []
        self._copx: List[Optional[float]] = []
        self._copy: List[Optional[float]] = []
        self._kept: Dict[str, List[dict]] = {c.id: [] for c in protocol.conditions}
        self._pending: Optional[dict] = None
        self._auto_end: Optional[dict] = None
        self._notes = ''
        self._skipped: List[str] = []

    # -- state ------------------------------------------------------------

    def _current_condition(self) -> Optional[Condition]:
        if (self._status == 'idle'
                or self._condition_index >= len(self.protocol.conditions)):
            return None
        return self.protocol.conditions[self._condition_index]

    def _trial_elapsed(self) -> float:
        if len(self._t) >= 2:
            return self._t[-1] - self._t[0]
        return 0.0

    def _reset_buffers(self) -> None:
        self._t = []
        self._fz = []
        self._copx = []
        self._copy = []
        self._auto_end = None

    def state(self) -> dict:
        """Current position: status, condition, trial and trial elapsed time.

        elapsed_s is derived from the sample timestamps of the current (or
        just-finished) trial - see the module docstring. requires_spotter (per
        condition) and fall_risk (protocol level) repeat the safety metadata on
        every reading, so a live consumer that does not re-join the catalog
        still sees the spotter requirement; both are None when no condition is
        current (idle / complete).

        auto_ended / auto_end describe the most recent trial when the duration
        rule (add_sample) ended it rather than the operator: auto_ended is True
        and auto_end carries duration_s (the recorded span), target_s (the
        condition's duration_s), grace_s and overrun_s while the trial waits
        for keep / discard. Both reset when the next trial is armed, so they
        never describe a trial other than the one just finished.
        """
        cond = self._current_condition()
        return {
            'status': self._status,
            'condition_id': None if cond is None else cond.id,
            'condition_index': self._condition_index,
            'trial_index': self._trial_index,
            'trials_in_condition': 0 if cond is None else cond.trials,
            'elapsed_s': self._trial_elapsed(),
            'requires_spotter': None if cond is None else cond.requires_spotter,
            'fall_risk': None if cond is None else self.protocol.fall_risk,
            'auto_ended': self._auto_end is not None,
            'auto_end': None if self._auto_end is None else dict(self._auto_end),
        }

    @property
    def skipped_conditions(self) -> Tuple[str, ...]:
        """Condition ids skipped via skip_condition(), in skip order."""
        return tuple(self._skipped)

    # -- session / trial control ------------------------------------------

    def start(self):
        """Begin the session and arm the first trial (idle -> running)."""
        if self._status != 'idle':
            raise RuntimeError("start() requires status 'idle' (got %r)"
                               % (self._status,))
        self._begin_trial()
        return self.state()

    def begin_trial(self):
        """Arm the next trial after a completed/skipped one (ready -> running)."""
        if self._status != 'ready':
            raise RuntimeError("begin_trial() requires status 'ready' (got %r)"
                               % (self._status,))
        self._begin_trial()
        return self.state()

    def _begin_trial(self) -> None:
        self._reset_buffers()
        self._pending = None
        self._status = 'running'

    def add_sample(self, t, fz_n, copx_mm, copy_mm) -> Optional[dict]:
        """Roll one hardware sample into the running trial.

        t (s) and fz_n (N) must be finite real numbers. copx_mm / copy_mm may
        be None when the channel is unavailable, otherwise finite. Timestamps
        must strictly increase inside a trial; each trial restarts its own
        local clock.

        Returns None on the hot path. When this sample brings the trial's
        sample-timestamp duration to the condition's duration_s plus the
        runner's auto_end_grace_s, the trial is ended here instead of buffering
        forever: status becomes 'trial_done', the record carries OVERRUN_FLAG
        (plus an explanatory note) and, when the sample crossed the threshold,
        that record is returned. The trial is never auto-kept - it waits for
        the operator's keep / discard decision exactly like a manual end.
        """
        if self._status != 'running':
            raise RuntimeError("add_sample() requires status 'running' (got %r)"
                               % (self._status,))
        if not _is_finite_number(t):
            raise ValueError('t must be a finite real number')
        if not _is_finite_number(fz_n):
            raise ValueError('fz_n must be a finite real number')
        for name, value in (('copx_mm', copx_mm), ('copy_mm', copy_mm)):
            if value is not None and not _is_finite_number(value):
                raise ValueError('%s must be None or a finite real number'
                                 % (name,))
        if self._t and t <= self._t[-1]:
            raise ValueError('sample time must strictly increase within a trial')
        self._t.append(float(t))
        self._fz.append(float(fz_n))
        self._copx.append(None if copx_mm is None else float(copx_mm))
        self._copy.append(None if copy_mm is None else float(copy_mm))
        cond = self._current_condition()
        if cond is None:
            return None
        target_s = float(cond.duration_s)
        elapsed_s = self._trial_elapsed()
        if elapsed_s >= target_s + self._auto_end_grace_s:
            self._auto_end = {'duration_s': elapsed_s, 'target_s': target_s,
                              'grace_s': self._auto_end_grace_s,
                              'overrun_s': elapsed_s - target_s}
            return self.end_trial(
                flags=[OVERRUN_FLAG],
                note=('Trial ended automatically at %.2f s (target %g s): the '
                      'trial reached its target duration plus the %g s '
                      'auto-end grace. Review the overrun flag before keeping.'
                      % (elapsed_s, target_s, self._auto_end_grace_s)))
        return None

    def end_trial(self, flags=None, note=None) -> dict:
        """Close the running trial; compute metrics and detection flags.

        The result stays 'pending' until keep_last() or discard_last()
        resolves it. Returns a defensive copy of the trial record:
        {condition_id, condition_index, trial_index, n_samples, duration_s,
         flags, assessable_raw, metrics, notes, kept, requires_spotter,
         fall_risk, auto_end}. assessable_raw is True when the trial holds at
        least one sample (raw data exists); the clinical keep/discard decision
        always stays with the operator. auto_end is None for an operator- or
        stream-ended trial and the {duration_s, target_s, grace_s, overrun_s}
        description when add_sample's duration rule ended it (that path also
        sets the 'overrun' flag).

        Flags/notes are merged from three sources so a compromised trial can
        never read as clean:
        - the built-in detector / flag derivation ('step_suspected', ...;
          a custom detector's non-whitelisted result is namespaced
          'detector:<token>' by detector_result_to_flag, so free-form
          detector text can never pose as a built-in flag);
        - 'short_trial' when the trial's timestamp duration is more than
          SHORT_TRIAL_TOLERANCE_S short of the condition's target duration_s
          (timed conditions only: the target must reach
          SHORT_TRIAL_MIN_TARGET_S, so the 3 s hold-to-capture squat blocks are
          exempt);
        - optional flags / note arguments (the session layer's hook for
          lifecycle events - the service passes flags=['stream_invalidated']
          plus an operator note when a hardware event ends a running trial);
        - the optional QUALITY_FLAGS_KEY / QUALITY_NOTE_KEY entries a
          compute_metrics callable may return (the service adapter reports
          'gapped_data' plus the dropped-run count and gap seconds there).
        Every fragment is appended once, in order, to flags / notes.
        """
        if self._status != 'running':
            raise RuntimeError("end_trial() requires status 'running' (got %r)"
                               % (self._status,))
        cond = self._current_condition()
        n_samples = len(self._t)
        duration_s = (self._t[-1] - self._t[0]) if n_samples >= 2 else 0.0
        trial_flags: List[str] = []
        if n_samples == 0:
            trial_flags.append('empty_trial')
        else:
            samples = list(zip(self._t, self._fz, self._copx, self._copy))
            try:
                detected = self._detector(samples)
            except Exception:            # detector bugs must not lose the trial
                detected = None
                trial_flags.append('weight_loss_detector_error')
            flag = detector_result_to_flag(detected)
            if flag is not None:
                trial_flags.append(flag)
        if (cond.duration_s >= SHORT_TRIAL_MIN_TARGET_S
                and duration_s < cond.duration_s - SHORT_TRIAL_TOLERANCE_S):
            # L2: a rounded-off attempt (e.g. 3 s of a 30 s condition) must not
            # look like a completed trial in summaries and reports.
            trial_flags.append('short_trial')
        try:
            metrics = self._compute_metrics(list(self._t), list(self._fz),
                                            list(self._copx), list(self._copy))
            if not isinstance(metrics, dict):
                raise TypeError('compute_metrics must return a dict')
            metrics = dict(metrics)
        except Exception:                # metrics bugs must not lose the trial
            metrics = {}
            trial_flags.append('metrics_error')
        notes = self._notes
        quality_flags = metrics.get(QUALITY_FLAGS_KEY)
        if isinstance(quality_flags, (list, tuple)):
            for value in quality_flags:
                _append_flag(trial_flags, value)
        quality_note = metrics.get(QUALITY_NOTE_KEY)
        if isinstance(quality_note, str):
            notes = _join_text(notes, quality_note)
        if flags is not None:
            for value in (flags if isinstance(flags, (list, tuple)) else [flags]):
                _append_flag(trial_flags, value)
        if note is not None:
            notes = _join_text(notes, str(note))
        record = {
            'condition_id': cond.id,
            'condition_index': self._condition_index,
            'trial_index': self._trial_index,
            'n_samples': n_samples,
            'duration_s': duration_s,
            'flags': trial_flags,
            'assessable_raw': n_samples > 0,
            'metrics': metrics,
            'notes': notes,
            'kept': None,
            'requires_spotter': cond.requires_spotter,
            'fall_risk': self.protocol.fall_risk,
            'auto_end': (None if self._auto_end is None
                         else dict(self._auto_end)),
        }
        self._pending = record
        self._status = 'trial_done'
        return deepcopy(record)

    def _pending_record(self, method: str) -> dict:
        if self._status != 'trial_done' or self._pending is None:
            raise RuntimeError('%s requires a finished trial awaiting keep / '
                               'discard (status %r)' % (method, self._status))
        return self._pending

    def keep_last(self):
        """Accept the pending trial into the condition record, then advance."""
        record = self._pending_record('keep_last')
        record['kept'] = True
        self._kept[record['condition_id']].append(deepcopy(record))
        self._pending = None
        self._advance()
        return self.state()

    def discard_last(self):
        """Drop the pending trial (never counted), then advance."""
        record = self._pending_record('discard_last')
        record['kept'] = False
        self._pending = None
        self._advance()
        return self.state()

    def repeat_trial(self):
        """Re-run the just-finished trial before accepting it, then advance.

        Mirrors the clinical 'repeat trial' control: the pending result is
        discarded unaccepted and the same condition/trial index is re-armed
        (status 'ready'); the operator starts the re-run with begin_trial().
        Trial notes are cleared for the fresh attempt.
        """
        self._pending_record('repeat_trial')
        self._pending = None
        self._reset_buffers()
        self._notes = ''
        self._status = 'ready'
        return self.state()

    def skip_condition(self):
        """Skip the remaining trials of the current condition.

        Allowed from 'ready', 'running' (the unfinished trial is abandoned) or
        'trial_done' (any finished-but-unresolved trial is discarded); idle and
        complete raise. The skipped condition id is recorded on
        skipped_conditions and its record stays empty in summary().
        """
        if self._status not in ('ready', 'running', 'trial_done'):
            raise RuntimeError("skip_condition() requires status 'ready', "
                               "'running' or 'trial_done' (got %r)"
                               % (self._status,))
        cond = self._current_condition()
        self._skipped.append(cond.id)
        self._pending = None
        self._reset_buffers()
        self._notes = ''
        self._condition_index += 1
        self._trial_index = 0
        self._status = ('complete' if self._condition_index
                        >= len(self.protocol.conditions) else 'ready')
        return self.state()

    def _advance(self) -> None:
        self._reset_buffers()
        self._notes = ''
        self._trial_index += 1
        if self._trial_index >= self._current_condition().trials:
            self._condition_index += 1
            self._trial_index = 0
        self._status = ('complete' if self._condition_index
                        >= len(self.protocol.conditions) else 'ready')

    # -- notes / summary --------------------------------------------------

    def trial_notes(self, text) -> None:
        """Store the operator note of the current trial ('left foot on line').

        Accepted while a trial is armed ('ready'), running, or finished but
        unresolved ('trial_done'); the note travels with the trial record and
        is cleared when the runner advances to the next trial.
        """
        if self._status not in ('ready', 'running', 'trial_done'):
            raise RuntimeError('trial_notes() requires an active trial '
                               '(status %r)' % (self._status,))
        self._notes = str(text)
        if self._status == 'trial_done' and self._pending is not None:
            self._pending['notes'] = self._notes

    def summary(self) -> dict:
        """Per-condition roll-up of kept trials: {condition_id: {...}}.

        Each entry: {'kept_trials': [...defensive copies...],
                     'means': {metric: mean over kept trials},
                     'flag_counts': {flag: count over kept trials},
                     'requires_spotter': bool, 'fall_risk': str}.
        Means use the union of metric keys across the kept trials and are
        None-safe: None, non-numeric and non-finite values are excluded, and a
        declared metric with no countable value reports None. A key whose
        stored value is non-numeric in any trial (a bool 'assessable' flag, a
        'reason' text) is bookkeeping rather than a measurement and never
        enters the union - it must not surface as a None-valued metric.
        Discarded trials and the practice-flagged trials still land here if
        kept (scoring policy - e.g. ASI - is applied by the calling layer from
        the catalog's scored flag). The safety metadata (requires_spotter,
        fall_risk) travels with every condition entry so a stored summary
        never loses the spotter requirement.
        """
        out = {}
        for cond in self.protocol.conditions:
            trials = self._kept[cond.id]
            means = {}
            keys = set()
            for trial in trials:
                for key, value in trial['metrics'].items():
                    if value is None or _is_number(value):
                        keys.add(key)
            for key in sorted(keys):
                values = [float(trial['metrics'][key]) for trial in trials
                          if key in trial['metrics']
                          and _is_finite_number(trial['metrics'][key])]
                means[key] = _mean(values)
            counts = Counter()
            for trial in trials:
                counts.update(trial['flags'])
            out[cond.id] = {
                'kept_trials': deepcopy(trials),
                'means': means,
                'flag_counts': dict(counts),
                'requires_spotter': cond.requires_spotter,
                'fall_risk': self.protocol.fall_risk,
            }
        return out
