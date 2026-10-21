"""Local, dependency-free center-of-pressure (COP) and postural-sway metrics.

Methodology and processing standard (project roadmap Phase 1): feed EVERY
hardware sample with hardware time in seconds; convert force/moments to COP
per sample; low-pass at 10 Hz with a zero-phase filter; decimate to 100 Hz
for path-based metrics; store both the raw 1000 Hz and the 100 Hz series.
Acquisition guidance lives in the protocol layer (Ruhe 2010, PMID 20947353;
Scoppa 2013, PMID 22889928; Quijoux 2021, PMC8623280: >=25 s per trial,
30-60 s preferred, same time-of-day and settings across visits): filter and
acquisition settings dominate between-session comparability more than the
metric formulas themselves.

COP convention (Bertec Device Interface Library 80P-0165: 'Center of Pressure
is computed as Moments divided by Force (ex: Mx / Fz)'; moments N.m, force N):
COPx = -My / Fz and COPy = +Mx / Fz, returned in millimetres (m * 1000).
The x sign is a keyword because empirical plate-orientation verification
(roadmap Phase 0: four-quadrant known loads) is still pending - callers must
not hard-code either sign. |Fz| below min_force_n (default 20 N, matching
analysis.py's unloaded threshold) returns (None, None): COP is undefined on an
unloaded/airborne plate and the division is never taken near zero. Signed
forces are never abs()'d for the division itself; the magnitude gate only
decides whether a COP value exists. The gate - not the internal 1e-9 N
epsilon, which only prevents an exact-zero division - is the load-bearing
guard: a caller who deliberately lowers min_force_n toward zero gets
arithmetically correct but physically absurd values (Fz = 1e-8 N with a
1 N.m moment is 1e11 mm of COP), so pass max_abs_cop_mm to bound the output -
coordinates strictly beyond that plausibility bound return (None, None)
instead of entering a metric (the service keeps its 20 N gate as primary).

95% confidence ellipse (Quijoux et al. 2021, PMC8623280): area =
F95(2, n-2) * 2 * pi * sqrt(lambda1 * lambda2) over the (n-1) sample
covariance of the COP coordinates in mm^2, where lambda1, lambda2 are its
eigenvalues and f95_2_df2(n) is the exact closed form of the F(2, n-2) 95%
quantile derived from P(F <= x) = 1 - (1 + 2x/v)^(-v/2) with v = n - 2. This
is an elliptical summary of COP scatter (a research estimate of sway extent),
not a diagnostic region and not a fall-risk classifier.

Change flags: MDC_TABLE holds literature minimal-detectable-change values
(examples only, each cited inline). ratio helpers (Romberg EC/EO, foam/firm)
and the asymmetry index (Nolff et al. 2023: flag >= 16% on single-leg total
path length) are screening aids for qualified staff, never diagnoses.

Everything here is pure-stdlib math on caller-supplied samples: no hardware,
no SDK, no I/O, no retention. Synthetic data belongs in tests only. These are
research estimates, not a clinical instrument; diagnosis and return-to-play
decisions remain physician-led (Amsterdam 2022 / SCAT5 consensus).
"""
import math

__all__ = [
    'ASI_FLAG_PCT', 'MDC_TABLE', 'PROCESSING_VERSION', 'asymmetry_index_pct',
    'axis_path_lengths_mm', 'change_exceeds_mdc', 'cop_from_fz_mx_my',
    'decimate_mean', 'ellipse95_area_mm2', 'f95_2_df2', 'foam_ratio',
    'lowpass_zero_phase', 'romberg_ratio', 'trial_metrics',
]

_SQRT2 = math.sqrt(2.0)
# Magnitude floor for the COP division: even a caller-supplied min_force_n
# of 0 must not produce a division by an exact zero (never divide near zero).
# L7: this epsilon ONLY blocks an exact-zero divide - it is not a
# plausibility guard. With a lowered min_force_n the gate admits tiny |Fz|,
# so the mm output can be arbitrarily large (1e-8 N x 1 N.m -> 1e11 mm);
# callers doing that must pass cop_from_fz_mx_my(..., max_abs_cop_mm=<bound>)
# (see the function docstring for the exact semantics).
_FORCE_EPSILON_N = 1e-9
_MIN_TRIAL_S = 2.0
_MIN_ELLIPSE_SAMPLES = 3

