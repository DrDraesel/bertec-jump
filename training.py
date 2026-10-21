"""Biofeedback training-mode engine for the Bertec BP5046 static balance plate.

RESEARCH / ENGAGEMENT TRAINING AID - NOT THERAPY, NOT A MEDICAL DEVICE.
This module implements the buildable core of the Balance Advantage training
catalog as three pure-stdlib modes plus a deterministic session state machine
that scores them: a steadiness hold, a target reach (limits-of-stability
style) and a cardinal weight shift. Nothing here diagnoses, treats or clears
anyone; clinical interpretation and return-to-play stay physician-led
(research/USE-CASES-REPORT.md sections E/G).

Sources (not paraphrased away):
- Bertec Balance Advantage Static System User Manual 80P-0009: training
  families with pacing 1-15 s or on-demand / random, random targets, the
  25 % -> 100 % limits-of-stability difficulty progression, and the clinician
  compliance scoring rules reproduced in ``COMPLIANCE_RULES`` below.
- research/USE-CASES-REPORT.md sections D and F (verified 2026-10-15):
  reachability directions, the 25 -> 100 % progression, the Bertec healthy
  lean reference (~6.25-8 deg forward, 4.5 deg back, 8 deg lateral), five
  training families (Quick / Weight Shifting / Seated / Closed Chain /
  Mobility) and the evidence note that augmented COP feedback is the active
  ingredient.
- research/IMPLEMENTATION-ROADMAP.md Phase 4: steadiness hold, target reach,
  weight-shift pacing, compliance scoring per the manual's formulas, with the
  manual's worked example as the acceptance test.

Compliance scoring (the manual's rules, all three implemented):
- Paced (fixed or random) target work: compliance = 100 x targets reached /
  targets presented. Manual example: 2 s pacing for 1 min = 30 targets;
  19 reached = 63.3 %. ``tests/test_training.py`` pins that number.
- No pacing, single target: compliance = percentage of credited time inside
  the target (that is exactly what ``steadiness_hold`` scores).
- On-demand pacing: the score is the mean transition time (seconds from a
  target appearing to the subject reaching it); no percentage applies.

Same-mode families left for later waves (documented here, not silently
dropped): Seated core and Closed-Chain percent-weight-bearing blocks need
protocol context this engine does not have yet; dual-task and audio-cue
layers belong to the service/UI wave.

Quiet-stance only by design (hard rule): every mode keeps both feet on the
plate and works inside a static ankle-strategy lean. No momentum, travel or
loss-of-contact exercise of any kind is defined here, because the BP5046 is a
patient-rated static plate and not rated for dynamic loading - impact-rated
hardware would be required for anything else (report sections F/G). The rule
binds the shipped catalog, not just this prose: every mode is double-leg
quiet stance or a double-leg weight shift, no eyes-closed, one-foot or other
loss-of-contact variation exists, and none may be added as a mode option.
Every mode therefore carries ``requires_spotter`` and ``fall_risk`` metadata,
surfaced by ``state()`` and ``summary()``: a spotter stands within reach of
the plate for every mode, and the moving-target modes are labelled 'moderate'
rather than 'low' because they ask the subject to lean to the edge of the
quiet-stance envelope.

Geometry and units (all parameters pinned and overridable per mode):
- Training frame: ``copx_mm`` is the medial-lateral axis with +x to the
  subject's right and ``copy_mm`` is the antero-posterior axis with +y
  anterior (forward). COP sign verification against the live plate is still
  an open item (report section 6, question 2), so the service layer must map
  verified hardware signs into this frame; the engine only defines training
  geometry.
- Full-scale target positions sit on an ellipse of +/- 60 mm ML by +/- 50 mm
  AP at difficulty 1.0 (``DEFAULT_MAX_ML_MM`` / ``DEFAULT_MAX_AP_MM``).
  Deliberately conservative: this is a sub-maximal engagement aid on a
  patient-rated plate, so near-maximal leans stay in the assessment battery.
  These are pinned engineering defaults, not normative values.
- ``difficulty`` (0.25-1.00) scales target distance from the plate centre,
  which is the manual's 25 % -> 100 % progression. The acceptance zone is a
  circle around the target whose radius is ``max(min_zone_radius_mm,
  zone_radius_fraction x distance)`` - i.e. it grows with the target's
  distance so the demand ramps monotonically while the floor keeps a low
  difficulty target holdable. Membership is inclusive of the boundary (1e-9
  float tolerance).
- That ramp cannot reach the plate centre: ``zone_radius_fraction`` is capped
  at ``MAX_ZONE_RADIUS_FRACTION`` = 0.95, strictly below 1.0, so
  ``zone_radius_fraction x distance`` always stays shorter than the target's
  own distance from the centre (at exactly 1.0 the zone boundary would pass
  through the centre and include it). The one honest exception is the
  ``min_zone_radius_mm`` floor, which is not a fraction of anything: a near
  target at a very low difficulty can sit closer to the plate centre than the
  floor is wide (e.g. an 8 mm floor with a target 5 mm from the centre), and
  that zone then does include the centre. That is intended - the floor is what
  keeps a low-difficulty hold holdable, and the centre-hold mode (whose target
  is the centre itself, radius = floor, no fraction) is the same case with
  distance zero, where the zone is meant to contain the centre.
- Geometry is bounded as well as defaulted: the ``max_ml_mm`` / ``max_ap_mm``
  semi-axes may not exceed twice the pinned default ellipse
  (``MAX_MAX_ML_MM`` = 120 mm, ``MAX_MAX_AP_MM`` = 100 mm, so the longest
  diagonal target sits at sqrt(120^2 + 100^2) ~ 156 mm), and
  ``zone_radius_fraction`` may not exceed ``MAX_ZONE_RADIUS_FRACTION`` = 0.95
  (strictly below 1.0: see the ramp note above).
  The ``min_zone_radius_mm`` floor may not exceed
  ``MAX_MIN_ZONE_RADIUS_MM`` = 50 mm (the full-scale AP semi-axis: a larger
  floor would accept every sample of a centre-standing subject). Without
  those ceilings one option could configure the quiet-stance envelope away -
  a 1e6 mm floor accepts a sample 500 m from the target - so
  ``TrainingOption.check``, ``validate_catalog`` and ``validate_mode_options``
  all enforce them.
- The 8 mm default floor is training-appropriate rather than clinical: it is
  slightly larger than the equivalent-area radius of the cited sport-student
  double-leg 30 s eyes-open 95 % ellipse (125 mm^2 -> r ~ 6.3 mm), so a
  steady stance mostly stays inside it while a swaying one does not.

Timing model: everything is derived from the timestamps handed to
``tick`` / ``add_sample``, never from wall clock, so sessions are
deterministic and replayable. A sample owns the interval that ended at its
timestamp (the same convention as ``protocols._low_run_span``); the first
valid sample of a target and the sample that closes a target switch carry no
credit, so at most one sample interval per switch is unaccounted. An optional
``now_fn`` is accepted only for the display-only ``wall_elapsed_s`` state
field and is never used for scoring.

Credit honesty (missing data): the credited interval between two consecutive
credited samples is capped at ``CREDIT_GAP_MAX_S`` = 0.5 s (50 nominal 100 Hz
samples). A longer gap - a stalled stream, or a dropout whose samples were
omitted instead of fed as invalid placeholders - credits only the capped
interval and raises the sticky ``stream_gap`` flag, so a hole in the data can
never be scored as a perfect hold. The service contract is therefore: feed
invalid samples (None / non-finite COP) across channel dropouts rather than
omitting them; an omitted stretch is visible only through this cap and flag.

Randomness (pacing intervals, random targets) is drawn from an injected
``random.Random``-style object so a session can be replayed exactly; a seeded
generator reproduces the identical target sequence.

Operator input is guarded like the pacing machine: a ``target_hit()`` whose
timestamp does not advance past the previous accepted hit is a duplicate, so
it is rejected and counted in ``duplicate_hit_ignored`` instead of fabricating
a 0.0 s transition on the default on_demand pacing; and ``set_target()``
refuses under on_demand pacing for the same reason ``advance()`` does, because
under on_demand a manual jump always closes a target the subject never
reached. Quality flags (``QUALITY_FLAGS``) are sticky: ``quality`` reports the
first flag raised and ``flags`` reports all of them, on both ``state()`` and
``summary()``.
"""
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import math
import random
from typing import Dict, List, Optional, Tuple

