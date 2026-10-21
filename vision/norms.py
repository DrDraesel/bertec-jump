"""Age/sex reference bands and provisional MDC - the truthful reference layer.

Scope
-----
This module encodes a faithful SUBSET of research/NORMS-AND-NEURO-INTERPRETATION.md
as plain data: published means (and SDs where the source published them),
published corridors, the device/task each value belongs to, a source URL and a
``transferable`` flag.  Nothing is modelled, interpolated or averaged across
sources.  Where the report says "not published", no entry exists here and
:func:`lookup` returns ``None`` - callers then print the raw value only.

The five rules the data obey (norms report section 0)
-----------------------------------------------------
1. Norms are device-bound.  COP path length/velocity depend on plate size,
   sampling rate, filtering, duration, foot position, footwear and target
   distance, so a Bertec BP5046 value cannot be scored against BTrackS,
   NeuroCom, Zebris, Leonardo or Kistler percentiles.  Those entries carry
   ``transferable=False`` and must never produce a verdict.
2. Duration changes path length, not velocity - a 20 s path length is not
   comparable with 30/60 s values.
3. Path length, area and velocity are different constructs; area dissociates
   from velocity and is the noisiest metric.
4. Sex effects are real but small and inconsistent; sex is adjusted only where
   the source stratified by sex (entries with ``sex is None`` are pooled).
5. A plate produces screening signals, not diagnoses.

Lookup semantics (documented, no silent interpolation)
-----------------------------------------------------
* Exact ``measure`` key only - there is no fuzzy matching and no unit
  conversion.
* Age must fall inside ``[age_min, age_max]`` inclusive.  Entries with both
  bounds ``None`` are *unbanded*: the source did not stratify that measure by
  age, so they match any age (and any unknown age) but rank below any banded
  entry.  A banded entry never matches when the age is unknown.
* Sex-specific entries are preferred; an entry with ``sex is None`` is a
  fallback.  An unrecognised sex string is treated as unspecified, so it can
  only match a pooled entry - never someone else's sex-specific band.
* Among equally-sexed, equally-banded candidates the NARROWEST band wins; ties
  keep table order.  There is no interpolation between bands: an age outside
  every published band returns ``None``.

MDC (minimal detectable change)
-------------------------------
:func:`mdc_for` is keyed by measure AND stance, because every published COP
constant in this file was derived in ONE stance:

* ``stance='single_leg'`` - COP velocity uses the absolute 9.0 mm/s from
  :data:`metrics.MDC_TABLE` (Vindis 2026: MDC95 ~6.3-9.7 mm/s; 9.0 is the
  conservative upper value).  The 28-31 % -of-baseline form from Kozinc 2025
  (~9.8-10.9 mm/s at the 35.0 mm/s reference) is carried as
  ``percent_of_mean`` for cross-checking only and is never the applied
  threshold.  COP area uses ~50 % of baseline.  Both are single-leg rules and
  must not be applied to double-leg trials.
* ``stance='double_leg'`` - no published MDC exists for this plate, so
  ``mdc_for`` returns ``None`` and the caller must say so.
* stance ``None``/unknown - ``mdc_for`` returns ``None``.  The single-leg
  constants are never borrowed by default; a caller must name the stance.
* ``mctsib_*`` keys - ``absolute`` thresholds (0.140-0.285 deg/s firm and foam
  eyes-open, 1.12 deg/s foam eyes-closed); the condition is part of the key,
  so no stance argument is needed.

Every rule is flagged ``provisional``: the constants come from other labs and
other devices, and a local test-retest study is required before any change
score is used clinically (Vindis 2026; Kozinc 2025).
:func:`interpret_change` therefore never says "improved" or "declined" - below
the threshold the verdict is literally ``'no measurable change'``, and an
unknown stance yields ``'MDC requires a known stance'`` instead of a threshold.

Sources are the numbered references of the norms report; every entry carries
its own ``source_url`` and ``year`` so a report can cite what it compared
against.  This is a research aid for qualified staff, never a diagnosis and
never a device that decides return to play.
"""
import math

__all__ = ['CAVEATS', 'MEASURES', 'NORM_TABLE_VERSION', 'ENTRIES', 'caveats',
           'interpret_change', 'lookup', 'mdc_for', 'select_entry']

NORM_TABLE_VERSION = 'norms-2026-10-v1'