# Filter/decimation settings version; stamp every stored trial with it so a
# stored value is traceable to the processing that produced it - settings
# dominate between-session comparability (Quijoux 2021, PMC8623280), so a
# version bump (not a silent settings change) is what makes two sessions
# comparable. v1 = a 10 Hz zero-phase low-pass followed by decimation to
# 100 Hz for path-based metrics.
PROCESSING_VERSION = 'lp10-dc100-v1'


def _finite(value):
    """Return float(value) when it is finite, else None (never raises)."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _finite_samples(samples, label):
    """Coerce a sequence to a list of finite floats or raise ValueError.

    None/NaN/inf are rejected loudly instead of silently poisoning a filter
    state or a path-length sum; callers must handle gaps before filtering
    (invalid COP samples are represented as None and are handled by
    trial_metrics, not by the filter).
    """
    try:
        values = [float(sample) for sample in samples]
    except (TypeError, ValueError, OverflowError):
        raise ValueError(label + ' must be a sequence of finite numbers')
    for value in values:
        if not math.isfinite(value):
            raise ValueError(label + ' must all be finite; '
                             'handle gaps before filtering')
    return values


def cop_from_fz_mx_my(fz_n, mx_nm, my_nm, *, copx_sign=-1.0, copy_sign=1.0,
                      min_force_n=20.0, max_abs_cop_mm=None):
    """Bertec COP from vertical force and moments, in millimetres.

    COPx = copx_sign * My / Fz, COPy = copy_sign * Mx / Fz, scaled by 1000 to
    mm. Defaults follow the Bertec Device Interface Library 80P-0165 example
    (COPx = -My/Fz, COPy = +Mx/Fz); the signs stay configurable until Phase 0
    empirically verifies plate orientation with four-quadrant known loads.

    Returns (None, None) when any input is nonfinite/non-numeric or when
    |Fz| < min_force_n (default 20 N). The signed force is never abs()'d for
    the division; the magnitude gate only decides validity, so a near-zero
    denominator can never be divided by.

    L7 caller hazard: the force gate - not the internal 1e-9 N epsilon, which
    only prevents an exact-zero division - is the load-bearing guard. A caller
    who deliberately lowers min_force_n toward zero can therefore get
    arithmetically correct but physically absurd output (Fz = 1e-8 N with a
    1 N.m moment is 1e11 mm of COP, with no upper plausibility clamp).
    max_abs_cop_mm is that optional clamp: when it is a finite number, any
    |COPx| or |COPy| strictly beyond it returns (None, None). Default None
    keeps previous behaviour exactly (the service's 20 N gate remains its
    primary guard); a non-finite max_abs_cop_mm is ignored (treated as unset),
    and a bound <= 0 rejects every non-zero coordinate (only an exact 0.0
    survives a bound of 0) - both are caller errors, never exceptions: this
    function is total and never raises.
    """
    fz = _finite(fz_n)
    mx = _finite(mx_nm)
    my = _finite(my_nm)
    sign_x = _finite(copx_sign)
    sign_y = _finite(copy_sign)
    threshold = _finite(min_force_n)
    bound = None if max_abs_cop_mm is None else _finite(max_abs_cop_mm)
    if None in (fz, mx, my, sign_x, sign_y, threshold):
        return None, None
    if abs(fz) < max(threshold, _FORCE_EPSILON_N):
        return None, None
    copx_mm = sign_x * my / fz * 1000.0
    copy_mm = sign_y * mx / fz * 1000.0
    if not (math.isfinite(copx_mm) and math.isfinite(copy_mm)):
        return None, None
    if bound is not None and (abs(copx_mm) > bound or abs(copy_mm) > bound):
        return None, None
    return copx_mm, copy_mm


def _butterworth2_lowpass_coeffs(fs_hz, cutoff_hz):
    """2nd-order Butterworth low-pass biquad, bilinear transform, pre-warped.

    H(s) = 1 / (s^2 + sqrt(2) s + 1) with the bilinear substitution and
    K = tan(pi * fc / fs) pre-warping the digital corner onto the analog
    -3 dB point (standard bilinear design; Oppenheim & Schafer,
    Discrete-Time Signal Processing). Returns (b0, b1, b2, a1, a2) for
    y[n] = b0 x[n] + b1 x[n-1] + b2 x[n-2] - a1 y[n-1] - a2 y[n-2].
    """
    k = math.tan(math.pi * cutoff_hz / fs_hz)
    norm = 1.0 / (1.0 + _SQRT2 * k + k * k)
    b0 = k * k * norm
    return b0, 2.0 * b0, b0, 2.0 * (k * k - 1.0) * norm, \
        (1.0 - _SQRT2 * k + k * k) * norm


def _biquad_pass(values, b0, b1, b2, a1, a2, reverse):
    """One causal biquad pass; delay lines start at steady state (x[0]).

    Steady-state initialisation makes a constant signal pass through exactly
    and removes the start-up transient without padding the samples (the
    odd-reflection padding some toolboxes use is not needed for COP data,
    which begins and ends in quiet standing).
    """
    seq = values[::-1] if reverse else values
    x1 = x2 = y1 = y2 = seq[0]
    out = []
    append = out.append
    for x0 in seq:
        y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        append(y0)
        x2 = x1
        x1 = x0
        y2 = y1
        y1 = y0
    if reverse:
        out.reverse()
    return out


def lowpass_zero_phase(samples, fs_hz, cutoff_hz=10.0, order=2):
    """Zero-phase 10 Hz Butterworth low-pass (forward, then backward).

    One 2nd-order Butterworth biquad (bilinear transform with pre-warping)
    is applied forward and then backward over the whole trial, so phase is
    exactly zero and the composite magnitude is the single-pass magnitude
    SQUARED: effective 4th-order roll-off (-24 dB/octave, asymptotically
    -12 dB/octave per pass), gain 0.5 at cutoff_hz, and the composite -3 dB
    corner at approximately 0.802 * cutoff_hz. Length is preserved exactly.
    Feed hardware-rate samples (1000 Hz), then decimate_mean().

    Raises ValueError for non-positive/nonfinite fs or cutoff, cutoff at or
    above the Nyquist frequency, order != 2 (only the 2nd-order prototype is
    implemented), or any None/nonfinite sample - gaps must be handled by the
    caller, never silently imputed here.
    """
    fs = _finite(fs_hz)
    cutoff = _finite(cutoff_hz)
    if fs is None or fs <= 0.0:
        raise ValueError('fs_hz must be a positive finite number')
    if cutoff is None or cutoff <= 0.0:
        raise ValueError('cutoff_hz must be a positive finite number')
    if cutoff >= fs / 2.0:
        raise ValueError('cutoff_hz must stay below the Nyquist frequency')
    if order != 2:
        raise ValueError('only the 2nd-order Butterworth prototype is '
                         'implemented; only order=2 is supported')
    values = _finite_samples(samples, 'samples')
    if len(values) < 2:
        return values
    coeffs = _butterworth2_lowpass_coeffs(fs, cutoff)
    forward = _biquad_pass(values, *coeffs, reverse=False)
    return _biquad_pass(forward, *coeffs, reverse=True)


def decimate_mean(samples, fs_hz, target_fs=100.0):
    """Block-mean decimation (1000 Hz -> 100 Hz by default).

    Each output sample is the arithmetic mean of one block of fs/target
    input samples (fractional factors are honoured by accumulating block
    boundaries at once per fs/target samples); a trailing partial block is
    averaged into a final output sample so no sample is silently dropped.
    It is a mean-value subsampler, NOT an anti-aliasing filter: it must be
    applied AFTER lowpass_zero_phase (roadmap processing standard: 10 Hz
    zero-phase low-pass, then decimate to 100 Hz for path-based metrics).

    Raises ValueError for non-positive/nonfinite rates, target_fs > fs_hz
    (block-mean decimation cannot upsample), or any None/nonfinite sample.
    """
    fs = _finite(fs_hz)
    target = _finite(target_fs)
    if fs is None or fs <= 0.0:
        raise ValueError('fs_hz must be a positive finite number')
    if target is None or target <= 0.0:
        raise ValueError('target_fs must be a positive finite number')
    if target > fs:
        raise ValueError('target_fs must not exceed fs_hz; block-mean '
                         'decimation cannot upsample')
    values = _finite_samples(samples, 'samples')
    if not values:
        return []
    ratio = fs / target
    out = []
    acc = 0.0
    count = 0
    for index, value in enumerate(values):
        acc += value
        count += 1
        if index + 1 >= (len(out) + 1) * ratio:
            out.append(acc / count)
            acc = 0.0
            count = 0
    if count:
        out.append(acc / count)
    return out


def f95_2_df2(n):
    """Exact 95% quantile of F(2, n-2), the ellipse scaling factor.

    Derivation: P(F <= x) = 1 - (1 + 2x/v)^(-v/2) for F ~ F(2, v); solving
    for probability 0.95 with v = n - 2 gives the closed form
    F = ((n-2)/2) * (20^(2/(n-2)) - 1) (since 0.05^(-2/v) = 20^(2/v)).
    Raises ValueError for nonfinite n or n <= 2 (undefined for <3 samples);
    n is a sample count, so the absurd 2 < n < 2 + 1e-12 corner that would
    overflow the power term returns inf rather than raising.
    """
    count = _finite(n)
    if count is None or count <= 2.0:
        raise ValueError('f95_2_df2 requires a finite n > 2 (at least '
                         'three samples for a 95% ellipse)')
    df2 = count - 2.0
    try:
        return (df2 / 2.0) * (20.0 ** (2.0 / df2) - 1.0)
    except OverflowError:
        return math.inf


def _ellipse95_from_moments(var_x, var_y, cov_xy, n):
    """(area_mm2, angle_deg) of the 95% ellipse from sample moments.

    Angle is the first principal axis (larger eigenvalue) of the covariance
    [var_x, cov_xy; cov_xy, var_y], counter-clockwise in degrees from +COPx
    (ML) towards +COPy (AP), reported in (-90, +90] because an axis is
    unoriented. angle = 0.5 * atan2(2*cov_xy, var_x - var_y) is the standard
    principal-axis solution; the determinant clamp only absorbs floating
    point noise (a sample covariance matrix is positive semi-definite).
    """
    scale = f95_2_df2(n)
    det = var_x * var_y - cov_xy * cov_xy
    if det < 0.0:
        det = 0.0
    area = scale * 2.0 * math.pi * math.sqrt(det)
    angle = math.degrees(0.5 * math.atan2(2.0 * cov_xy, var_x - var_y))
    if angle > 90.0:
        angle -= 180.0
    elif angle <= -90.0:
        angle += 180.0
    return area, angle


def ellipse95_area_mm2(xs, ys):
    """95% confidence ellipse area in mm^2 of paired COP coordinates.

    '95% confidence ellipse area; formula per Quijoux et al. 2021
    (PMC8623280) using the F-distribution scaling':
    area = f95_2_df2(n) * 2 * pi * sqrt(lambda1 * lambda2), where the
    lambdas are the eigenvalues of the (n-1) sample covariance matrix
    [varX, covXY; covXY, varY] in mm^2, so sqrt(lambda1*lambda2) =
    sqrt(det) = sqrt(varX*varY - covXY^2). Raises ValueError for nonfinite
    inputs, unequal lengths, or fewer than three points (f95 is undefined
    for n <= 2).
    """
    x = _finite_samples(xs, 'xs')
    y = _finite_samples(ys, 'ys')
    if len(x) != len(y):
        raise ValueError('xs and ys must be equal length')
    n = len(x)
    if n < _MIN_ELLIPSE_SAMPLES:
        raise ValueError('at least three samples are required for a 95% '
                         'confidence ellipse')
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    var_x = sum((value - mean_x) ** 2 for value in x) / (n - 1)
    var_y = sum((value - mean_y) ** 2 for value in y) / (n - 1)
    cov_xy = sum((a - mean_x) * (b - mean_y) for a, b in zip(x, y)) / (n - 1)
    area, _ = _ellipse95_from_moments(var_x, var_y, cov_xy, n)
    return area


def _ratio(condition_value, reference_value):
    """condition/reference, or None for None/nonfinite inputs or a zero
    reference (a ratio to zero is undefined, not inf)."""
    condition = _finite(condition_value)
    reference = _finite(reference_value)
    if condition is None or reference is None or reference == 0.0:
        return None
    result = condition / reference
    return result if math.isfinite(result) else None


def romberg_ratio(condition_value, reference_value):
    """Romberg quotient: eyes-closed sway / eyes-open sway.

    Background (project use-case report, vestibular section): healthy
    quiet-stance EC/EO ratios are approximately 2-2.5, and <= 1 ('postural
    blindness') hints that vision is not being used to support posture.
    Returns None for None/nonfinite inputs or a zero eyes-open reference.
    Compare like metrics with like (same metric, protocol, duration).
    """
    return _ratio(condition_value, reference_value)


def foam_ratio(condition_value, firm_value):
    """Foam/firm sway ratio for the same eyes condition (e.g. foam-EC over
    firm-EC).

    This is the refined vestibular ratio used to separate peripheral
    vestibulopathy from somatosensory ataxia where the classic Romberg
    quotient cannot (Anagnostou 2025, PMC12248627). Returns None for
    None/nonfinite inputs or a zero firm-surface denominator.
    """
    return _ratio(condition_value, firm_value)


# Absolute symmetry index flag threshold: >= 16% on total path length in
# single-leg stance (Nolff et al. 2023; 10% is the conventional rule of
# thumb quoted in the same literature). A screening flag only.
ASI_FLAG_PCT = 16.0


def asymmetry_index_pct(left, right):
    """Absolute symmetry index in percent: |L - R| / ((L + R) / 2) * 100.

    Inputs are the same non-negative measure per side (path length, mean
    velocity, ...) from a sequential left-then-right protocol, per Nolff et
    al. 2023. Returns None for None/nonfinite inputs, a zero mean (both
    sides zero carries no asymmetry information), or negative inputs
    (path-length-like quantities are non-negative by construction).
    """
    left_value = _finite(left)
    right_value = _finite(right)
    if left_value is None or right_value is None:
        return None
    if left_value < 0.0 or right_value < 0.0:
        return None
    mean = (left_value + right_value) / 2.0
    if mean == 0.0:
        return None
    result = abs(left_value - right_value) / mean * 100.0
    return result if math.isfinite(result) else None


# Minimal detectable change (MDC) examples, keyed by metric name, in the
# metric's own unit. Each entry is a cited literature value for a SPECIFIC
# protocol; do not apply an entry to a different task, sampling rate or
# processing chain. Extend only with a cited source.
MDC_TABLE = {
    # Single-leg mean COP velocity. Vindis 2026 (PMID 42390291) reports
    # MDC95 ~ 6.3-9.7 mm/s across single-leg conditions (see also Kozinc
    # 2025); 9.0 mm/s is the conservative, near-upper-bound value used for
    # change flags here.
    'sl_mean_velocity_mm_s': 9.0,
    # mCTSIB condition 4 (foam, eyes closed - the vestibular probe): COG
    # sway velocity MDC90 ~ 1.12 deg/s; the vestibular condition is the
    # noisiest of the four (ICC 0.762-0.909) and needs careful change-score
    # interpretation (project use-case report, vestibular section).
    'mctsib_foam_ec_sway_velocity_deg_s': 1.12,
}


def change_exceeds_mdc(metric_key, baseline_value, new_value):
    """True when |new - baseline| >= the tabulated MDC for metric_key.

    Equality counts as detected (the MDC is the smallest change considered
    beyond measurement error). Returns None when the key is unknown to
    MDC_TABLE or a value is None/nonfinite - an absent MDC is not evidence
    of no change. Values are compared as-is (no abs() of the inputs beyond
    the change magnitude), so direction of change is preserved for callers.
    """
    mdc = MDC_TABLE.get(metric_key)
    baseline = _finite(baseline_value)
    new = _finite(new_value)
    if mdc is None or baseline is None or new is None:
        return None
    return abs(new - baseline) >= mdc


def axis_path_lengths_mm(copx_mm, copy_mm):
    """Per-axis COP path lengths (mm) from paired COP coordinates.

    AP run-length is summed along the y (COPy) axis and ML along the x (COPx)
    axis: for every pair of consecutive samples inside one contiguous valid
    run, each axis contributes the absolute delta of its own coordinate. So
    ap_path_mm is total anteroposterior travel and ml_path_mm total
    mediolateral travel; both are the orthogonal components of the planar
    path (hypot(ap_path_mm, ml_path_mm) equals the planar length only for
    motion that never reverses on either axis, otherwise planar is larger).

    Gap convention matches trial_metrics' path_length_mm: a sample whose x or
    y is None/NaN/non-numeric is invalid and segments the runs - a pair
    spanning a gap is never bridged, and only contiguous valid deltas are
    added. Sequence length mismatch compares pairwise (zip, shorter wins).

    Processing standard: Quijoux et al. 2021 (PMC8623280) - >=25 s per trial,
    30-60 s preferred, with fixed filter/decimation settings across sessions
    (see PROCESSING_VERSION); settings dominate between-session comparability
    more than the metric formulas do. A research estimate of sway travel per
    axis for qualified staff, not a diagnostic value.

    Returns (ap_path_mm, ml_path_mm), or (None, None) when fewer than two
    samples have both coordinates valid (no valid delta can exist; also
    returned for non-iterable input rather than raising). Two isolated valid
    points separated by a gap therefore return (0.0, 0.0): both are valid,
    but no contiguous pair exists.
    """
    try:
        pairs = list(zip(copx_mm, copy_mm))
    except TypeError:
        return None, None
    ap_path = 0.0
    ml_path = 0.0
    valid_count = 0
    previous_x = previous_y = None
    for raw_x, raw_y in pairs:
        x = _finite(raw_x)
        y = _finite(raw_y)
        if x is None or y is None:
            previous_x = previous_y = None
            continue
        valid_count += 1
        if previous_x is not None:
            ml_path += abs(x - previous_x)
            ap_path += abs(y - previous_y)
        previous_x = x
        previous_y = y
    if valid_count < 2:
        return None, None
    return ap_path, ml_path


def _reject(reason):
    return {'assessable': False, 'reason': reason}


def trial_metrics(t_s, fz_n, copx_mm, copy_mm, *, min_valid_fraction=0.98):
    """Summary COP metrics for one quiet-standing trial.

    Inputs are equal-length sequences over one shared hardware time base:
    t_s (seconds, strictly increasing), fz_n (vertical force; carried for
    API symmetry and shape validation only - COP already encodes the force
    information and samples below the COP force gate must arrive as
    None/NaN COP), copx_mm and copy_mm (None/NaN where invalid).

    Gates (return {'assessable': False, 'reason': ...} instead of numbers):
    empty/mismatched lengths, nonfinite timestamps, non-strictly-increasing
    timestamps, duration < 2 s, more than (1 - min_valid_fraction) of COP
    samples invalid (None or nonfinite), invalid min_valid_fraction, fewer
    than three valid samples (the 95% ellipse needs n > 2), or a nonfinite
    computed result. The 2 s floor only rejects nonsense input; the roadmap
    protocol standard is >=25 s (30-60 s preferred) per trial.

    Returned metrics (AP = COPy, the Mx/Fz axis; ML = COPx):
    duration_s, n_samples, path_length_mm (sum of consecutive-valid-segment
    distances only - a pair spanning an invalid sample is never bridged),
    mean_velocity_mm_s (= path / duration; the most sensitive summary metric,
    Prieto 1996, PMID 9214811), range_ap_mm, range_ml_mm, rms_ap_mm,
    rms_ml_mm (RMS about the mean), mean_copx_mm, mean_copy_mm,
    ellipse95_area_mm2 and ellipse95_angle_deg (Quijoux 2021, PMC8623280;
    see ellipse95_area_mm2 for the formula and angle convention).

    Gap/contiguity semantics (L8, documented and pinned): path_length_mm (and
    mean_velocity_mm_s, which divides it by the full duration) sums
    index-ADJACENT valid deltas only, so each contiguous valid run contributes
    its own travel and no pair ever bridges a gap. Every OTHER summary - the
    means, ranges, RMS and the 95% ellipse (whose moments are computed over
    the concatenated valid samples, identical by construction to
    ellipse95_area_mm2 applied to them) - pools ALL valid samples regardless
    of which run they came from. A gapped trial can therefore understate path
    length relative to a gap-free trial with the same sample cloud while its
    ellipse is exactly the pooled-sample ellipse: treat ellipse / range / RMS
    here as pooled descriptors of the valid samples, not as single-run
    descriptors. Pooled moments are the documented choice, not an accident:
    the service's merged series contains no gaps, pooling matches
    ellipse95_area_mm2's standalone semantics, and the baseline suite pins
    the behaviour (tests/test_metrics.py, TrialMetricsTests,
    test_ellipse_pools_samples_across_gaps_while_path_does_not).

    A research estimate of postural sway for qualified staff, not a clinical
    instrument and not a fall-risk classifier.
    """
    try:
        times = [float(value) for value in t_s]
        force = [float(value) for value in fz_n]
        copx = list(copx_mm)
        copy = list(copy_mm)
    except (TypeError, ValueError, OverflowError):
        return _reject('nonnumeric_input')
    if not (len(times) == len(force) == len(copx) == len(copy)):
        return _reject('length_mismatch')
    if len(times) < 2:
        return _reject('too_short')
    if any(not math.isfinite(value) for value in times):
        return _reject('nonfinite_timestamp')
    if any(times[index + 1] <= times[index]
           for index in range(len(times) - 1)):
        return _reject('nonmonotonic_timestamps')
    duration_s = times[-1] - times[0]
    if duration_s < _MIN_TRIAL_S - 1e-9:
        return _reject('too_short')
    fraction = _finite(min_valid_fraction)
    if fraction is None or not 0.0 < fraction <= 1.0:
        return _reject('invalid_min_valid_fraction')
    valid = []
    for index in range(len(times)):
        x = _finite(copx[index])
        y = _finite(copy[index])
        if x is not None and y is not None:
            valid.append(index)
    invalid_fraction = 1.0 - len(valid) / len(times)
    if invalid_fraction > (1.0 - fraction) + 1e-12:
        return _reject('too_many_invalid_cop')
    if len(valid) < _MIN_ELLIPSE_SAMPLES:
        return _reject('insufficient_valid_samples')
    xs = [float(copx[index]) for index in valid]
    ys = [float(copy[index]) for index in valid]
    path_length = 0.0
    for index in range(len(times) - 1):
        x0 = _finite(copx[index])
        y0 = _finite(copy[index])
        x1 = _finite(copx[index + 1])
        y1 = _finite(copy[index + 1])
        if None not in (x0, y0, x1, y1):
            path_length += math.hypot(x1 - x0, y1 - y0)
    n = float(len(xs))
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    var_x = sum((value - mean_x) ** 2 for value in xs) / (n - 1.0)
    var_y = sum((value - mean_y) ** 2 for value in ys) / (n - 1.0)
    cov_xy = sum((x - mean_x) * (y - mean_y)
                 for x, y in zip(xs, ys)) / (n - 1.0)
    area, angle = _ellipse95_from_moments(var_x, var_y, cov_xy, n)
    result = {
        'assessable': True,
        'duration_s': duration_s,
        'n_samples': len(times),
        'path_length_mm': path_length,
        'mean_velocity_mm_s': path_length / duration_s,
        'range_ap_mm': max(ys) - min(ys),
        'range_ml_mm': max(xs) - min(xs),
        'rms_ap_mm': math.sqrt(sum((value - mean_y) ** 2
                                   for value in ys) / n),
        'rms_ml_mm': math.sqrt(sum((value - mean_x) ** 2
                                   for value in xs) / n),
        'mean_copx_mm': mean_x,
        'mean_copy_mm': mean_y,
        'ellipse95_area_mm2': area,
        'ellipse95_angle_deg': angle,
    }
    for key, value in result.items():
        if key != 'assessable' and not math.isfinite(value):
            return _reject('nonfinite_result')
    return result