__all__ = [
    'TrainingOption', 'TrainingMode', 'TRAINING_MODES',
    'get_training_mode', 'training_mode_ids', 'validate_catalog',
    'validate_mode_options',
    'TrainingSession',
    'STATUSES', 'PACING_MODES', 'COMPLIANCE_RULES', 'QUALITY_FLAGS',
    'FALL_RISK_LEVELS', 'DIRECTIONS_LOS', 'DIRECTIONS_CARDINAL',
    'DIRECTION_VECTORS',
    'MIN_DIFFICULTY', 'MAX_DIFFICULTY', 'DEFAULT_DIFFICULTY',
    'MIN_PACING_S', 'MAX_PACING_S', 'DEFAULT_PACING_S', 'DEFAULT_PACING',
    'DEFAULT_RANDOM_MIN_S', 'DEFAULT_RANDOM_MAX_S',
    'DEFAULT_MAX_ML_MM', 'DEFAULT_MAX_AP_MM', 'DEFAULT_ZONE_RADIUS_FRACTION',
    'DEFAULT_MIN_ZONE_RADIUS_MM',
    'MAX_MAX_ML_MM', 'MAX_MAX_AP_MM', 'MAX_ZONE_RADIUS_FRACTION',
    'MAX_MIN_ZONE_RADIUS_MM',
    'INSUFFICIENT_COP_AFTER_S', 'INSUFFICIENT_COP_FRACTION',
    'CREDIT_GAP_MAX_S',
]

SOURCE_MANUAL = 'Bertec Balance Advantage Static System User Manual 80P-0009'
SOURCE_REPORT = 'research/USE-CASES-REPORT.md (repo, verified 2026-10-15)'
SOURCE_ROADMAP = 'research/IMPLEMENTATION-ROADMAP.md (Phase 4)'

STATUSES = ('idle', 'running', 'complete')
PACING_MODES = ('on_demand', 'random', 'fixed', 'none')   # manual section F
COMPLIANCE_RULES = ('targets_reached', 'time_in_zone', 'mean_transition')
# Sticky data-quality flags, in report order. ``state()`` / ``summary()``
# report the first flag raised as ``quality`` and all of them as ``flags``:
# insufficient_cop = > 50 % invalid samples after the first second;
# stream_gap       = a credited interval exceeded CREDIT_GAP_MAX_S.
QUALITY_FLAGS = ('insufficient_cop', 'stream_gap')

# Fall-risk vocabulary. 'high' is documented so a future mode can be labelled
# - but nothing in this catalog uses it, because no high-risk (dynamic,
# travel or loss-of-contact) mode is buildable on this plate.
FALL_RISK_LEVELS = ('low', 'moderate', 'high')

MIN_DIFFICULTY = 0.25          # manual training progression starts at 25 %
MAX_DIFFICULTY = 1.0           # ... and ends at the full-scale target ellipse
DEFAULT_DIFFICULTY = 1.0       # full scale is already a sub-maximal envelope

MIN_PACING_S = 1.0             # manual: pacing selectable from 1 s ...
MAX_PACING_S = 15.0            # ... to 15 s
DEFAULT_PACING_S = 2.0         # the manual's worked example uses 2 s
DEFAULT_RANDOM_MIN_S = MIN_PACING_S
DEFAULT_RANDOM_MAX_S = MAX_PACING_S
DEFAULT_PACING = 'on_demand'   # self-paced default; never rushes a subject

DEFAULT_MAX_ML_MM = 60.0       # full-scale target ellipse, medial-lateral
DEFAULT_MAX_AP_MM = 50.0       # full-scale target ellipse, antero-posterior
DEFAULT_ZONE_RADIUS_FRACTION = 0.30   # zone radius = 30 % of target distance
DEFAULT_MIN_ZONE_RADIUS_MM = 8.0   # zone-radius floor (see module docstring)

# Geometry ceilings (review M2): the quiet-stance envelope may not be
# configured away. Twice the pinned default ellipse is the hard maximum for
# each semi-axis; the zone-radius fraction is capped strictly below 1.0 (0.95,
# NEW-5) so the distance ramp alone can never put the zone boundary on the
# plate centre - at exactly 1.0 the boundary would pass through it - while the
# min_zone_radius_mm floor stays the documented exception (see module
# docstring); and a zone floor above the full-scale AP semi-axis (50 mm) would
# accept every sample of a centre-standing subject.
MAX_MAX_ML_MM = 2.0 * DEFAULT_MAX_ML_MM          # 120.0 mm
MAX_MAX_AP_MM = 2.0 * DEFAULT_MAX_AP_MM          # 100.0 mm
MAX_ZONE_RADIUS_FRACTION = 0.95
MAX_MIN_ZONE_RADIUS_MM = DEFAULT_MAX_AP_MM       # 50.0 mm