# Source URLs (numbered as in research/NORMS-AND-NEURO-INTERPRETATION.md).
_URL_BTRACKS = ('https://balancetrackingsystems.com/wp-content/uploads/2020/'
                '06/Goble-and-Baweja-GGI.pdf')                      # [1]
_URL_KISTLER_60S = 'https://pmc.ncbi.nlm.nih.gov/articles/PMC10170532/'  # [3]
_URL_SINGLE_LEG = ('https://www.springermedicine.com/reliability-of-postural-'
                   'sway-measures-during-single-leg-stance-a/51340468')  # [8]
_URL_SPRINGER = 'https://pubmed.ncbi.nlm.nih.gov/19839175/'             # [7]
_URL_WBA = 'https://hal.science/dumas-05490769v1'                       # [21]
_URL_MCTSIB = 'https://pubmed.ncbi.nlm.nih.gov/40559318'                # [10]
_URL_MCTSIB_MDC = ('https://journals.lww.com/jgpt/Abstract/2021/10000/'
                   'Test_Retest_Reliability_and_Minimal_Detectable.3.aspx')  # [11]
_URL_NAKAGAWA = 'https://www.jospt.org/doi/10.2519/jospt.2012.3987'     # [5.1]
_URL_HERRINGTON = 'https://pubmed.ncbi.nlm.nih.gov/24380805/'           # [5.1]


def _row(measure, unit, *, device, source_url, year, condition='',
         age_min=None, age_max=None, sex=None, mean=None, sd=None,
         lo=None, hi=None, transferable=False, note=''):
    """Build one entry with the full, fixed schema (all keys always present)."""
    return {
        'measure': measure,
        'unit': unit,
        'age_min': age_min,
        'age_max': age_max,
        'sex': sex,
        'mean': mean,
        'sd': sd,
        'lo': lo,
        'hi': hi,
        'device': device,
        'condition': condition,
        'source_url': source_url,
        'year': year,
        'transferable': transferable,
        'note': note,
    }


# ---------------------------------------------------------------------------
# 1. Double-leg quiet stance - BTrackS 20 s eyes-closed path length (cm).
#    Goble & Baweja 2018 [1], n=6,280, shoulder-width, 25 Hz.  Means only: the
#    source table publishes no SD.  Device- and duration-specific.
#    Columns: age_min, age_max, male cm, female cm.
# ---------------------------------------------------------------------------
_BTRACKS_BANDS = (
    (20, 29, 25.2, 22.6),
    (30, 39, 24.7, 22.4),
    (40, 49, 27.1, 24.6),
    (50, 59, 32.0, 27.3),
    (60, 69, 39.1, 30.2),
    (70, 79, 45.8, 36.2),
    (80, 120, 54.4, 44.4),   # ">=80" encoded as 80-120
)

_BTRACKS = tuple(
    _row('cop_path_length_20s', 'cm',
         age_min=band[0], age_max=band[1], sex=sex, mean=value,
         device='BTrackS Balance Plate, 25 Hz, shoulder-width stance',
         condition='EC', source_url=_URL_BTRACKS, year=2018,
         transferable=False,
         note=('Mean only - the published table gives no SD. Device- and '
               'duration-specific: a 20 s path length must not be compared '
               'with 30 s or 60 s values (convert to velocity instead).'))
    for band in _BTRACKS_BANDS
    for sex, value in (('male', band[2]), ('female', band[3]))
)

# ---------------------------------------------------------------------------
# 1.2 COP velocity - Kistler 9281C, 5 x 60 s, ages 19-28 (mean 21), n=100.
#     Blaszczyk & Beck 2023 [3].  Resultant "sway vector amplitude";
#     eyes-open is pooled, eyes-closed is published by sex.
# ---------------------------------------------------------------------------
_KISTLER_60S = (
    _row('cop_velocity_60s', 'mm/s', age_min=19, age_max=28, sex=None,
         mean=9.2, sd=1.6,
         device='Kistler 9281C, 40 Hz, 5 x 60 s', condition='EO',
         source_url=_URL_KISTLER_60S, year=2023, transferable=False,
         note=('Resultant sway-vector amplitude for a pooled 50 M / 50 F '
               'sample; 60 s trials on a laboratory plate.')),
    _row('cop_velocity_60s', 'mm/s', age_min=19, age_max=28, sex='female',
         mean=11.2, sd=2.4,
         device='Kistler 9281C, 40 Hz, 5 x 60 s', condition='EC',
         source_url=_URL_KISTLER_60S, year=2023, transferable=False,
         note='Eyes closed, female participants; 60 s trials.'),
    _row('cop_velocity_60s', 'mm/s', age_min=19, age_max=28, sex='male',
         mean=12.4, sd=3.5,
         device='Kistler 9281C, 40 Hz, 5 x 60 s', condition='EC',
         source_url=_URL_KISTLER_60S, year=2023, transferable=False,
         note='Eyes closed, male participants; 60 s trials.'),
)

