"""Markerless pose geometry for the Bertec vision sidecar - PURE STDLIB.

Every function here consumes MediaPipe's 33-point pose landmarks as plain
Python sequences ``[x, y, visibility]`` in normalised image units: x and y in
[0, 1] with **y growing downward**, visibility in [0, 1].  Nothing in this
module imports numpy, cv2 or mediapipe - the Bertec app runs in a stdlib-only
venv and must be able to import it, and the pose worker imports the very same
file under a mediapipe interpreter so both sides agree on the geometry.

Validated scope (frozen design v1.1) - what these numbers may and may not be
used for:

* trunk and pelvis RELATIVE sway from one camera: usable as a descriptive
  within-session measure;
* sagittal knee/hip/ankle angles: markerless ESTIMATES - label them as such;
* frontal-plane valgus: NOT reportable as an absolute angle;
* foot-lift / step detection: NOT scored from this lane;
* camera COP: NOT derived here (no force data, no plate calibration).

Aspect ratio (H1, QC 2026-10-21)
--------------------------------
MediaPipe normalises x by frame WIDTH and y by frame HEIGHT, so a normalised
coordinate pair is only isotropic on a square frame.  Every angle and tilt in
this module is therefore computed after scaling x by ``aspect = width/height``
(= ``frame_width / frame_height``), which puts both axes in units of frame
height - the same physical scale.  Pass ``width``/``height`` (the worker passes
``frame.shape[1]`` / ``frame.shape[0]``) or an explicit ``aspect`` to
:func:`frame_metrics`; the default 1.0 reproduces the old square-frame
behaviour, which is the only frame shape where an uncorrected angle was ever
right.  On a 16:9 frame an uncorrected 45 deg knee reads 29.33 deg and an
uncorrected 10 deg trunk lean reads 5.66 deg.

Sign conventions are camera-relative, not anatomical.  ``trunk_lean_deg`` is
positive when the trunk tilts toward image right; ``head_tilt``,
``shoulder_tilt`` and ``pelvic_tilt_deg`` are positive when the *second*
(left/right index as named) landmark sits LOWER in the image, i.e. a larger y.
Mirroring the feed (``--mirror``) flips every horizontal and tilt sign.

``summarize_frames`` never invents data: metrics with no finite samples are
omitted rather than reported as 0, and a clip shorter than 10 frames returns
``available: False`` instead of a mean over three samples.
"""

from __future__ import annotations

import math

# MediaPipe Pose (33 landmarks) indices used by this module.
NOSE = 0
L_EAR, R_EAR = 7, 8
L_SHOULDER, R_SHOULDER = 11, 12
L_ELBOW, R_ELBOW = 13, 14
L_WRIST, R_WRIST = 15, 16
L_HIP, R_HIP = 23, 24
L_KNEE, R_KNEE = 25, 26
L_ANKLE, R_ANKLE = 27, 28
L_HEEL, R_HEEL = 29, 30
L_FOOT, R_FOOT = 31, 32

#: Minimum MediaPipe visibility for a landmark to enter an angle.  MediaPipe
#: visibility is a heatmap confidence, not a measured occlusion fraction; 0.5
#: is the conventional cut and keeps half-hidden joints out of a mean.
VIS_GATE = 0.5

#: Landmarks whose visibility is summarised into ``visibility_min``.
_TRACKED = (NOSE, L_EAR, R_EAR,
            L_SHOULDER, R_SHOULDER, L_ELBOW, R_ELBOW,
            L_WRIST, R_WRIST, L_HIP, R_HIP, L_KNEE, R_KNEE,
            L_ANKLE, R_ANKLE, L_HEEL, R_HEEL, L_FOOT, R_FOOT)

#: Joints summarised by :func:`summarize_frames` (sagittal + trunk/pelvis).
SUMMARY_ANGLE_KEYS = ("left_knee", "right_knee", "left_hip", "right_hip",
                      "left_ankle", "right_ankle",
                      "trunk_lean_deg", "pelvic_tilt_deg")

#: Sway baseline length in SECONDS (H10).  Long enough to average out per-frame
#: jitter, short enough to follow a slow weight shift.  The rolling window is
#: sized from the measured frame rate, so 30 fps and 60 fps cameras produce
#: comparable numbers for the same physical sway.  ``SWAY_BASELINE_FRAMES`` is
#: only the fallback when no frame rate can be measured.
SWAY_BASELINE_S = 2.5