# Every option that shapes the quiet-stance envelope, with its hard ceiling.
_GEOMETRY_CEILINGS = {
    'max_ml_mm': MAX_MAX_ML_MM,
    'max_ap_mm': MAX_MAX_AP_MM,
    'zone_radius_fraction': MAX_ZONE_RADIUS_FRACTION,
    'min_zone_radius_mm': MAX_MIN_ZONE_RADIUS_MM,
}

INSUFFICIENT_COP_AFTER_S = 1.0        # quality check starts after 1 s ...
INSUFFICIENT_COP_FRACTION = 0.5       # ... and > 50 % invalid flags the run

# Longest sample interval credited to a target (review M1): 0.5 s, i.e. 50
# nominal 100 Hz samples - far above any plausible inter-sample interval on
# this hardware (5 samples even at 10 Hz) and far below any stall that could
# inflate a score. A longer gap between consecutive credited samples credits
# this bound and raises the sticky 'stream_gap' flag.
CREDIT_GAP_MAX_S = 0.5

_EPS = 1e-9

# --- directions -----------------------------------------------------------
# Limits-of-stability clock order (report section D: F, FR, R, BR, B, BL, L,
# FL); the weight shift family works the four cardinals F/B/L/R.
DIRECTIONS_LOS: Tuple[str, ...] = ('front', 'front_right', 'right',
                                   'back_right', 'back', 'back_left',
                                   'left', 'front_left')
DIRECTIONS_CARDINAL: Tuple[str, ...] = ('front', 'back', 'left', 'right')

_SQRT_HALF = math.sqrt(0.5)
DIRECTION_VECTORS: Dict[str, Tuple[float, float]] = {
    'center': (0.0, 0.0),
    'front': (0.0, 1.0),
    'front_right': (_SQRT_HALF, _SQRT_HALF),
    'right': (1.0, 0.0),
    'back_right': (_SQRT_HALF, -_SQRT_HALF),
    'back': (0.0, -1.0),
    'back_left': (-_SQRT_HALF, -_SQRT_HALF),
    'left': (-1.0, 0.0),
    'front_left': (-_SQRT_HALF, _SQRT_HALF),
}


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_finite_number(value) -> bool:
    return _is_number(value) and math.isfinite(float(value))


# --- option schema --------------------------------------------------------

@dataclass(frozen=True)
class TrainingOption:
    """One training-mode option: type, default, range and description.

    kind is 'float' (finite real number, optional -> None also allowed),
    'bool' or 'str'. Ranges are inclusive. ``check`` raises TypeError for a
    wrong type and ValueError for a non-finite number or an out-of-range or
    unknown choice value.
    """

    name: str
    kind: str
    default: object
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    choices: Optional[Tuple[str, ...]] = None
    optional: bool = False
    description: str = ''

    def check(self, value):
        """Validate (and coerce) one value; returns the stored value."""
        if value is None and self.optional:
            return None
        if self.kind == 'float':
            if not _is_number(value):
                raise TypeError('option %r must be a real number'
                                % (self.name,))
            number = float(value)
            if not math.isfinite(number):
                raise ValueError('option %r must be finite' % (self.name,))
            if self.minimum is not None and number < self.minimum:
                raise ValueError('option %r must be >= %r'
                                 % (self.name, self.minimum))
            if self.maximum is not None and number > self.maximum:
                raise ValueError('option %r must be <= %r'
                                 % (self.name, self.maximum))
            return number
        if self.kind == 'bool':
            if not isinstance(value, bool):
                raise TypeError('option %r must be a bool' % (self.name,))
            return value
        if self.kind == 'str':
            if not isinstance(value, str):
                raise TypeError('option %r must be a string' % (self.name,))
            if self.choices is not None and value not in self.choices:
                raise ValueError('option %r must be one of %r'
                                 % (self.name, tuple(self.choices)))
            return value
        raise ValueError('option %r has unknown kind %r'
                         % (self.name, self.kind))


@dataclass(frozen=True)
class TrainingMode:
    """One training mode: metadata, option schema and scoring description.

    ``requires_spotter`` and ``fall_risk`` are machine-readable safety
    metadata (review M4): every shipped mode is double-leg quiet stance work,
    a spotter stands within reach for all of them, and the moving-target modes
    carry 'moderate' fall risk rather than 'low'. Both fields are surfaced by
    ``TrainingSession.state()`` and ``summary()``.
    """

    id: str
    label: str
    description: str
    directions: Tuple[str, ...]
    options: Tuple[TrainingOption, ...]
    compliance_rule: str          # COMPLIANCE_RULES
    requires_spotter: bool        # spotter within reach of the plate
    fall_risk: str                # FALL_RISK_LEVELS
    scoring: str
    safety: str
    sources: str

    @property
    def default_options(self) -> Dict[str, object]:
        """Fresh {option name: default} mapping of this mode's schema."""
        return {spec.name: spec.default for spec in self.options}

    def resolve_options(self, options=None) -> Dict[str, object]:
        """Merge caller options over the defaults and validate everything.

        None -> the defaults. Unknown names, wrong types and out-of-range
        values raise (ValueError / TypeError); the returned dict is a fresh
        object that never aliases the catalog.
        """
        if options is None:
            options = {}
        if not isinstance(options, Mapping):
            raise TypeError('options must be a mapping of option names to '
                            'values')
        specs = {spec.name: spec for spec in self.options}
        unknown = sorted(set(options) - set(specs))
        if unknown:
            raise ValueError('unknown option(s) for mode %r: %r'
                             % (self.id, tuple(unknown)))
        resolved = self.default_options
        for name, value in options.items():
            spec = specs[name]
            resolved[name] = spec.check(value)
        if 'pacing_min_s' in resolved and 'pacing_max_s' in resolved:
            if resolved['pacing_min_s'] > resolved['pacing_max_s']:
                raise ValueError('pacing_min_s must not exceed pacing_max_s')
        return resolved


_DURATION_OPTIONS = (TrainingOption(
    'duration_s', 'float', None, minimum=0.1, optional=True,
    description='Session length in seconds (>= 0.1); None means the '
                'operator stops the session.'),)

_PACING_OPTIONS = (
    TrainingOption(
        'pacing', 'str', DEFAULT_PACING, choices=PACING_MODES,
        description='Target pacing: on_demand (the target advances when the '
                    'subject reaches it), random (intervals drawn from '
                    'pacing_min_s..pacing_max_s), fixed (a constant '
                    'pacing_s window), none (the operator advances the '
                    'target manually).'),
    TrainingOption(
        'pacing_s', 'float', DEFAULT_PACING_S, minimum=MIN_PACING_S,
        maximum=MAX_PACING_S,
        description='Fixed pacing interval in seconds, 1-15 s.'),
    TrainingOption(
        'pacing_min_s', 'float', DEFAULT_RANDOM_MIN_S, minimum=MIN_PACING_S,
        maximum=MAX_PACING_S,
        description='Shortest random pacing interval in seconds, 1-15 s.'),
    TrainingOption(
        'pacing_max_s', 'float', DEFAULT_RANDOM_MAX_S, minimum=MIN_PACING_S,
        maximum=MAX_PACING_S,
        description='Longest random pacing interval in seconds, 1-15 s.'),
    TrainingOption(
        'random_targets', 'bool', False,
        description='True picks each next direction at random (never '
                    'repeating the current one) instead of cycling through '
                    'the direction list in order.'),
)