# ---------------------------------------------------------------------------
# 2.2 Single-leg COP, 30 s eyes open, healthy adults n=48, Kistler 9260AA.
#     Kozinc 2025 [8].  The source gives no age band - encoded as unbanded.
#     Values are the first session; the retest values are in the note.
# ---------------------------------------------------------------------------
_SINGLE_LEG = (
    _row('single_leg_cop_velocity', 'mm/s', mean=35.0, sd=7.9,
         device='Kistler 9260AA, 30 s, 3 trials, healthy adults n=48',
         condition='single_leg_eo', source_url=_URL_SINGLE_LEG, year=2025,
         transferable=False,
         note=('First session of a two-session study (31.9 +/- 8.4 mm/s at '
               'retest). Single-leg velocity is roughly 3-4x double-leg '
               'velocity - never score it against double-leg norms. No '
               'left/right-specific norms and no validated asymmetry cut-off '
               'exist for healthy adults.')),
    _row('single_leg_cop_area', 'mm^2', mean=2958.0, sd=957.0,
         device='Kistler 9260AA, 30 s, 3 trials, healthy adults n=48',
         condition='single_leg_eo', source_url=_URL_SINGLE_LEG, year=2025,
         transferable=False,
         note=('95% confidence ellipse (state the definition whenever area is '
               'reported); 2,599 +/- 983 mm^2 at retest. Weakest reliability '
               'of the single-leg metrics (ICC 0.71) - demote area to a '
               'supporting plot.')),
)

# ---------------------------------------------------------------------------
# 2.1 Unipedal stance hold time, best of 3, n=549 (Springer 2007 [7]).
#     Columns: age_min, age_max, EO female, EO male, EC female, EC male.
#     SDs are large (~9-14 s in young bands); ceiling 45 s, floor >=80 y.
# ---------------------------------------------------------------------------
_SPRINGER_BANDS = (
    (18, 39, 43.5, 43.2, 8.5, 10.2),
    (40, 49, 40.4, 40.1, 7.4, 7.3),
    (50, 59, 36.0, 38.1, 5.0, 4.5),
    (60, 69, 25.1, 28.7, 2.5, 3.1),
    (70, 79, 11.3, 18.3, 2.2, 1.9),
    (80, 120, 7.4, 5.6, 1.4, 1.3),   # "80+" encoded as 80-120
)

_SPRINGER = tuple(
    _row(measure, 's',
         age_min=band[0], age_max=band[1], sex=sex, mean=value,
         device='Stopwatch-timed unipedal stance, best of 3, n=549',
         condition=condition, source_url=_URL_SPRINGER, year=2007,
         transferable=True,
         note=('Device-free timed test, so the values transfer across labs - '
               'but scoring must match: best of 3, ceiling 45 s, floor at '
               '>=80 y, SDs ~9-14 s in young bands (treat as distributions, '
               'not targets). The source reports no sex effect; the values '
               'shown are its published sex-specific means. Camera- or '
               'plate-scored hold times differ from stopwatch timing, so keep '
               'one method across visits.'),
         )
    for band in _SPRINGER_BANDS
    for measure, condition, sex, value in (
        ('single_leg_hold_s_eo', 'EO', 'female', band[2]),
        ('single_leg_hold_s_eo', 'EO', 'male', band[3]),
        ('single_leg_hold_s_ec', 'EC', 'female', band[4]),
        ('single_leg_hold_s_ec', 'EC', 'male', band[5]),
    )
)

