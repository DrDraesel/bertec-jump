"""Plate-only COP-COM estimate by gravity-line projection (low-pass COP).

What this computes
------------------
The centre of mass (COM) is the variable the postural controller actually
keeps over the base of support; the centre of pressure (COP) is the control
variable that moves underneath it (Winter 1995, Gait Posture 3:193-214,
doi:10.1016/0966-6362(96)82849-9; Winter et al. 1998, J Neurophysiol
80:1211-1221).  COP-COM is their horizontal difference: proportional to
horizontal COM acceleration and read as the *error signal* / control effort of
standing balance rather than its outcome.  Two athletes with identical COM
sway separate here: one swaying passively with large COP-COM excursions (high
effort, low stiffness), one stiffly holding position.

This module estimates COM from the plate alone, as the zero-phase low-pass
filtered COP on each horizontal axis, and reports the difference:

    COPf(t)     = lowpass(COP(t), COP_CUTOFF_HZ=10 Hz)   # plate-noise floor
    COM(t)      ~= lowpass(COPf(t), cutoff_hz=0.5 Hz)    # postural COM proxy
    COP-COM(t)  =  COPf(t) - COM(t)                      # 0.5-10 Hz residual

Both branches come from ONE series through the SAME zero-phase biquad
implementation, and the subtraction happens only after filtering (H6).  Before
that fix the raw hardware-rate COP (1 kHz) was subtracted from a 0.5 Hz
filtered COM, so the residual carried all plate noise above 0.5 Hz and its
reported velocity grew with the sampling rate.  research/VISION-BALANCE-
RESEARCH.md:127 is explicit: COP and COM must be filtered identically before
subtraction, or the COP-COM signal is mostly processing artefact.  Here the
COP branch is the pipeline's documented 10 Hz low-pass and COM is the
documented 0.5 Hz low-pass *of that same filtered COP* - the chain is stated in
every payload (``chain``) so a reader can reproduce it.

Method provenance and its honest limits
---------------------------------------
* The low-pass-filter (LPF) shortcut is Benda, Riley & Krebs 1994
  (IEEE Trans Rehabil Eng 2:3-10): cutoff 0.4-0.5 Hz, MSE < 0.1 cm in quiet
  standing, degrading as the dynamics increase.
* Lafond, Duarte & Prince 2004 (J Biomech 37:1421-6) compared three COM
  methods head to head across quiet standing, ONE-LEG standing, ankle
  oscillation and ankle+hip oscillation: the kinematic (segmental) method is
  the reference standard; zero-point-to-zero-point double integration
  ("gravity-line projection" proper) is comparable to it; the LPF shortcut is
  SIGNIFICANTLY DIFFERENT from it and fails exactly where this lab's
  interesting tests are - single-leg stance, foam and strong hip strategy.
* This module therefore reports a **low-pass COP-COM estimate**, NOT the
  validated gravity-line projection (GLP).  GLP double integration remains the
  upgrade path; until it exists, no payload string or report heading may call
  this GLP.  ``method`` says so explicitly.
* Absolute COP-COM values depend on the cutoff and on the caller's upstream
  filtering/sampling; Corriveau 2000 (PMID 10638875) reports single-trial
  ICC 0.79 AP / 0.69 ML rising to 0.94 / 0.90 with four trials, and
  sub-millimetre MDCs - so the measure is exquisitely sensitive to
  synchronisation error, filter mismatch and COM-model error.  Treat it as a
  within-athlete, same-settings comparison, never a population percentile.

Filter design (documented passband)
-----------------------------------
``metrics.lowpass_zero_phase`` is reused so this module filters exactly like
the rest of the plate pipeline: ONE 2nd-order Butterworth biquad (bilinear
transform with pre-warping, K = tan(pi*fc/fs)) applied forward and then
backward over the whole trial.  Phase is exactly zero; the composite
magnitude is the single-pass magnitude SQUARED (4th-order roll-off,
-24 dB/octave asymptotically), the two-pass gain is 0.5 at ``cutoff_hz`` and
the composite -3 dB corner is at about 0.802 * cutoff_hz.  On the COM branch
(0.5 Hz) the passband is flat to within 0.2 % below 0.1 Hz (gain 0.9984),
which is why a slow 0.1 Hz COP oscillation passes into COM almost unchanged,
while a 2 Hz component is attenuated to 0.39 % of its input amplitude.  On the
COP branch (10 Hz) the passband is flat to 0.2 % below 2 Hz (gain 0.9984) and
a 20 Hz component is attenuated to 5.9 % of its input amplitude, so
above-band plate noise no longer reaches the residual.

Edge handling: before filtering, the series is extended at each end by odd
reflection (2*x[0]-x[k]) over three filter time constants (3*fs/cutoff
samples, capped at n-1), then trimmed back to the original length.  This is
the standard zero-phase padding and, for a locally odd signal such as a sine
starting at zero, it reproduces the true continuation instead of inventing a
start-up step - without it the first second of a 2 Hz test signal carries a
spurious ~1 mm excursion, the same order as the COP-COM signal itself.  A
constant signal still passes exactly, so a zero-variance trial returns
defined zeros.

Because that extension is only an approximation at the trial ends, the
SUMMARY discards a boundary settle at each end before computing anything:
six filter time constants (6/(2*pi*fc) = 1.9 s at 0.5 Hz), at least
``EDGE_SETTLE_S`` (2 s), and never more than a quarter of the trial.  The
``samples`` field reports how many samples were actually summarised, and
``com_series`` still returns the full-length series for plotting.

Gates (never fabricate)
-----------------------
``summarize`` requires at least ``MIN_SAMPLES`` (100) samples over at least
``MIN_DURATION_S`` (2 s), an equal-length numeric time base that is strictly
increasing, and finite samples on every channel.  A gap is never imputed and a
non-finite sample is never dropped: the payload comes back ``available=False``
with a ``reason`` and every numeric field ``None``.  A constant (zero-variance)
COP is a legitimate input and yields defined zeros, not ``None``.  Nothing
here is a diagnosis; interpretation stays with qualified staff and any
return-to-play decision is physician-led (Amsterdam 2022 consensus).
"""
import math