_GEOMETRY_OPTIONS = (
    TrainingOption(
        'difficulty', 'float', DEFAULT_DIFFICULTY, minimum=MIN_DIFFICULTY,
        maximum=MAX_DIFFICULTY,
        description='Target-distance progression 0.25-1.00 of the full-scale '
                    'target ellipse (the manual starts a progression at '
                    '0.25 and works up to 1.00).'),
    TrainingOption(
        'max_ml_mm', 'float', DEFAULT_MAX_ML_MM, minimum=1.0,
        maximum=MAX_MAX_ML_MM,
        description='Full-scale medial-lateral target distance in mm '
                    '(the base ellipse semi-axis); at most 120 mm, twice '
                    'the conservative 60 mm default.'),
    TrainingOption(
        'max_ap_mm', 'float', DEFAULT_MAX_AP_MM, minimum=1.0,
        maximum=MAX_MAX_AP_MM,
        description='Full-scale antero-posterior target distance in mm '
                    '(the base ellipse semi-axis); at most 100 mm, twice '
                    'the conservative 50 mm default.'),
    TrainingOption(
        'zone_radius_fraction', 'float', DEFAULT_ZONE_RADIUS_FRACTION,
        minimum=0.0, maximum=MAX_ZONE_RADIUS_FRACTION,
        description='Acceptance-zone radius as a fraction of the target '
                    'distance from the plate centre; at most 0.95, so '
                    'the distance ramp keeps the zone boundary short of '
                    'the plate centre (only the radius floor can reach '
                    'it, for a target closer than the floor is wide).'),
    TrainingOption(
        'min_zone_radius_mm', 'float', DEFAULT_MIN_ZONE_RADIUS_MM,
        minimum=0.0, maximum=MAX_MIN_ZONE_RADIUS_MM,
        description='Floor of the acceptance-zone radius in mm; for the '
                    'centre hold target this floor is the whole radius; at '
                    'most 50 mm, the full-scale antero-posterior semi-axis.'),
)

_HOLD_RADIUS_OPTION = TrainingOption(
    'min_zone_radius_mm', 'float', DEFAULT_MIN_ZONE_RADIUS_MM, minimum=0.0,
    maximum=MAX_MIN_ZONE_RADIUS_MM,
    description='Radius in mm of the hold zone around the plate centre '
                '(the centre target sits at distance zero, so the radius '
                'floor is the whole radius); at most 50 mm, the full-scale '
                'antero-posterior semi-axis.')

_STEADINESS_HOLD = TrainingMode(
    id='steadiness_hold',
    label='Steadiness Hold',
    description='Stand quietly with both feet on the plate and keep the COP '
                'cursor inside one target zone at the plate centre for the '
                'whole window; the score is the percentage of the session '
                'spent inside the zone.',
    directions=('center',),
    options=_DURATION_OPTIONS + (_HOLD_RADIUS_OPTION,),
    compliance_rule='time_in_zone',
    requires_spotter=True,
    fall_risk='low',
    scoring='No pacing, single target (the manual rule): compliance = 100 x '
            '(credited time with the COP inside the zone) / (credited sample '
            'time). Reported as time_in_zone_pct and as compliance_pct.',
    safety='Quiet double-leg standing only, both feet on the plate and in '
           'contact with it for the whole session. No eyes-closed, one-foot '
           'or other loss-of-contact variation is defined by this engine and '
           'none may be added as an option; a spotter stands within reach of '
           'the plate throughout. Stop on dizziness, discomfort or any loss '
           'of balance. Research engagement aid - not therapy and not a '
           'medical device.',
    sources='{0}: training families and compliance Scoring rules. {1} '
            'sections D/F (steadiness holds with live COP feedback; '
            'augmented feedback is the active ingredient). {2}.'
            .format(SOURCE_MANUAL, SOURCE_REPORT, SOURCE_ROADMAP),
)

_TARGET_REACH = TrainingMode(
    id='target_reach',
    label='Target Reach (limits-of-stability directions)',
    description='Move the COP cursor to one highlighted target at a time '
                'through the eight limits-of-stability directions; the '
                'difficulty option scales the target distance from the plate '
                'centre over the 0.25-1.00 progression.',
    directions=DIRECTIONS_LOS,
    options=_DURATION_OPTIONS + _PACING_OPTIONS + _GEOMETRY_OPTIONS,
    compliance_rule='targets_reached',
    requires_spotter=True,
    fall_risk='moderate',
    scoring='Paced (fixed or random): compliance = 100 x targets reached / '
            'targets presented - the manual example is 2 s pacing for 1 min '
            '= 30 targets, 19 reached = 63.3 %. On-demand: mean transition '
            'time in seconds from a target appearing to being reached. No '
            'pacing with a single target: percentage of credited time inside '
            'the target (see time_in_zone_pct).',
    safety='Both feet stay on the plate and in contact with it for the whole '
           'session; the subject leans with an ankle strategy and never '
           'leaves the plate. This plate is patient-rated static equipment - '
           'no dynamic exercise and no momentum of any kind. A spotter '
           'stands within reach of the plate throughout. Stop on dizziness, '
           'discomfort or loss of balance. Research engagement aid - not '
           'therapy and not a medical device.',
    sources='{0}: training families, pacing (1-15 s / on-demand / random), '
            'random targets, the 25-100 % progression and the compliance '
            'formulas. {1} sections D/F (LOS directions and the training '
            'progression). {2}.'
            .format(SOURCE_MANUAL, SOURCE_REPORT, SOURCE_ROADMAP),
)