# ---------------------------------------------------------------------------
# 5.2 / report section 6 - physiological weight-bearing asymmetry corridor.
#     A body-level proportion (43-57 % of body weight), not a device-derived
#     percentile, so it transfers; it still needs per-limb loading to measure.
# ---------------------------------------------------------------------------
_WBA = (
    _row('weight_bearing_left_pct', '% body weight', lo=43.0, hi=57.0,
         device=('Per-limb vertical force (two plates or insoles); n=40, '
                 'mean 62.6 y'),
         condition='double_leg', source_url=_URL_WBA, year=2025,
         transferable=True,
         note=('Physiological corridor 43-57 % applies to either limb by '
               'symmetry. A single plate yields only the resultant COP, so '
               'this is NOT measurable here; a camera weight-bearing estimate '
               'is not a substitute for per-limb force.')),
)

# ---------------------------------------------------------------------------
# 3. Instrumented mCTSIB control values, sway velocity in deg/s, 30 s.
#     Healthy age-matched controls in a 2025 SBMA study [10], n=26-29 per
#     band.  Order-of-magnitude anchors only: the two bands are nearly
#     identical (contradicting the larger age-gradient literature) and deg/s
#     from a different plate cannot be transferred to mm/s COP.
# ---------------------------------------------------------------------------
_MCTSIB_BANDS = ((40, 59), (60, 69))
_MCTSIB_VALUES = (
    # condition, (40-59 mean, sd), (60-69 mean, sd)
    ('firm_eo', (0.27, 0.12), (0.28, 0.12)),
    ('firm_ec', (0.33, 0.13), (0.31, 0.11)),
    ('foam_eo', (0.63, 0.17), (0.69, 0.15)),
    ('foam_ec', (1.61, 0.45), (1.60, 0.52)),
)

_MCTSIB = tuple(
    _row('mctsib_sway_velocity', 'deg/s',
         age_min=band[0], age_max=band[1], mean=values[0], sd=values[1],
         device='VSR Sport plate, 30 s, sway velocity (deg/s)',
         condition=condition, source_url=_URL_MCTSIB, year=2025,
         transferable=False,
         note=('Order-of-magnitude anchor only (n=26-29 healthy, possibly '
               'active adults; the two bands are nearly identical). Angular '
               'deg/s from a different plate must not be compared with this '
               'plate in mm/s. Conditions 3-4 require a foam pad. Never apply '
               'adult values to children.'))
    for condition, first, second in _MCTSIB_VALUES
    for band, values in zip(_MCTSIB_BANDS, (first, second))
)

# ---------------------------------------------------------------------------
# 5.1 Context values for camera kinematics.  These are GROUP MEANS from
#     case-control single-leg squat / landing studies, not normative bands, and
#     the task is not quiet stance - hence transferable=False.  They exist so a
#     report can say a published comparator exists without ever applying it as
#     a reference band.
# ---------------------------------------------------------------------------
_CAMERA_CONTEXT = (
    _row('trunk_lean_deg_sls_squat', 'deg', mean=6.7, sd=3.0,
         device='3-D motion analysis, case-control (asymptomatic adults)',
         condition='sls_squat', source_url=_URL_NAKAGAWA, year=2012,
         transferable=False,
         note=('Group mean during a single-leg squat (patellofemoral-pain '
               'group 9.3 +/- 5.3 deg), not quiet stance; no normative '
               'percentiles. Never use as a band for camera trunk lean.')),
    _row('contralateral_pelvic_drop_deg', 'deg', mean=7.4, sd=3.8,
         device='3-D motion analysis, case-control (asymptomatic adults)',
         condition='sls_squat', source_url=_URL_NAKAGAWA, year=2012,
         transferable=False,
         note=('Contralateral pelvic drop during a single-leg squat (pain '
               'group 10.3 +/- 4.7 deg); a group mean, not a stance norm.')),
    _row('knee_valgus_deg_fppa', 'deg', mean=8.4, sd=5.1,
         device='2-D frontal-plane projection angle, single-leg squat',
         condition='sls_squat', source_url=_URL_HERRINGTON, year=2014,
         transferable=False,
         note=('2-D FPPA SEM 2.7-3.0 deg, smallest detectable difference '
               '7.5-8.9 deg. Absolute frontal-plane knee valgus from a single '
               'camera is not valid (bias about -19 deg), and this lane '
               'computes no valgus angle at all - not even a change score - '
               'so this entry is context only, never a comparison.')),
)

ENTRIES = (_BTRACKS + _KISTLER_60S + _SINGLE_LEG + _SPRINGER + _WBA
           + _MCTSIB + _CAMERA_CONTEXT)

#: Every measure key present in the table (sorted).
MEASURES = tuple(sorted({entry['measure'] for entry in ENTRIES}))