from metrics import lowpass_zero_phase

__all__ = ['COP_CUTOFF_HZ', 'DEFAULT_CUTOFF_HZ', 'EDGE_SETTLE_S', 'METHOD',
           'MIN_DURATION_S', 'MIN_SAMPLES', 'NOTE', 'com_series', 'cop_series',
           'summarize']

#: COP-branch low-pass cutoff: the pipeline's documented 10 Hz plate-noise
#: filter (``metrics.lowpass_zero_phase`` default), applied to COP BEFORE the
#: 0.5 Hz COM smoothing and before the subtraction (H6).
COP_CUTOFF_HZ = 10.0

#: Default COM LPF cutoff (Benda 1994 uses 0.4-0.5 Hz; the pipeline fixes
#: 0.5 Hz).
DEFAULT_CUTOFF_HZ = 0.5

#: Minimum boundary settle discarded at each end of the trial before
#: summarising (six filter time constants, whichever is longer, capped at a
#: quarter of the trial).  The filter's boundary response is partly padding,
#: not postural control.
EDGE_SETTLE_S = 2.0

#: Minimum samples for a COP-COM summary - below this the filter and the RMS
#: estimates are dominated by edge effects, not by postural control.
MIN_SAMPLES = 100

#: Minimum trial span in seconds (matches the plate pipeline's >= 2 s window).
MIN_DURATION_S = 2.0

#: Honest method label.  NOT GLP: see the module docstring.  Deliberately does
#: not contain "gravity-line projection" so no report can quote this payload as
#: the validated method (M2 in the 2026-10-21 QC review).
METHOD = 'low-pass COP-COM estimate (not validated GLP)'

#: Verbatim contract caveat.  Absolute values depend on the cutoff; the LPF
#: approximation is least reliable where the inverted-pendulum assumption
#: breaks (single-leg stance, foam), so COP metrics are the primary report
#: there.
NOTE = ('absolute values depend on cutoff; least reliable in single-leg '
        'stance \u2014 report COP metrics there')

_EMPTY = {
    'available': False,
    'method': METHOD,
    'cutoff_hz': None,
    'cop_cutoff_hz': COP_CUTOFF_HZ,
    'chain': None,
    'fs_hz': None,
    'samples': 0,
    'cop_com_rms_mm': None,
    'cop_com_velocity_mm_s': None,
    'cop_com_ap_rms_mm': None,
    'com_rms_mm': None,
    'note': NOTE,
}