#: Legacy fallback window in frames (= SWAY_BASELINE_S at 60 fps).  Used only
#: when a clip carries neither ``fps`` nor timestamps; kept as a module name
#: because v1 consumers import it.
SWAY_BASELINE_FRAMES = 150

#: Frames needed before any sway number is emitted (see :func:`sway_metrics`).
SWAY_MIN_FRAMES = 10

MDC_NOTE = ("Single-camera markerless pose: no published minimal-detectable-"
            "change values apply. Treat every value as a descriptive estimate, "
            "not a clinical MDC or a diagnostic threshold.")


def _finite(value):
    """Return ``value`` as a plain float when finite, else ``None``."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _aspect(value, default=1.0):
    """A usable ``width/height`` ratio, else ``default`` (never raises)."""
    out = _finite(value)
    if out is None or out <= 0.0:
        return float(default)
    return out


def _frame_aspect(width=None, height=None, aspect=None):
    """Resolve the x-scale for one frame: explicit aspect, else width/height.

    ``aspect`` wins when supplied; otherwise ``width``/``height`` (pixels from
    ``frame.shape``) are divided.  When neither is usable the frame is treated
    as square - the documented, v1-compatible assumption.
    """
    if aspect is not None:
        return _aspect(aspect)
    w = _finite(width)
    h = _finite(height)
    if w is not None and h is not None and w > 0.0 and h > 0.0:
        return w / h
    return 1.0


def _xy(point):
    """Return ``(x, y)`` floats for a landmark-like point, else ``None``."""
    if point is None:
        return None
    try:
        x = float(point[0])
        y = float(point[1])
    except (TypeError, ValueError, IndexError):
        return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return (x, y)


def _point(lms, index, min_vis=None):
    """Landmark ``index`` as ``(x, y, visibility)`` or ``None``.

    A missing/short landmark is ``None``.  A landmark given without a third
    element is treated as fully visible (handy for hand-built fixtures); real
    MediaPipe output always carries visibility.
    """
    try:
        raw = lms[index]
    except (IndexError, KeyError, TypeError):
        return None
    if raw is None:
        return None
    try:
        x = float(raw[0])
        y = float(raw[1])
        vis = float(raw[2]) if len(raw) > 2 and raw[2] is not None else 1.0
    except (TypeError, ValueError, IndexError):
        return None
    if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(vis)):
        return None
    if min_vis is not None and vis < float(min_vis):
        return None
    return (x, y, vis)


def visible(lms, index, min_vis=VIS_GATE):
    """True when landmark ``index`` exists and clears the visibility gate."""
    return _point(lms, index, min_vis) is not None


def angle_deg(a, b, c, aspect=1.0):
    """Interior angle at vertex ``b`` for points ``a``-``b``-``c``, in degrees.

    ``aspect`` is the frame's ``width / height``: x is multiplied by it before
    the vectors are taken, because MediaPipe normalises x by width and y by
    height.  Without it (default 1.0) the angle is only physical on a square
    frame - on 16:9 a true 45 deg knee would read 29.33 deg.

    Returns a float in [0, 180], or ``None`` when a point is missing/degenerate
    (zero-length limb segment) or a coordinate is not finite.  No visibility
    gate here - see :func:`joint_angle` for the landmark-array form.
    """
    pa, pb, pc = _xy(a), _xy(b), _xy(c)
    if pa is None or pb is None or pc is None:
        return None
    scale = _aspect(aspect)
    v1 = ((pa[0] - pb[0]) * scale, pa[1] - pb[1])
    v2 = ((pc[0] - pb[0]) * scale, pc[1] - pb[1])
    n1 = math.hypot(*v1)
    n2 = math.hypot(*v2)
    if n1 < 1e-9 or n2 < 1e-9:
        return None
    cos = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
    cos = max(-1.0, min(1.0, cos))
    return _finite(math.degrees(math.acos(cos)))


def joint_angle(lms, a, b, c, min_vis=VIS_GATE, aspect=1.0):
    """Visibility-gated :func:`angle_deg` over a landmark array.

    Returns ``None`` when any of the three landmarks is missing or below
    ``min_vis`` - an angle from an occluded limb is not reported at all.
    ``aspect`` is passed straight through to :func:`angle_deg`.
    """
    pa = _point(lms, a, min_vis)
    pb = _point(lms, b, min_vis)
    pc = _point(lms, c, min_vis)
    if pa is None or pb is None or pc is None:
        return None
    return angle_deg(pa, pb, pc, aspect=aspect)


def _mid(p, q):
    if p is None or q is None:
        return None
    return ((p[0] + q[0]) / 2.0, (p[1] + q[1]) / 2.0)


def _line_tilt_deg(left, right, aspect=1.0):
    """Signed tilt of the ``left``->``right`` line away from horizontal.

    Positive when ``right`` is lower in the image (larger y).  ``None`` when
    either point is missing or the line is vertical - a side view collapses
    left/right onto each other, and the tilt of a near-zero-width line is
    numerical noise, not a measurement.  ``aspect`` scales the horizontal
    component so the result is a physical angle on a non-square frame.
    """
    if left is None or right is None:
        return None
    dx = (right[0] - left[0]) * _aspect(aspect)
    dy = right[1] - left[1]
    if abs(dx) < 1e-6:
        return None
    return _finite(math.degrees(math.atan2(dy, abs(dx))))


def _trunk_lean_deg(mid_shoulder, mid_hip, aspect=1.0):
    """Signed lean of the hip->shoulder vector away from image vertical."""
    if mid_shoulder is None or mid_hip is None:
        return None
    dx = (mid_shoulder[0] - mid_hip[0]) * _aspect(aspect)
    dy = mid_shoulder[1] - mid_hip[1]
    if math.hypot(dx, dy) < 1e-9:
        return None
    # "Up" in image coordinates is (0, -1); atan2(dx, -dy) is 0 when the
    # trunk is vertical and positive when the shoulders sit toward image right.
    return _finite(math.degrees(math.atan2(dx, -dy)))


def _weighted_com(mid_shoulder, mid_hip, torso_points):
    """Trunk COM proxy: 0.35 hips + 0.35 shoulders + 0.30 mid-torso mean.

    This is a body-segment weight proxy for the TRUNK only (feet and arms are
    excluded), not a whole-body COM and not a COP.  When one of the two
    midpoints is invisible the other one stands in (weight renormalised by
    construction) so a half-occluded frame still yields a usable trunk point.
    """
    if mid_shoulder is not None and mid_hip is not None:
        pts = [p for p in torso_points if p is not None]
        mid_torso = None
        if pts:
            mid_torso = (sum(p[0] for p in pts) / len(pts),
                         sum(p[1] for p in pts) / len(pts))
        else:
            mid_torso = _mid(mid_shoulder, mid_hip)
        x = 0.35 * mid_hip[0] + 0.35 * mid_shoulder[0] + 0.30 * mid_torso[0]
        y = 0.35 * mid_hip[1] + 0.35 * mid_shoulder[1] + 0.30 * mid_torso[1]
        return (x, y)
    return mid_shoulder if mid_shoulder is not None else mid_hip


def frame_metrics(lms, width=None, height=None, aspect=None):
    """Per-frame pose metrics for one 33-landmark array.

    ``width`` / ``height`` are the frame's PIXEL dimensions (the worker passes
    ``frame.shape[1]`` / ``frame.shape[0]``) or pass ``aspect = width/height``
    directly.  Every angle and tilt below is computed in aspect-corrected
    coordinates: x is scaled by ``aspect`` so both axes are in units of frame
    height.  Without either argument the frame is treated as square, which is
    the only shape where an uncorrected angle is physical - a 16:9 camera then
    reports a true 45 deg knee as 29.33 deg (H1 in the 2026-10-21 QC review).

    Returns a dict containing only the metrics that could actually be computed
    (an occluded joint is absent, never 0).  Keys:

    ``left_knee`` / ``right_knee`` / ``left_hip`` / ``right_hip`` /
    ``left_ankle`` / ``right_ankle``
        Included joint angles in degrees (knee = hip-knee-ankle, hip =
        shoulder-hip-knee, ankle = knee-ankle-toe).  Sagittal ESTIMATES.
    ``trunk_lean_deg``
        Signed mid-hip -> mid-shoulder lean from vertical, + = toward image
        right.
    ``head_tilt`` / ``shoulder_tilt`` / ``pelvic_tilt_deg``
        Signed line tilts in degrees, + = the right-index landmark is lower in
        the image (``pelvic_tilt_deg`` follows that convention explicitly:
        + = right hip lower).
    ``shoulder_width_norm`` / ``hip_width_norm``
        Euclidean segment length in normalised image units (x in [0, 1]).
        Both endpoints must be visible.  NOT aspect-corrected: this is a
        width-axis length, useful as a within-session scale reference.
    ``body_height_norm``
        Nose-to-mean-foot vertical span in normalised image units; the scale
        used to express sway as a fraction of body height.
    ``visibility_min``
        Lowest visibility across the tracked body landmarks (quality flag).
    ``com_x`` / ``com_y`` / ``trunk_x`` / ``trunk_y``
        Trunk COM proxy and trunk centre in normalised image units.  The
        ``trunk_*`` pair is what :func:`sway_metrics` baselines.
    """
    out = {}
    if not lms:
        return out

    scale = _frame_aspect(width=width, height=height, aspect=aspect)

    # Midpoints drive every tilt/COM number, so they are gated at VIS_GATE: a
    # shoulder inferred from a sliver of pixels must not move the trunk line.
    sh_l = _point(lms, L_SHOULDER, VIS_GATE)
    sh_r = _point(lms, R_SHOULDER, VIS_GATE)
    hip_l = _point(lms, L_HIP, VIS_GATE)
    hip_r = _point(lms, R_HIP, VIS_GATE)

    for key, (a, b, c) in (
            ("left_knee", (L_HIP, L_KNEE, L_ANKLE)),
            ("right_knee", (R_HIP, R_KNEE, R_ANKLE)),
            ("left_hip", (L_SHOULDER, L_HIP, L_KNEE)),
            ("right_hip", (R_SHOULDER, R_HIP, R_KNEE)),
            ("left_ankle", (L_KNEE, L_ANKLE, L_FOOT)),
            ("right_ankle", (R_KNEE, R_ANKLE, R_FOOT))):
        value = joint_angle(lms, a, b, c, aspect=scale)
        if value is not None:
            out[key] = value

    mid_sh = _mid(sh_l, sh_r)
    mid_hip = _mid(hip_l, hip_r)

    lean = _trunk_lean_deg(mid_sh, mid_hip, aspect=scale)
    if lean is not None:
        out["trunk_lean_deg"] = lean

    head = _line_tilt_deg(_point(lms, L_EAR, VIS_GATE),
                          _point(lms, R_EAR, VIS_GATE), aspect=scale)
    if head is not None:
        out["head_tilt"] = head

    shoulder_tilt = _line_tilt_deg(sh_l, sh_r, aspect=scale)
    if shoulder_tilt is not None:
        out["shoulder_tilt"] = shoulder_tilt

    pelvic_tilt = _line_tilt_deg(hip_l, hip_r, aspect=scale)
    if pelvic_tilt is not None:
        out["pelvic_tilt_deg"] = pelvic_tilt

    if sh_l is not None and sh_r is not None:
        out["shoulder_width_norm"] = _finite(
            math.hypot(sh_r[0] - sh_l[0], sh_r[1] - sh_l[1]))
    if hip_l is not None and hip_r is not None:
        out["hip_width_norm"] = _finite(
            math.hypot(hip_r[0] - hip_l[0], hip_r[1] - hip_l[1]))

    nose = _point(lms, NOSE, VIS_GATE)
    feet = [p for p in (_point(lms, L_FOOT, VIS_GATE),
                        _point(lms, R_FOOT, VIS_GATE)) if p is not None]
    if nose is not None and feet:
        foot_y = sum(p[1] for p in feet) / len(feet)
        height = foot_y - nose[1]
        if height > 1e-6:
            out["body_height_norm"] = _finite(height)

    com = _weighted_com(mid_sh, mid_hip,
                        [_point(lms, L_SHOULDER, VIS_GATE),
                         _point(lms, R_SHOULDER, VIS_GATE),
                         _point(lms, L_HIP, VIS_GATE),
                         _point(lms, R_HIP, VIS_GATE)])
    if com is not None:
        out["com_x"] = _finite(com[0])
        out["com_y"] = _finite(com[1])
    trunk = _mid(mid_sh, mid_hip)
    if trunk is None:
        trunk = mid_sh if mid_sh is not None else mid_hip
    if trunk is not None:
        out["trunk_x"] = _finite(trunk[0])
        out["trunk_y"] = _finite(trunk[1])

    vis = []
    for idx in _TRACKED:
        p = _point(lms, idx)
        if p is not None:
            vis.append(p[2])
    if vis:
        out["visibility_min"] = _finite(min(vis))

    return out


def _metrics_of(frame):
    """Accept a worker frame ``{"metrics": {...}}`` or a bare metrics dict."""
    if not isinstance(frame, dict):
        return {}
    metrics = frame.get("metrics")
    if isinstance(metrics, dict):
        return metrics
    return frame


def baseline_frames(baseline_s=SWAY_BASELINE_S, fps=None):
    """Rolling-window length in FRAMES for a ``baseline_s``-second baseline.

    ``fps`` is the measured frame rate of the clip (processed rate, from the
    frame payloads or their timestamps).  When it is missing or unusable the
    legacy :data:`SWAY_BASELINE_FRAMES` fallback is returned and the caller is
    expected to say so - a frame-count window silently means a different
    duration at every camera setting (H10).
    """
    seconds = _finite(baseline_s)
    if seconds is None or seconds <= 0.0:
        seconds = SWAY_BASELINE_S
    rate = _finite(fps)
    if rate is None or rate <= 0.0:
        return SWAY_BASELINE_FRAMES
    return max(SWAY_MIN_FRAMES, int(round(seconds * rate)))


def baseline_point(history, max_frames=SWAY_BASELINE_FRAMES):
    """Mean ``(trunk_x, trunk_y)`` over the last ``max_frames`` history frames.

    ``None`` until at least :data:`SWAY_MIN_FRAMES` finite points exist.  Falls
    back to the COM proxy for frames recorded before ``trunk_*`` existed.
    """
    frames = list(history or [])[-int(max_frames):]
    xs, ys = [], []
    for frame in frames:
        metrics = _metrics_of(frame)
        x = _finite(metrics.get("trunk_x", metrics.get("com_x")))
        y = _finite(metrics.get("trunk_y", metrics.get("com_y")))
        if x is None or y is None:
            continue
        xs.append(x)
        ys.append(y)
    if len(xs) < SWAY_MIN_FRAMES:
        return None
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def sway_metrics(history, current, max_frames=None,
                 min_frames=SWAY_MIN_FRAMES, baseline_s=SWAY_BASELINE_S,
                 fps=None):
    """RELATIVE trunk sway of ``current`` against the rolling baseline.

    ``history`` is the sequence of previously processed frames (worker frames
    or bare metrics dicts), ``current`` the frame being scored.  Only the last
    ``max_frames`` history entries are used; when ``max_frames`` is omitted the
    window is :func:`baseline_frames` ``(baseline_s, fps)`` - i.e. a TIME
    window sized from the measured frame rate, not a fixed frame count (H10).
    Output is the offset of the current trunk point from the baseline mean,
    divided by the CURRENT body height, i.e. sway expressed as a fraction of
    the person's on-screen height:

        ``trunk_sway_x = (trunk_x - baseline_x) / body_height_norm``

    Both keys are ``None`` until at least ``min_frames`` usable history frames
    exist, and whenever the current body height is unavailable.

    THIS IS RELATIVE SWAY, NOT AN ABSOLUTE DISPLACEMENT: without a plate-
    referenced camera calibration the baseline is wherever the person happened
    to stand, and body-height normalisation only removes gross scale changes
    (camera distance, growth, crop).  Compare within a session, never across
    sessions or camera positions.  The rolling mean also removes the slow
    component by construction, so this is short-term sway about a moving
    baseline - :func:`summarize_frames` reports the total range separately.
    """
    none_result = {"trunk_sway_x": None, "trunk_sway_y": None}
    current_metrics = _metrics_of(current)
    if not current_metrics:
        return none_result
    height = _finite(current_metrics.get("body_height_norm"))
    if height is None or height <= 0:
        return none_result
    if max_frames is None:
        max_frames = baseline_frames(baseline_s, fps)
    else:
        max_frames = max(int(min_frames), int(max_frames))
    base = baseline_point(history, max_frames=max_frames)
    if base is None:
        return none_result
    cur_x = _finite(current_metrics.get("trunk_x", current_metrics.get("com_x")))
    cur_y = _finite(current_metrics.get("trunk_y", current_metrics.get("com_y")))
    if cur_x is None or cur_y is None:
        return none_result
    return {"trunk_sway_x": _finite((cur_x - base[0]) / height),
            "trunk_sway_y": _finite((cur_y - base[1]) / height)}


def _stats(values):
    """``{mean, sd, min, max}`` for finite values, or ``None`` when empty.

    ``sd`` is the sample standard deviation (n-1) and is ``None`` for a single
    sample - one frame cannot measure variability.
    """
    vals = [v for v in (_finite(x) for x in values) if v is not None]
    if not vals:
        return None
    n = len(vals)
    mean = sum(vals) / n
    sd = None
    if n >= 2:
        var = sum((v - mean) ** 2 for v in vals) / (n - 1)
        sd = _finite(math.sqrt(var))
    return {"mean": _finite(mean), "sd": sd,
            "min": _finite(min(vals)), "max": _finite(max(vals))}


def _p95(values):
    """Nearest-rank 95th percentile of finite values (no interpolation)."""
    vals = sorted(v for v in (_finite(x) for x in values) if v is not None)
    if not vals:
        return None
    rank = max(0, math.ceil(0.95 * len(vals)) - 1)
    return _finite(vals[min(rank, len(vals) - 1)])


def _fps_from_timestamps(rows):
    """Median-interval frame rate from ``t_mono_ms`` / ``t_rel``, else ``None``.

    Plate-trial rows are built as ``{**metrics, 'frame_id', 't_mono_ms',
    't_rel'}`` and carry no ``fps``; their timestamps still say how fast frames
    arrived, which is what lets the sway window stay time-based (H10).
    """
    for key, scale in (("t_mono_ms", 1000.0), ("t_rel", 1.0)):
        stamps = []
        for row in rows:
            value = _finite(row.get(key))
            if value is not None:
                stamps.append(value)
        if len(stamps) < 2:
            continue
        diffs = sorted(b - a for a, b in zip(stamps, stamps[1:]) if b > a)
        if not diffs:
            continue
        middle = len(diffs) // 2
        median = (diffs[middle] if len(diffs) % 2
                  else 0.5 * (diffs[middle - 1] + diffs[middle]))
        if median <= 0.0:
            continue
        rate = scale / median
        if math.isfinite(rate) and rate > 0.0:
            return rate
    return None


def summarize_frames(frames, baseline_s=SWAY_BASELINE_S):
    """Descriptive summary of a clip's worker frames.

    ``frames`` is the list of JSONL frame dicts (``{"frame_id", "pose_found",
    "fps", "metrics", "latency_ms"}`` or bare metrics dicts).  ``baseline_s``
    is the sway baseline length in SECONDS (default
    :data:`SWAY_BASELINE_S`); the rolling window is sized from the clip's
    measured frame rate (payload ``fps``, else the median timestamp interval),
    so a clip captured at 30 fps and one at 60 fps use windows of the same
    DURATION and report comparable sway.  Returns ``{'frames': n,
    'available': False, ...}`` when fewer than :data:`SWAY_MIN_FRAMES` frames
    were captured - three frames say nothing about a person's sway.  Otherwise:

    * ``pose_coverage_pct`` - share of frames with a detected pose;
    * ``fps_mean`` - mean reported processing fps;
    * ``sway_baseline_s`` / ``sway_baseline_frames`` / ``sway_baseline_fps`` -
      the window actually used, and the rate it was derived from (the rate key
      is absent and a note is added when only the 150-frame fallback applies);
    * ``angles`` - ``{name: {mean, sd, min, max}}`` for the six sagittal angles
      plus trunk lean and pelvic tilt, finite samples only;
    * ``sway`` - the RELATIVE (rolling-baseline residual) ranges
      ``trunk_sway_x_range`` / ``trunk_sway_y_range``, the TOTAL ranges
      ``trunk_x_range`` / ``trunk_y_range`` of the raw trunk point, and the raw
      COM proxy ranges ``com_x_range`` / ``com_y_range``, in normalised image /
      body-height units;
    * ``latency_ms`` - ``{mean, p95}`` when frames carry it;
    * ``notes`` - coverage/quality warnings for the report;
    * ``estimates_only`` / ``mdc_note`` - always present, so a consumer cannot
      silently promote these numbers to clinical absolutes.

    Missing metrics are omitted, never zero-filled.
    """
    rows = [f for f in (frames or []) if isinstance(f, dict)]
    n = len(rows)
    result = {"frames": n, "available": False, "notes": [],
              "estimates_only": True, "mdc_note": MDC_NOTE}
    if n < SWAY_MIN_FRAMES:
        result["notes"].append("insufficient frames")
        return result

    found = 0
    for row in rows:
        pose_found = row.get("pose_found")
        if isinstance(pose_found, bool):
            found += 1 if pose_found else 0
        else:
            found += 1 if _metrics_of(row) else 0
    coverage = _finite(100.0 * found / n)
    result["available"] = True
    result["pose_coverage_pct"] = coverage

    fps_vals = [_finite(row.get("fps")) for row in rows]
    fps_vals = [v for v in fps_vals if v is not None and v > 0.0]
    rate = None
    if fps_vals:
        rate = _finite(sum(fps_vals) / len(fps_vals))
        if rate is not None:
            result["fps_mean"] = rate
    if rate is None:
        rate = _fps_from_timestamps(rows)

    seconds = _finite(baseline_s)
    if seconds is None or seconds <= 0.0:
        seconds = SWAY_BASELINE_S
    window_frames = baseline_frames(seconds, rate)
    result["sway_baseline_s"] = float(seconds)
    result["sway_baseline_frames"] = float(window_frames)
    if rate is not None:
        result["sway_baseline_fps"] = _finite(rate)
    else:
        result["notes"].append(
            "frame rate unknown - sway baseline falls back to "
            f"{SWAY_BASELINE_FRAMES} frames (assumes 60 fps)")

    angles = {}
    for key in SUMMARY_ANGLE_KEYS:
        stats = _stats([_metrics_of(row).get(key) for row in rows])
        if stats is not None:
            angles[key] = stats
    if angles:
        result["angles"] = angles

    sway_x, sway_y, com_x, com_y = [], [], [], []
    trunk_x, trunk_y = [], []
    for index, row in enumerate(rows):
        metrics = _metrics_of(row)
        cx = _finite(metrics.get("com_x"))
        cy = _finite(metrics.get("com_y"))
        if cx is not None:
            com_x.append(cx)
        if cy is not None:
            com_y.append(cy)
        tx = _finite(metrics.get("trunk_x", metrics.get("com_x")))
        ty = _finite(metrics.get("trunk_y", metrics.get("com_y")))
        if tx is not None:
            trunk_x.append(tx)
        if ty is not None:
            trunk_y.append(ty)
        if index == 0:
            continue
        window = rows[max(0, index - window_frames):index]
        sway = sway_metrics(window, row, max_frames=window_frames)
        if sway["trunk_sway_x"] is not None:
            sway_x.append(sway["trunk_sway_x"])
            sway_y.append(sway["trunk_sway_y"])
    sway_out = {}
    if sway_x:
        sway_out["trunk_sway_x_range"] = _finite(max(sway_x) - min(sway_x))
        sway_out["trunk_sway_y_range"] = _finite(max(sway_y) - min(sway_y))
    if trunk_x:
        sway_out["trunk_x_range"] = _finite(max(trunk_x) - min(trunk_x))
    if trunk_y:
        sway_out["trunk_y_range"] = _finite(max(trunk_y) - min(trunk_y))
    if com_x:
        sway_out["com_x_range"] = _finite(max(com_x) - min(com_x))
    if com_y:
        sway_out["com_y_range"] = _finite(max(com_y) - min(com_y))
    if sway_out:
        result["sway"] = sway_out

    latencies = [_finite(row.get("latency_ms")) for row in rows]
    latencies = [v for v in latencies if v is not None]
    if latencies:
        result["latency_ms"] = {
            "mean": _finite(sum(latencies) / len(latencies)),
            "p95": _p95(latencies),
        }

    if coverage is not None and coverage < 70.0:
        result["notes"].append(
            "pose coverage below 70% - results descriptive only")
    return result