# ---------------------------------------------------------------------------
# MDC table.  mode 'percent_of_mean' -> threshold = value * |baseline|;
# mode 'absolute' -> threshold = value in the measure's unit.  'range' is the
# range the source published, when it published one.
#
# Stance-dependent COP rules live in _MDC_BY_STANCE: the published constants
# were all derived in single-leg stance, so a double-leg trial gets no rule and
# an unknown stance is refused rather than defaulted to single-leg.
# ---------------------------------------------------------------------------
_STANCE_ALIASES = {
    'single_leg': 'single_leg', 'single-leg': 'single_leg',
    'single leg': 'single_leg', 'singleleg': 'single_leg',
    'sls': 'single_leg', 'unipedal': 'single_leg', 'one_leg': 'single_leg',
    'one-leg': 'single_leg', 'one leg': 'single_leg',
    'double_leg': 'double_leg', 'double-leg': 'double_leg',
    'double leg': 'double_leg', 'doubleleg': 'double_leg',
    'bipedal': 'double_leg', 'both': 'double_leg', 'two_leg': 'double_leg',
    'two-leg': 'double_leg', 'feet_together': 'double_leg',
}

_MDC_BY_STANCE = {
    'cop_velocity': {
        'single_leg': {
            'mode': 'absolute',
            'value': 9.0,
            'unit': 'mm/s',
            'range': (6.3, 9.7),
            'percent_of_mean': 0.30,
            'percent_range': (0.28, 0.31),
            'provisional': True,
            'source_url': _URL_SINGLE_LEG,
            'basis': (
                'Single-leg mean COP velocity. Preferred threshold: the '
                'absolute 9.0 mm/s conservative upper value in '
                'metrics.MDC_TABLE (Vindis 2026: MDC95 ~6.3-9.7 mm/s across '
                'single-leg conditions; Kozinc 2025 30 s single-leg velocity '
                'agrees). The percentage form (28-31 % of baseline, i.e. '
                '~9.8-10.9 mm/s at the 35.0 mm/s single-leg reference) is '
                'carried as a cross-check only and is NEVER applied as the '
                'threshold. Literature constant from other labs - a local '
                'test-retest study is required before clinical use.'),
        },
        'double_leg': None,
    },
    'cop_area': {
        'single_leg': {
            'mode': 'percent_of_mean',
            'value': 0.50,
            'range': (0.50, 0.50),
            'provisional': True,
            'source_url': _URL_SINGLE_LEG,
            'basis': (
                'Single-leg 95 % ellipse area: MDC 1,466.7 mm^2 at '
                '2,958 mm^2 mean (~50 %); weakest reliability of the '
                'single-leg metrics (ICC 0.71). Single-leg only - no '
                'double-leg area MDC is published for this plate.'),
        },
        'double_leg': None,
    },
}

_MDC = {
    'mctsib_sway_velocity_firm': {
        'mode': 'absolute',
        'value': 0.285,
        'range': (0.140, 0.285),
        'provisional': True,
        'source_url': _URL_MCTSIB_MDC,
        'basis': ('i-mCTSIB MDC90 0.140-0.285 deg/s, firm conditions (VSR '
                  'Sport plate, n=20 adults 65-85 y). Upper bound used so a '
                  'small change is never called real. Not transferable to '
                  'this plate.'),
    },
    'mctsib_sway_velocity_firm_ec': {
        'mode': 'absolute',
        'value': 0.285,
        'range': (0.140, 0.285),
        'provisional': True,
        'source_url': _URL_MCTSIB_MDC,
        'basis': ('i-mCTSIB MDC90 0.140-0.285 deg/s, firm eyes-closed '
                  '(VSR Sport plate, n=20 adults 65-85 y). Upper bound used; '
                  'not transferable to this plate.'),
    },
    'mctsib_sway_velocity_foam_eo': {
        'mode': 'absolute',
        'value': 0.285,
        'range': (0.140, 0.285),
        'provisional': True,
        'source_url': _URL_MCTSIB_MDC,
        'basis': ('i-mCTSIB MDC90 0.140-0.285 deg/s, foam eyes-open (VSR '
                  'Sport plate, n=20 adults 65-85 y). Upper bound used; not '
                  'transferable to this plate.'),
    },
    'mctsib_sway_velocity_foam_ec': {
        'mode': 'absolute',
        'value': 1.12,
        'range': (1.12, 1.12),
        'provisional': True,
        'source_url': _URL_MCTSIB_MDC,
        'basis': ('i-mCTSIB MDC90 1.12 deg/s, foam eyes-closed - the hardest '
                  'condition is 4-8x noisier, so small foam-EC changes are '
                  'uninterpretable. Not transferable to this plate.'),
    },
}