_WEIGHT_SHIFT = TrainingMode(
    id='weight_shift',
    label='Weight Shift (cardinal directions)',
    description='Shift body weight toward one of the four cardinal targets '
                '(front / back / left / right) shown one at a time; pacing, '
                'random targets and the difficulty progression match target '
                'reach.',
    directions=DIRECTIONS_CARDINAL,
    options=_DURATION_OPTIONS + _PACING_OPTIONS + _GEOMETRY_OPTIONS,
    compliance_rule='targets_reached',
    requires_spotter=True,
    fall_risk='moderate',
    scoring='Same three manual rules as target reach: paced sessions score '
            '100 x targets reached / targets presented, on-demand sessions '
            'score the mean transition time, and a single unpaced target '
            'scores the percentage of credited time inside it.',
    safety='Both feet stay on the plate and in contact with it for the whole '
           'session; the subject shifts weight with an ankle strategy and '
           'never leaves the plate. This plate is patient-rated static '
           'equipment - no dynamic exercise and no momentum of any kind. A '
           'spotter stands within reach of the plate throughout. Stop on '
           'dizziness, discomfort or loss of balance. Research engagement '
           'aid - not therapy and not a medical device.',
    sources='{0}: the Weight Shifting training family. {1} sections D/F '
            '(weight shifting F/B/L/R with pacing 1-15 s / on-demand / none, '
            'random targets, LOS 25-100 % progression). {2}.'
            .format(SOURCE_MANUAL, SOURCE_REPORT, SOURCE_ROADMAP),
)

TRAINING_MODES: Dict[str, TrainingMode] = {
    mode.id: mode for mode in (_STEADINESS_HOLD, _TARGET_REACH, _WEIGHT_SHIFT)
}


def training_mode_ids() -> Tuple[str, ...]:
    """Catalog order (stable): -> ('steadiness_hold', 'target_reach', ...)."""
    return tuple(TRAINING_MODES)


def get_training_mode(mode_id: str) -> TrainingMode:
    """Look up a training mode by id; ValueError on an unknown id."""
    try:
        return TRAINING_MODES[mode_id]
    except (KeyError, TypeError):
        raise ValueError('unknown training mode id: %r' % (mode_id,)) from None


def validate_mode_options(mode_id, options=None) -> Dict[str, object]:
    """Validate mode options without building a session (pure, read-only).

    Returns a fresh {option name: resolved value} mapping, or raises exactly
    what the ``TrainingSession`` constructor would: TypeError for a
    non-mapping ``options`` or a wrong-typed value, ValueError for an unknown
    mode id, an unknown option, a non-finite number, an out-of-range value
    (including the quiet-stance geometry ceilings) or inverted random pacing
    bounds. Nothing is mutated and the catalog is never aliased, so the
    service / UI wiring wave can call this before ``start()`` and refuse to
    begin a session whose options the engine would reject.
    """
    return get_training_mode(mode_id).resolve_options(options)


def validate_catalog() -> None:
    """Raise ValueError if the training catalog violates its invariants.

    Beyond the shape checks (ids, non-empty text, known rules/directions,
    unique option names) this enforces the quiet-stance rails: every mode
    declares ``requires_spotter`` / ``fall_risk`` from the documented
    vocabularies, and every envelope-shaping option carries both bounds with
    a maximum inside its ceiling (``_GEOMETRY_CEILINGS``), so no preset can
    configure the envelope away.
    """
    if not TRAINING_MODES:
        raise ValueError('training catalog is empty')
    for key, mode in TRAINING_MODES.items():
        if key != mode.id or not mode.id:
            raise ValueError('catalog key/id mismatch: %r' % (key,))
        for field in ('label', 'description', 'scoring', 'safety', 'sources'):
            if not str(getattr(mode, field)).strip():
                raise ValueError('mode %r has an empty %s' % (key, field))
        if mode.compliance_rule not in COMPLIANCE_RULES:
            raise ValueError('mode %r has an unknown compliance rule' % (key,))
        if not isinstance(mode.requires_spotter, bool):
            raise ValueError('mode %r must declare requires_spotter as a bool'
                             % (key,))
        if mode.fall_risk not in FALL_RISK_LEVELS:
            raise ValueError('mode %r has an unknown fall_risk %r'
                             % (key, mode.fall_risk))
        if not mode.directions:
            raise ValueError('mode %r has no directions' % (key,))
        seen_directions = set()
        for direction in mode.directions:
            if direction not in DIRECTION_VECTORS:
                raise ValueError('mode %r has an unknown direction %r'
                                 % (key, direction))
            if direction in seen_directions:
                raise ValueError('mode %r repeats a direction %r'
                                 % (key, direction))
            seen_directions.add(direction)
        if not mode.options:
            raise ValueError('mode %r has no option schema' % (key,))
        seen_options = set()
        for spec in mode.options:
            if not spec.name or spec.name in seen_options:
                raise ValueError('mode %r has a duplicate/empty option name'
                                 % (key,))
            seen_options.add(spec.name)
            if not spec.description.strip():
                raise ValueError('option %r.%s has no description'
                                 % (key, spec.name))
            spec.check(spec.default)          # defaults must be valid
            ceiling = _GEOMETRY_CEILINGS.get(spec.name)
            if ceiling is not None:
                # Envelope-shaping options must be bounded (review M2).
                if (spec.kind != 'float' or spec.minimum is None
                        or spec.maximum is None):
                    raise ValueError('geometry option %r.%s must carry both '
                                     'bounds' % (key, spec.name))
                if spec.maximum > ceiling:
                    raise ValueError('geometry option %r.%s exceeds the '
                                     'quiet-stance ceiling %r'
                                     % (key, spec.name, ceiling))
        if 'pacing' in seen_options:
            defaults = mode.default_options
            if defaults['pacing'] not in PACING_MODES:
                raise ValueError('mode %r has an invalid default pacing'
                                 % (key,))
            if not MIN_PACING_S <= defaults['pacing_s'] <= MAX_PACING_S:
                raise ValueError('mode %r has an out-of-range default pacing_s'
                                 % (key,))
            if defaults['pacing_min_s'] > defaults['pacing_max_s']:
                raise ValueError('mode %r has inverted random pacing bounds'
                                 % (key,))
        elif mode.compliance_rule != 'time_in_zone':
            raise ValueError('mode %r has no pacing schema but is not a '
                             'single-target hold' % (key,))
    return None


validate_catalog()   # fail fast on a malformed catalog at import time