def _number(value):
    """Coerce to a finite float, else None (never raises)."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _floats(values, label):
    """Coerce a sequence to finite floats or raise ValueError."""
    try:
        series = [float(value) for value in values]
    except (TypeError, ValueError, OverflowError):
        raise ValueError(label + ' must be a sequence of finite numbers')
    if not all(math.isfinite(value) for value in series):
        raise ValueError(label + ' must all be finite; gaps are never imputed')
    return series


def _odd_reflect(values, pad):
    """Odd-reflection extension: 2*x[0]-x[k] before, 2*x[-1]-x[k] after.

    Returned with the pad length actually used so the caller can trim it.
    Odd reflection reproduces a locally odd signal (a sine starting at zero)
    exactly, so the zero-phase pass does not manufacture an onset transient -
    without it the first (and last) second of a trial carries a spurious
    excursion of the order of 1 mm, which is comparable to the COP-COM signal
    itself.
    """
    count = len(values)
    if count < 3 or pad <= 0:
        return list(values), 0
    pad = min(int(pad), count - 1)
    head = [2.0 * values[0] - values[index]
            for index in range(pad, 0, -1)]
    tail = [2.0 * values[-1] - values[count - 1 - index]
            for index in range(1, pad + 1)]
    return head + list(values) + tail, pad


def _zero_phase(values, fs, cutoff):
    """Odd-reflection padded, zero-phase low-pass of one full-length series."""
    # Pad three cutoff periods (3/cutoff seconds; ~19 time constants at
    # 0.5 Hz) on each side, which is far longer than the filter's settle.
    requested = int(round(3.0 * fs / cutoff))
    padded, pad = _odd_reflect(values, requested)
    if pad == 0:
        return lowpass_zero_phase(values, fs, cutoff)
    filtered = lowpass_zero_phase(padded, fs, cutoff)
    return filtered[pad:pad + len(values)]


def cop_series(cop_mm, fs_hz, cutoff_hz=COP_CUTOFF_HZ):
    """Zero-phase 10 Hz low-pass COP on one axis - the COP term of COP-COM.

    The subtraction must use a filtered COP, not the raw hardware-rate series:
    ``summarize`` subtracts this from its 0.5 Hz smooth, so both branches pass
    through the same biquad design and the residual is the documented 0.5-10 Hz
    band instead of broadband plate noise (H6).  Exposed so callers can plot the
    COP branch that was actually subtracted.

    Raises ValueError for non-numeric/non-finite samples, a non-positive or
    non-finite rate, or a cutoff at/above the Nyquist frequency.
    """
    values = _floats(cop_mm, 'cop_mm')
    fs = _number(fs_hz)
    cutoff = _number(cutoff_hz)
    if fs is None or fs <= 0.0:
        raise ValueError('fs_hz must be a positive finite number')
    if cutoff is None or cutoff <= 0.0:
        raise ValueError('cutoff_hz must be a positive finite number')
    if cutoff >= fs / 2.0:
        raise ValueError('cutoff_hz must stay below the Nyquist frequency')
    if len(values) < 3:
        return list(values)
    return _zero_phase(values, fs, cutoff)


def com_series(cop_mm, fs_hz, cutoff_hz=DEFAULT_CUTOFF_HZ):
    """Zero-phase low-pass COP on one axis - the COM estimate, in mm.

    This is the primitive behind :func:`summarize`, exposed so callers (and
    reports) can plot the COM trace without re-deriving the filter.  It uses
    the same 2nd-order Butterworth biquad as the rest of the pipeline, run
    forward then backward (zero phase, magnitude squared), over an
    odd-reflection extension of the series that is trimmed afterwards.  In the
    summary chain its input is :func:`cop_series` output, so COM is the 0.5 Hz
    smooth of the already 10 Hz-filtered COP.  See the module docstring for the
    passband and for why this is an approximation of the gravity-line
    projection rather than the validated double-integration method.

    Raises ValueError for non-numeric/non-finite samples, a non-positive or
    non-finite rate, or a cutoff at/above the Nyquist frequency - refusing
    loudly beats silently producing a differently filtered series.
    """
    values = _floats(cop_mm, 'cop_mm')
    fs = _number(fs_hz)
    cutoff = _number(cutoff_hz)
    if fs is None or fs <= 0.0:
        raise ValueError('fs_hz must be a positive finite number')
    if cutoff is None or cutoff <= 0.0:
        raise ValueError('cutoff_hz must be a positive finite number')
    if cutoff >= fs / 2.0:
        raise ValueError('cutoff_hz must stay below the Nyquist frequency')
    if len(values) < 3:
        return list(values)
    return _zero_phase(values, fs, cutoff)


def _unavailable(payload, reason):
    """Mark a payload unavailable with an explicit, honest reason."""
    payload['available'] = False
    payload['reason'] = reason
    return payload


def summarize(t, copx_mm, copy_mm, fs_hz, cutoff_hz=DEFAULT_CUTOFF_HZ):
    """COP-COM summary for one quiet-standing trial, in millimetres.

    Parameters are equal-length sequences over one shared hardware time base
    (seconds) plus the sampling rate the samples were taken at.  ``copx_mm``
    is the medio-lateral axis and ``copy_mm`` the antero-posterior axis
    (Bertec convention in this repo: COPx = -My/Fz, COPy = +Mx/Fz), matching
    ``metrics.trial_metrics``' ap/ml naming.

    Returns a dict with exactly these keys:

    ``available`` (bool)
        True only when every gate passed and every output is finite.
    ``method``
        ``'low-pass COP-COM estimate (not validated GLP)'`` - the honest label;
        see the module docstring for why this is the LPF approximation and not
        the double-integration GLP.
    ``cutoff_hz`` / ``cop_cutoff_hz`` / ``fs_hz``
        The COM cutoff, the COP branch's 10 Hz pre-filter, and the sampling
        rate, or None when they could not be read.
    ``chain``
        Human-readable processing chain actually applied, including both
        cutoffs, e.g. ``'COP -> LP10.0Hz -> COPf; COM = LP0.5Hz(COPf);
        COP-COM = COPf - COM'``.  None when the payload is unavailable.
    ``samples``
        Number of samples actually summarised: the supplied series minus the
        boundary settle discarded at each end (see the module docstring), 0
        when the series could not be read.
    ``cop_com_rms_mm``
        Resultant RMS of the horizontal COP-COM vector about zero:
        sqrt(mean(dx^2 + dy^2)).  Control-effort magnitude; compares only
        within athlete and settings.
    ``cop_com_velocity_mm_s``
        Mean speed of the COP-COM vector - its path length divided by the
        span actually summarised (the repo's mean-velocity convention, after
        the boundary settle).  Both branches are low-passed before the
        subtraction, so (for fs well above the 10 Hz COP cutoff) this no
        longer scales with the sampling rate; report it as a relative measure.
    ``cop_com_ap_rms_mm``
        RMS of the AP component (copy axis), about zero.
    ``com_rms_mm``
        Resultant RMS of the COM estimate ABOUT ITS MEAN - i.e. the
        low-frequency sway amplitude the controller allowed, in mm.
    ``note``
        The fixed caveat string (cutoff dependence; single-leg stance).

    ``available=False`` payloads carry an extra ``reason`` string and leave
    every numeric field None.  A constant COP is available and returns
    defined zeros.
    """
    payload = dict(_EMPTY)

    fs = _number(fs_hz)
    cutoff = _number(cutoff_hz)
    if fs is not None:
        payload['fs_hz'] = fs
    if cutoff is not None:
        payload['cutoff_hz'] = cutoff

    if t is None or copx_mm is None or copy_mm is None:
        return _unavailable(payload, 'time base and both COP axes are required')

    try:
        times = [float(value) for value in t]
        xs = [float(value) for value in copx_mm]
        ys = [float(value) for value in copy_mm]
    except (TypeError, ValueError, OverflowError):
        return _unavailable(payload, 'time base and COP axes must be numeric '
                                     'sequences')

    payload['samples'] = len(xs)
    if not (len(times) == len(xs) == len(ys)):
        return _unavailable(payload, 't, copx_mm and copy_mm must be equal '
                                     'length')

    if not all(math.isfinite(value) for value in times + xs + ys):
        return _unavailable(payload, 'non-finite time or COP samples; gaps '
                                     'are never imputed')

    n_samples = len(xs)
    if n_samples < MIN_SAMPLES:
        return _unavailable(payload, 'fewer than {} samples'.format(
            MIN_SAMPLES))
    if any(times[index] <= times[index - 1]
           for index in range(1, n_samples)):
        return _unavailable(payload, 'time base must be strictly increasing')

    duration_s = times[-1] - times[0]
    if duration_s < MIN_DURATION_S:
        return _unavailable(payload, 'trial shorter than {:g} s'.format(
            MIN_DURATION_S))

    if fs is None or fs <= 0.0:
        return _unavailable(payload, 'fs_hz must be a positive finite number')
    if cutoff is None or cutoff <= 0.0:
        return _unavailable(payload, 'cutoff_hz must be a positive finite '
                                     'number')
    if cutoff >= fs / 2.0:
        return _unavailable(payload, 'cutoff_hz must stay below the Nyquist '
                                     'frequency')

    try:
        # H6: both branches pass through the SAME zero-phase biquad before the
        # subtraction - COP at the pipeline's documented 10 Hz plate-noise
        # cutoff, COM as the 0.5 Hz smooth of that already-filtered COP.
        copx = cop_series(xs, fs, COP_CUTOFF_HZ)
        copy = cop_series(ys, fs, COP_CUTOFF_HZ)
        comx = com_series(copx, fs, cutoff)
        comy = com_series(copy, fs, cutoff)
    except ValueError as exc:  # pragma: no cover - inputs are pre-validated
        return _unavailable(payload, 'filter rejected the input: '
                                     + str(exc))

    diff_x = [x - cx for x, cx in zip(copx, comx)]
    diff_y = [y - cy for y, cy in zip(copy, comy)]
    payload['chain'] = (
        'COP -> LP{:g}Hz -> COPf; COM = LP{:g}Hz(COPf); '
        'COP-COM = COPf - COM; zero-phase 2nd-order Butterworth (2 pass)'
        .format(COP_CUTOFF_HZ, cutoff))

    # Boundary settle: a zero-phase biquad's response at the ends of a trial is
    # partly the extension, not the data.  Discard six filter time constants
    # (at least 2 s, never more than a quarter of the trial) at each end before
    # summarising, so a start-up or end-of-trial transient cannot masquerade as
    # COP-COM.  The trimmed samples are still filtered (and returned by
    # com_series for plotting); they are simply not summarised.
    settle_s = min(max(EDGE_SETTLE_S, 6.0 / (2.0 * math.pi * cutoff)),
                   duration_s / 4.0)
    edge = int(round(settle_s * fs))
    edge = max(0, min(edge, (n_samples - 2) // 2))
    first = edge
    last = n_samples - edge
    used = last - first
    duration_used = times[last - 1] - times[first]

    mean_comx = sum(comx[first:last]) / used
    mean_comy = sum(comy[first:last]) / used
    cop_com_rms = math.sqrt(
        sum(diff_x[index] ** 2 + diff_y[index] ** 2
            for index in range(first, last)) / used)
    ap_rms = math.sqrt(sum(diff_y[index] ** 2
                           for index in range(first, last)) / used)
    com_rms = math.sqrt(
        sum((comx[index] - mean_comx) ** 2 + (comy[index] - mean_comy) ** 2
            for index in range(first, last)) / used)

    path_mm = 0.0
    for index in range(first + 1, last):
        path_mm += math.hypot(diff_x[index] - diff_x[index - 1],
                              diff_y[index] - diff_y[index - 1])

    outputs = (cop_com_rms, ap_rms, com_rms, path_mm / duration_used)
    if not all(math.isfinite(value) for value in outputs):
        return _unavailable(payload, 'non-finite result; refusing to report '
                                     'a fabricated value')
    # Two stacked biquad passes leave ~1e-15 mm of float noise on a constant
    # input; a value that small is not a measurement.  Snap it to a defined
    # zero so a zero-variance trial keeps reporting zeros, not 5e-15.
    def _snap(value):
        return 0.0 if abs(value) < 1e-9 else value

    cop_com_rms, ap_rms, com_rms, velocity = (_snap(value) for value in outputs)

    payload.update({
        'available': True,
        'samples': used,
        'cop_com_rms_mm': cop_com_rms,
        'cop_com_velocity_mm_s': velocity,
        'cop_com_ap_rms_mm': ap_rms,
        'com_rms_mm': com_rms,
    })
    return payload