# ---------------------------------------------------------------------------
# The report's explicit "do not claim" list (norms report section 10), as data.
# Phrasing deliberately avoids the words "normal"/"abnormal": the report layer
# must never emit them, and these strings are rendered into that report.
# ---------------------------------------------------------------------------
CAVEATS = (
    'Do not claim a diagnosis of vestibular loss, neuropathy, cerebellar '
    'disease, Parkinsonism, concussion or anything else from plate or video '
    'data.',
    'Do not claim fall risk for an individual from any published cut-off - '
    'they are group-level discriminators.',
    'Do not clear - or refuse to clear - an athlete for return to play.',
    'Do not transfer a percentile across devices: Bertec, NeuroCom, BTrackS, '
    'Zebris, Leonardo and Wii Board are not interchangeable, and Bertec\'s own '
    'CDP norms differ from NeuroCom on SOT conditions 1, 4 and 6 and on the '
    'Adaptation Test.',
    'Do not compare path lengths recorded over different durations without '
    'converting to velocity.',
    'Do not treat sway area as a stability index on its own; area and '
    'velocity dissociate.',
    'Do not assume sway rises monotonically with age - area is U-shaped '
    'across the lifespan.',
    'Do not apply adult mCTSIB values to children, or paediatric values to '
    'adults.',
    'Do not report a Romberg quotient computed across different target '
    'distances or stance widths.',
    'Do not claim left/right single-leg norms exist for healthy adults - they '
    'do not; report each limb, the difference and the ratio.',
    'Do not present a change smaller than the MDC as improvement or decline.',
    'Do not describe a wide-based stance as better balance - widening can hold '
    'the COP steady while masking instability.',
    'Do not treat "within reference limits" as a synonym for "no disease".',
    'Do not silently mix 90 % and 95 % ellipse definitions - state which one '
    'was computed.',
)

_SEX_ALIASES = {
    'female': 'female', 'f': 'female', 'woman': 'female', 'women': 'female',
    'girl': 'female', 'girls': 'female',
    'male': 'male', 'm': 'male', 'man': 'male', 'men': 'male',
    'boy': 'male', 'boys': 'male',
}


def _finite(value):
    """Coerce to a finite float, else None (never raises)."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _norm_sex(sex):
    """Normalise a sex label to 'female'/'male'/None (unknown -> None)."""
    if sex is None:
        return None
    return _SEX_ALIASES.get(str(sex).strip().lower())


def _age_matches(entry, age):
    """True when ``age`` falls inside the entry's band (or it is unbanded)."""
    low = entry['age_min']
    high = entry['age_max']
    if low is None and high is None:
        return True                      # unbanded: the source made no claim
    if low is None or high is None:
        return False                     # half-open bands are not encoded
    if age is None:
        return False                     # banded entry + unknown age: no match
    return low <= age <= high


def select_entry(candidates, age=None, sex=None):
    """Pick the narrowest entry matching ``age``/``sex`` from ``candidates``.

    Ranking, in order: sex-specific before pooled; banded before unbanded;
    narrower band before wider; table order on a tie.  Returns a copy of the
    entry, or ``None`` when nothing matches.  Never interpolates.
    """
    wanted = _norm_sex(sex)
    age_value = _finite(age)
    best = None
    best_key = None
    for index, entry in enumerate(candidates):
        entry_sex = entry.get('sex')
        if entry_sex is not None and entry_sex != wanted:
            continue
        if not _age_matches(entry, age_value):
            continue
        banded = 0 if (entry.get('age_min') is not None
                       and entry.get('age_max') is not None) else 1
        width = (entry['age_max'] - entry['age_min']) if not banded else 0.0
        sex_rank = 0 if (entry_sex is not None and entry_sex == wanted) else 1
        key = (sex_rank, banded, width, index)
        if best_key is None or key < best_key:
            best_key = key
            best = entry
    return dict(best) if best is not None else None