class TrainingSession:
    """One training session of one mode, fed by explicit calls.

    Lifecycle: ``idle --start()--> running --stop()--> complete`` (the session
    also completes itself when a ``duration_s`` option is reached). ``stop()``
    is idempotent: stopping an idle session completes it as an empty session,
    and stopping a complete session changes nothing.

    Target sequencing:
    - fixed pacing: the target closes at the first call at or after its
      scheduled instant and the next one is scheduled a full interval later;
      a stalled stream therefore resynchronises instead of producing a burst
      of targets.
    - random pacing: the same, with each interval drawn from the injected
      ``rng`` (``uniform(pacing_min_s, pacing_max_s)``, clamped to the
      manual's 1-15 s band).
    - on_demand: the target advances when the subject reaches it -
      automatically when a valid sample enters the zone, or explicitly via
      ``target_hit()``.
    - none: the operator advances with ``advance(by_operator=True)``.
    - steadiness_hold has no target sequencing (one centre target).

    Scoring inputs: a valid sample inside the current target's zone marks
    that target reached (``targets_reached``) and can complete an on-demand
    target. ``add_sample`` accepts missing/non-finite COP (the channel may
    drop on hardware) and counts those samples as invalid instead of raising;
    a non-finite or non-increasing ``t`` always raises. ``tick(t)`` and
    ``add_sample(t, ...)`` consume one strictly increasing time stream.

    Missing-data and operator guards (review M1/M3): a credited interval
    longer than ``CREDIT_GAP_MAX_S`` is capped at that bound and raises the
    sticky 'stream_gap' flag (feed invalid placeholder samples across channel
    dropouts instead of omitting them); a ``target_hit()`` whose timestamp
    does not advance past the previous accepted hit is counted in
    ``duplicate_hit_ignored`` and changes nothing; and ``set_target()`` is
    refused under on_demand pacing for the same reason ``advance()`` is.

    Args:
        mode_id: TrainingMode id string.
        options: optional mapping of option names to values; validated
            against the mode's schema (defaults come from the catalog).
        now_fn: optional callable returning wall-clock seconds; used only for
            the display-only ``state()['wall_elapsed_s']`` (None when absent
            or not started) and never for scoring.
        rng: optional random.Random-style object (uniform + choice) injected
            for deterministic replay of random pacing and random targets.

    All returned dicts are defensive copies; nothing returned aliases the
    session's internal state.
    """

    def __init__(self, mode_id, options=None, now_fn=None, rng=None):
        if not isinstance(mode_id, str):
            raise TypeError('mode_id must be a training-mode id string')
        mode = get_training_mode(mode_id)
        if now_fn is not None and not callable(now_fn):
            raise TypeError('now_fn must be callable')
        if rng is not None and not (hasattr(rng, 'uniform')
                                    and hasattr(rng, 'choice')):
            raise TypeError('rng must provide uniform() and choice() '
                            '(e.g. random.Random)')
        self.mode = mode
        self._options = mode.resolve_options(options)
        self._now_fn = now_fn
        self._rng = rng if rng is not None else random.Random()
        self._pacing = self._options.get('pacing', 'none')
        self._status = 'idle'
        self._t0: Optional[float] = None
        self._last_t: Optional[float] = None
        self._last_valid_t: Optional[float] = None
        self._next_switch_t: Optional[float] = None
        self._targets: List[dict] = []
        self._index = -1
        self._cycle = 0
        self._in_zone: Optional[bool] = None
        self._samples = 0
        self._valid = 0
        self._invalid = 0
        self._quality: Optional[str] = None
        self._flags: List[str] = []
        self._last_hit_t: Optional[float] = None
        self._duplicate_hit_ignored = 0
        self._wall_start: Optional[float] = None

    # -- introspection -----------------------------------------------------

    @property
    def options(self) -> Dict[str, object]:
        """The resolved option values of this session (defensive copy)."""
        return dict(self._options)

    def state(self) -> dict:
        """Current position of the session (stable keys, defensive copy).

        {'status', 'mode', 'elapsed_s' (sample-derived), 'wall_elapsed_s'
         (display only, None without now_fn), 'target_index' (-1 before
         start()), 'targets_total', 'targets_reached', 'duplicate_hit_ignored'
         (operator hits rejected by the debounce), 'target' (current target
         record or None), 'in_zone' (membership of the most recent valid
         sample against the target that was active then; None before any
         valid sample or right after a target switch), 'quality' (first sticky
         flag raised, None when clean), 'flags' (all sticky flags, in the
         order raised), 'requires_spotter', 'fall_risk' and 'safety' (the
         mode's safety metadata, so a live UI never has to look up the
         catalog to render the policy)}.
        """
        return {
            'status': self._status,
            'mode': self.mode.id,
            'elapsed_s': self._elapsed(),
            'wall_elapsed_s': self._wall_elapsed(),
            'target_index': self._index,
            'targets_total': len(self._targets),
            'targets_reached': self._reached(),
            'duplicate_hit_ignored': self._duplicate_hit_ignored,
            'target': (None if self._current() is None
                       else deepcopy(self._current())),
            'in_zone': self._in_zone,
            'quality': self._quality,
            'flags': tuple(self._flags),
            'requires_spotter': self.mode.requires_spotter,
            'fall_risk': self.mode.fall_risk,
            'safety': self.mode.safety,
        }

    def targets(self) -> Tuple[dict, ...]:
        """Every target presented so far, in order (defensive copies).

        Each record: {'index', 'direction', 'x_mm', 'y_mm', 'radius_mm',
        'presented_t', 'closed_t', 'hit', 'hit_t', 'transition_s', 'time_s',
        'in_zone_s'}. 'time_s' / 'in_zone_s' are the credited seconds for the
        target (see the module docstring's timing model).
        """
        return tuple(deepcopy(target) for target in self._targets)

    def summary(self) -> dict:
        """Scores and counters of the session so far (stable keys).

        {'mode', 'duration_s' (sample-derived), 'targets_total',
         'targets_reached', 'compliance_rule' (the rule actually applied),
         'compliance_pct' (None when no rule applies), 'time_in_zone_pct',
         'time_in_zone_s', 'mean_transition_s', 'samples', 'valid_fraction',
         'quality' (first sticky flag raised), 'flags' (all sticky flags),
         'duplicate_hit_ignored', 'requires_spotter', 'fall_risk', 'safety'}.
         Percentages are rounded to 0.1 (the manual's 63.3 % example) and
         mean_transition_s to 3 decimals; every division is guarded, so an
         empty session reports None rather than raising. This is the session
         record a caller persists - it carries the safety metadata so a
         stored session still states its own spotter / fall-risk policy.
        """
        reached = self._reached()
        total = len(self._targets)
        time_s = sum(target['time_s'] for target in self._targets)
        in_zone_s = sum(target['in_zone_s'] for target in self._targets)
        in_zone_pct = (None if time_s <= 0.0
                       else round(100.0 * in_zone_s / time_s, 1))
        transitions = [target['transition_s'] for target in self._targets
                       if target['transition_s'] is not None]
        mean_transition = (None if not transitions
                           else round(sum(transitions) / len(transitions), 3))
        if self._pacing == 'on_demand':
            rule, compliance = 'mean_transition', None
        elif self.mode.compliance_rule == 'time_in_zone':
            rule, compliance = 'time_in_zone', in_zone_pct
        elif total == 0:
            rule, compliance = 'targets_reached', None
        elif self._pacing == 'none' and total <= 1:
            rule, compliance = 'time_in_zone', in_zone_pct
        else:
            rule, compliance = ('targets_reached',
                                round(100.0 * reached / total, 1))
        return {
            'mode': self.mode.id,
            'duration_s': self._elapsed(),
            'targets_total': total,
            'targets_reached': reached,
            'compliance_rule': rule,
            'compliance_pct': compliance,
            'time_in_zone_pct': in_zone_pct,
            'time_in_zone_s': round(in_zone_s, 3),
            'mean_transition_s': mean_transition,
            'samples': self._samples,
            'valid_fraction': (None if self._samples == 0
                               else self._valid / self._samples),
            'quality': self._quality,
            'flags': tuple(self._flags),
            'duplicate_hit_ignored': self._duplicate_hit_ignored,
            'requires_spotter': self.mode.requires_spotter,
            'fall_risk': self.mode.fall_risk,
            'safety': self.mode.safety,
        }

    # -- helpers -----------------------------------------------------------

    def _current(self) -> Optional[dict]:
        if 0 <= self._index < len(self._targets):
            return self._targets[self._index]
        return None

    def _reached(self) -> int:
        return sum(1 for target in self._targets if target['hit'])

    def _elapsed(self) -> float:
        if self._t0 is None or self._last_t is None:
            return 0.0
        return self._last_t - self._t0

    def _wall_elapsed(self) -> Optional[float]:
        if self._now_fn is None or self._wall_start is None:
            return None
        return self._now_fn() - self._wall_start

    def _require_running(self, method: str) -> None:
        if self._status != 'running':
            raise RuntimeError("%s() requires status 'running' (got %r)"
                               % (method, self._status))

    def _raise_flag(self, name: str) -> None:
        """Raise one sticky quality flag (idempotent).

        ``quality`` reports the first flag raised; ``flags`` reports all of
        them in the order they were raised. Flags are never cleared - a run
        that lost data stays flagged for its whole length.
        """
        if name in self._flags:
            return
        self._flags.append(name)
        if self._quality is None:
            self._quality = name

    def _zone_radius_mm(self, distance_mm: float) -> float:
        """Zone radius for a target ``distance_mm`` from the plate centre.

        The fraction ramp is capped at MAX_ZONE_RADIUS_FRACTION = 0.95, so
        ``fraction x distance`` stays strictly below the distance itself and
        the ramp alone can never put the zone boundary on the plate centre.
        The floor is deliberately not capped that way: a target closer to the
        centre than the floor is wide (a very low difficulty, or the centre
        hold whose distance is zero) has a zone that includes the centre -
        the documented, intended exception (see the module docstring).
        """
        floor_mm = self._options.get('min_zone_radius_mm',
                                     DEFAULT_MIN_ZONE_RADIUS_MM)
        fraction = self._options.get('zone_radius_fraction')
        if fraction is None:               # centre hold: no distance ramp
            return float(floor_mm)
        return max(float(floor_mm), float(fraction) * distance_mm)

    def _inside_zone(self, target: dict, copx_mm: float,
                     copy_mm: float) -> bool:
        """Inclusive zone membership (1e-9 boundary tolerance)."""
        distance = math.hypot(copx_mm - target['x_mm'],
                              copy_mm - target['y_mm'])
        return distance <= target['radius_mm'] + _EPS

    def _resolve_direction(self, value) -> int:
        directions = self.mode.directions
        if isinstance(value, str):
            if value not in directions:
                raise ValueError('mode %r has no direction %r'
                                 % (self.mode.id, value))
            return directions.index(value)
        if isinstance(value, int) and not isinstance(value, bool):
            if not 0 <= value < len(directions):
                raise ValueError('direction index out of range: %r' % (value,))
            return value
        raise TypeError('target must be a direction id string or an index')

    def _draw_direction(self, explicit=None):
        directions = self.mode.directions
        if explicit is not None:
            index = self._resolve_direction(explicit)
            self._cycle = index + 1
            return directions[index]
        if self._options.get('random_targets', False) and len(directions) > 1:
            previous = (self._targets[-1]['direction']
                        if self._targets else None)
            pool = [d for d in directions if d != previous]
            return self._rng.choice(pool)
        direction = directions[self._cycle % len(directions)]
        self._cycle += 1
        return direction

    def _present_target(self, presented_t: Optional[float], explicit=None):
        direction = self._draw_direction(explicit)
        unit_x, unit_y = DIRECTION_VECTORS[direction]
        difficulty = self._options.get('difficulty', DEFAULT_DIFFICULTY)
        x_mm = (unit_x * self._options.get('max_ml_mm', DEFAULT_MAX_ML_MM)
                * difficulty)
        y_mm = (unit_y * self._options.get('max_ap_mm', DEFAULT_MAX_AP_MM)
                * difficulty)
        target = {
            'index': len(self._targets),
            'direction': direction,
            'x_mm': x_mm,
            'y_mm': y_mm,
            'radius_mm': self._zone_radius_mm(math.hypot(x_mm, y_mm)),
            'presented_t': presented_t,
            'closed_t': None,
            'hit': False,
            'hit_t': None,
            'transition_s': None,
            'time_s': 0.0,
            'in_zone_s': 0.0,
        }
        self._targets.append(target)
        self._index = target['index']
        self._in_zone = None
        return target

    def _switch_target(self, t: float, explicit=None) -> None:
        current = self._current()
        if current is not None and current['closed_t'] is None:
            current['closed_t'] = t
        self._last_valid_t = None   # the sample spanning the switch scores 0
        self._present_target(t, explicit)

    def _draw_interval(self) -> float:
        low = self._options.get('pacing_min_s', DEFAULT_RANDOM_MIN_S)
        high = self._options.get('pacing_max_s', DEFAULT_RANDOM_MAX_S)
        interval = self._rng.uniform(low, high)
        return min(max(float(interval), MIN_PACING_S), MAX_PACING_S)

    def _schedule_next_switch(self, t: float) -> None:
        if self._pacing == 'fixed':
            self._next_switch_t = t + self._options['pacing_s']
        elif self._pacing == 'random':
            self._next_switch_t = t + self._draw_interval()
        else:
            self._next_switch_t = None

    def _finish(self, t: Optional[float]) -> None:
        current = self._current()
        if current is not None and current['closed_t'] is None:
            current['closed_t'] = t
        self._last_valid_t = None
        self._status = 'complete'

    def _mark_hit(self, t: Optional[float]) -> None:
        target = self._current()
        if target is None:
            return
        if not target['hit']:
            target['hit'] = True
            target['hit_t'] = t
            if t is not None:
                self._last_hit_t = t     # watermark for the duplicate guard
            if t is not None and target['presented_t'] is not None:
                target['transition_s'] = t - target['presented_t']
        if (self._pacing == 'on_demand' and self._status == 'running'):
            moment = t if t is not None else 0.0
            self._switch_target(moment)
            self._schedule_next_switch(moment)

    def _update_time(self, t: float) -> None:
        """Consume one timestamp: learn t0, switch and complete on time."""
        if self._t0 is None:
            self._t0 = t
            current = self._current()
            if current is not None and current['presented_t'] is None:
                current['presented_t'] = t
            self._schedule_next_switch(t)
        duration_s = self._options.get('duration_s')
        if duration_s is not None and (t - self._t0) >= duration_s - _EPS:
            self._last_t = t
            self._finish(t)
            return
        if (self._next_switch_t is not None
                and t >= self._next_switch_t - _EPS):
            self._switch_target(t)
            self._schedule_next_switch(t)
        self._last_t = t

    def _register_position(self, t: float, copx_mm: float, copy_mm: float
                           ) -> None:
        target = self._current()
        if target is None:
            self._last_valid_t = t
            return
        inside = self._inside_zone(target, copx_mm, copy_mm)
        if self._last_valid_t is not None:
            dt = t - self._last_valid_t
            if dt > CREDIT_GAP_MAX_S:
                # A hole in the stream is not evidence of a hold (review M1):
                # credit at most one nominal cadence multiple and flag the run
                # instead of scoring the whole stall as time in the zone.
                self._raise_flag('stream_gap')
                dt = CREDIT_GAP_MAX_S
            if dt > 0.0:
                target['time_s'] += dt
                if inside:
                    target['in_zone_s'] += dt
        self._last_valid_t = t
        self._in_zone = inside
        if inside:
            self._mark_hit(t)

    def _check_quality(self, t: float) -> None:
        if 'insufficient_cop' in self._flags or self._t0 is None:
            return
        if (t - self._t0) < INSUFFICIENT_COP_AFTER_S - _EPS:
            return
        mostly_invalid = (self._invalid
                          > INSUFFICIENT_COP_FRACTION * self._samples)
        if self._samples and mostly_invalid:
            self._raise_flag('insufficient_cop')

    def _guard_time(self, t) -> None:
        if not _is_finite_number(t):
            raise ValueError('t must be a finite real number')
        if self._last_t is not None and t <= self._last_t:
            raise ValueError('time must strictly increase within a session')

    # -- session control ---------------------------------------------------

    def start(self):
        """Begin the session and present the first target (idle -> running)."""
        if self._status != 'idle':
            raise RuntimeError("start() requires status 'idle' (got %r)"
                               % (self._status,))
        if self._now_fn is not None:
            self._wall_start = self._now_fn()
        self._status = 'running'
        self._present_target(None)
        return self.state()

    def tick(self, t):
        """Advance the session clock to t (pacing switches, auto-complete).

        This is the pacing heartbeat for callers that also feed samples
        through add_sample (both consume the same strictly increasing time
        stream). Returns state().
        """
        self._require_running('tick')
        self._guard_time(t)
        self._update_time(float(t))
        return self.state()

    def add_sample(self, t, copx_mm, copy_mm) -> None:
        """Roll one hardware sample into the session (hot path, returns None).

        t must be finite and strictly increasing; copx_mm / copy_mm may be
        None or non-finite when the channel drops (the sample counts as
        invalid rather than raising). Membership is evaluated against the
        target active at that moment; entering the zone marks the target
        reached and, under on-demand pacing, advances it. The sample that
        reaches a duration_s session completes it; later calls raise.
        """
        self._require_running('add_sample')
        self._guard_time(t)
        self._update_time(float(t))
        self._samples += 1
        if _is_finite_number(copx_mm) and _is_finite_number(copy_mm):
            self._valid += 1
            self._register_position(float(t), float(copx_mm), float(copy_mm))
        else:
            self._invalid += 1
        self._check_quality(float(t))

    def target_hit(self):
        """Mark the current target reached (explicit on-demand / operator).

        Under on-demand pacing this also closes the target, records the
        transition time and presents the next one. Under paced modes it only
        records that the subject reached the target. A hit whose timestamp
        does not advance past the previous accepted hit (a double-click, a
        replayed debounce event, or a hit right after the subject's own zone
        entry switched the target) is rejected as a duplicate: it is counted
        in ``duplicate_hit_ignored`` and touches nothing, so it can never
        fabricate a 0.0 s transition on the default on_demand pacing.
        Returns state().
        """
        self._require_running('target_hit')
        moment = self._last_t
        if (moment is not None and self._last_hit_t is not None
                and moment <= self._last_hit_t):
            self._duplicate_hit_ignored += 1
            return self.state()
        self._mark_hit(moment)
        return self.state()

    def advance(self, by_operator: bool = False):
        """Close the current target and present the next one manually.

        Meant for none-pacing operator control; accepted for fixed / random
        pacing as an operator override (the next switch is then rescheduled
        a full interval out). ``by_operator=True`` is required so no caller
        can silently skip a target. Returns state().
        """
        self._require_running('advance')
        if not by_operator:
            raise ValueError('advance() moves the target manually: pass '
                             'by_operator=True')
        if self._pacing == 'on_demand':
            raise RuntimeError("advance() is not used with on_demand pacing: "
                               "call target_hit() instead")
        moment = self._last_t if self._last_t is not None else 0.0
        self._switch_target(moment)
        self._schedule_next_switch(moment)
        return self.state()

    def set_target(self, target):
        """Present a specific direction now (operator choice / exercise plan).

        ``target`` is a direction id ('right') or an index into the mode's
        direction tuple. The current target is closed; the chosen target is
        presented at the current clock time and counts toward targets_total.
        Under on_demand pacing this is refused, exactly like ``advance()``:
        the current target always advances the moment the subject reaches it,
        so an operator-side jump would always close a target that was never
        reached (the subject's own advance is ``target_hit()``). Paced modes
        (none / fixed / random) accept it as the documented operator override,
        and a target closed that way stays ``hit: False``. Returns state().
        """
        self._require_running('set_target')
        self._resolve_direction(target)       # validate before touching state
        if self._pacing == 'on_demand':
            raise RuntimeError("set_target() is not used with on_demand "
                               "pacing: call target_hit() instead")
        moment = self._last_t if self._last_t is not None else 0.0
        self._switch_target(moment, explicit=target)
        self._schedule_next_switch(moment)
        return self.state()

    def stop(self):
        """End the session (idempotent); closes the current target window.

        Allowed from any status: an idle session completes as an empty
        session, a running session closes its open target, and a complete
        session is unchanged (so a caller may always call stop() in a
        finally-style flow).
        """
        if self._status == 'running':
            self._finish(self._last_t)
        elif self._status == 'idle':
            self._status = 'complete'
        return self.state()