def lookup(measure, age=None, sex=None):
    """Narrowest published entry for ``measure`` at ``age``/``sex``, or None.

    Exact measure key only; no interpolation across bands and no unit
    conversion.  ``None`` means "no published reference for this measure and
    band" - a report must then print the raw value with no comparison.
    """
    if not isinstance(measure, str):
        return None
    candidates = [entry for entry in ENTRIES if entry['measure'] == measure]
    return select_entry(candidates, age=age, sex=sex)


def _family(measure):
    """Map a COP measure name onto an MDC family key, or None.

    Deliberately narrow: only COP velocity and COP area share published
    thresholds.  Anything else (path length, range, frequency, angular mCTSIB
    velocity without a condition) returns None rather than borrowing a rule
    that was not derived for it.  Every family returned here is
    stance-dependent - see :func:`mdc_for`.
    """
    lowered = measure.lower()
    if 'mctsib' in lowered:
        return None
    if 'cop' not in lowered:
        return None
    if 'velocity' in lowered:
        return 'cop_velocity'
    if 'area' in lowered:
        return 'cop_area'
    return None


def _normalise_stance(stance):
    """'single_leg' / 'double_leg' from any documented spelling, else None."""
    if stance is None:
        return None
    return _STANCE_ALIASES.get(str(stance).strip().lower())


def mdc_for(measure, stance=None):
    """Provisional minimal-detectable-change rule for ``measure``, or None.

    Keyed by measure AND stance.  Returns ``{'mode', 'value', 'provisional',
    'source_url'}`` plus ``'measure'`` (the queried key), ``'stance'`` (the
    normalised stance, or None for a condition-named ``mctsib_*`` rule),
    ``'basis'`` (provenance prose) and ``'range'`` (the published range, when
    there is one).  ``percent_of_mean`` thresholds scale with the baseline;
    ``absolute`` ones are in the measure's unit.

    A stance-dependent COP measure with an unknown/missing stance returns
    ``None`` - the single-leg constants are never applied by default, and a
    double-leg trial gets ``None`` because no double-leg MDC is published for
    this plate.  Every returned rule is provisional (literature constants from
    other labs and devices), so a change is never called real on their
    strength alone.
    """
    if not isinstance(measure, str):
        return None
    rule = _MDC.get(measure)
    normalised = None
    if rule is None:
        family = _family(measure)
        if family is None:
            return None
        normalised = _normalise_stance(stance)
        if normalised is None:
            return None
        rule = _MDC_BY_STANCE.get(family, {}).get(normalised)
    if rule is None:
        return None
    payload = dict(rule)
    payload['measure'] = measure
    payload['stance'] = normalised
    return payload


def interpret_change(measure, baseline, followup, stance=None):
    """Compare two visits against the provisional MDC - never calls a winner.

    Returns ``{'delta', 'mdc', 'verdict', 'provisional'}``.  ``verdict`` is
    ``'no measurable change'`` whenever ``|delta|`` does not EXCEED the
    threshold (equality included), otherwise ``'change exceeds provisional
    MDC'``.  There is deliberately no direction in the verdict, so a change
    below the MDC can never be reported as improvement.

    ``stance`` is required for a stance-dependent COP measure: without it the
    verdict is ``'MDC requires a known stance'`` rather than a borrowed
    single-leg threshold.  When no MDC is published for the measure (or for
    that stance) the verdict says so instead of guessing.
    """
    base = _finite(baseline)
    follow = _finite(followup)
    result = {'delta': None, 'mdc': None,
              'verdict': 'not assessable (missing or non-finite value)',
              'provisional': True}
    if base is None or follow is None:
        return result
    delta = follow - base
    result['delta'] = delta
    rule = mdc_for(measure, stance=stance)
    if rule is None:
        family = _family(measure) if isinstance(measure, str) else None
        if family is not None and _normalise_stance(stance) is None:
            result['verdict'] = 'MDC requires a known stance'
        elif family is not None:
            result['verdict'] = ('no published MDC for this stance; '
                                 'descriptive only')
        else:
            result['verdict'] = 'no published MDC; descriptive only'
        return result
    if rule['mode'] == 'percent_of_mean':
        if base == 0.0:
            result['verdict'] = 'not assessable (zero baseline)'
            return result
        threshold = rule['value'] * abs(base)
    else:
        threshold = rule['value']
    result['mdc'] = threshold
    result['verdict'] = ('change exceeds provisional MDC'
                         if abs(delta) > threshold else 'no measurable change')
    return result


def caveats():
    """The explicit "do not claim" list, as a tuple of strings."""
    return CAVEATS
