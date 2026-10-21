"""Normalised facial-expression PROXIES from MediaPipe FaceLandmarker - STDLIB.

What this is
------------
Five geometric ratios computed from the 478-point canonical face mesh that
MediaPipe's FaceLandmarker returns.  They describe *how the face is moving*:

===============  =========================================================
``mouth_open``   inner-lip gap / face height.  Jaw opening proxy.
``brow_raise``   inner-brow height above the eye line / face height.
``brow_furrow``  how far the inner brows close in, relative to inter-eye
                 distance (0 = neutral spacing, 1 = brows touching).
``eye_squint``   eye aperture / eye width, inverted (0 = relaxed/wide,
                 1 = narrowest).
``visible``      FaceLandmarker does not populate per-landmark visibility, so
                 the worker fills this with :func:`presence_proxy` (a mesh-
                 presence fraction) or ``False`` when no face was detected.
===============  =========================================================

All five are clamped to [0, 1]; each is ``None`` when the landmarks it needs
are missing or degenerate.  Nothing is invented: a half-visible face yields
``None`` for the affected proxy rather than 0.

WHAT THIS IS NOT
----------------
These are PROXIES, not emotion recognition and **not pain detection**.  There
is no validated facial pain scale behind these numbers: no PSPI, no FACS
coding, no trained classifier, no per-subject calibration.  A furrowed brow
can mean concentration, bright light, effort or camera angle; an open mouth
can mean speech or breathing.  The worker's ``face_pain_proxy`` event is a
display hint that the geometry *resembles* a guarded expression - it must
never be presented to a patient or recorded as a finding.

Units are normalised image coordinates (x right, y DOWN, values in [0, 1]),
matching the rest of the vision sidecar.  Because y grows downward, "brow
above eye line" is a *smaller* y.  Because MediaPipe normalises x by frame
width and y by frame height, a non-square frame needs ``aspect = width /
height`` (the worker passes it); without it, ``eye_squint`` reads high on tall
frames and low on wide ones.
"""

from __future__ import annotations

import math

# ---------------------------------------------------------------- landmarks
# MediaPipe FaceLandmarker / canonical_face_model (478 points incl. irises).
# Names follow the mediapipe face mesh documentation.
FOREHEAD_TOP = 10       # top of the forehead / hairline
CHIN = 152              # bottom of the chin
LIP_UPPER_INNER = 13    # centre of the upper inner lip
LIP_LOWER_INNER = 14    # centre of the lower inner lip
L_EYE_OUTER = 33        # left eye, outer corner (subject's left)
L_EYE_INNER = 133       # left eye, inner corner
L_EYE_UPPER = 159       # left upper eyelid centre
L_EYE_LOWER = 145       # left lower eyelid centre
R_EYE_INNER = 362       # right eye, inner corner
R_EYE_OUTER = 263       # right eye, outer corner
R_EYE_UPPER = 386       # right upper eyelid centre
R_EYE_LOWER = 374       # right lower eyelid centre
L_BROW_INNER = 107      # left inner brow
R_BROW_INNER = 336      # right inner brow

#: Keys returned by :func:`face_metrics`, in display order.
PROXIES = ("mouth_open", "brow_raise", "brow_furrow", "eye_squint", "visible")

#: Canonical face-mesh vertex count (468 without irises; FaceLandmarker with
#: refinement returns 478).  Used by :func:`presence_proxy`.
FACE_MESH_POINTS = 468


def presence_proxy(landmarks):
    """Presence-based ``visible`` proxy: ``min(1, n_points / 468)``.

    MediaPipe's FaceLandmarker does NOT populate per-landmark visibility (the
    field is absent or 0.0), so a returned mesh cannot report a *mean
    visibility*.  This proxy answers the only question the data can answer -
    "was a face detected and how complete is the mesh" - and is documented as
    such wherever it is emitted: 1.0 means the full canonical mesh came back,
    a lower value means fewer points, and ``None`` means no landmarks at all.

    The worker uses it for ``face.visible``; it must never be read as an
    occlusion or pain measure.
    """
    try:
        count = len(landmarks)
    except TypeError:
        return None
    if count <= 0:
        return None
    return float(min(1.0, count / float(FACE_MESH_POINTS)))

# ------------------------------------------------------------- calibration
# Heuristic reference values, expressed as fractions of face height (chin to
# forehead) or as aperture/width ratios.  They are midpoints sanity-checked
# against a handful of public portraits (neutral head-and-shoulders shots) so
# a resting face lands near 0 - NOT fitted to a labelled dataset and NOT a
# clinical scale.  They exist so that 0 means "resting" and 1 means "clearly
# at the end of the range", and nothing more.  Recalibrate per camera setup if
# the proxies sit pinned at 0 or 1 for a resting subject.
MOUTH_NEUTRAL = 0.012      # inner-lip gap / face height at rest
MOUTH_SPAN = 0.110         # additional gap mapped to 1.0
BROW_RAISE_NEUTRAL = 0.115  # inner-brow-to-eye-line / face height at rest
BROW_RAISE_SPAN = 0.060    # extra brow lift mapped to 1.0
BROW_GAP_NEUTRAL = 0.570   # inner-brow gap / inter-eye distance at rest
BROW_GAP_SPAN = 0.300      # closing this much ratio reaches 1.0
EYE_NEUTRAL = 0.300        # aperture / eye width at rest
EYE_SPAN = 0.180           # narrowing this much reaches 1.0

#: Thresholds used by :func:`pain_proxy_triggered` (worker event).
PAIN_PROXY_FURROW = 0.60
PAIN_PROXY_RAISE = 0.50


# ------------------------------------------------------------------- helpers

def _point(landmarks, index):
    """``(x, y, z, visibility|None)`` for ``landmarks[index]``, else ``None``.

    Accepts 2-, 3- or 4-element points.  MediaPipe gives ``[x, y, z]``; a
    fourth element is treated as visibility (tests, or a caller that merged
    the optional visibility array).  Non-finite or missing points are ``None``
    so a proxy built on them disappears instead of reporting a fake zero.
    """
    try:
        raw = landmarks[index]
    except (IndexError, KeyError, TypeError):
        return None
    if raw is None:
        return None
    try:
        x = float(raw[0])
        y = float(raw[1])
    except (TypeError, ValueError, IndexError):
        return None
    z = 0.0
    vis = None
    try:
        if len(raw) > 2 and raw[2] is not None:
            candidate = float(raw[2])
            if math.isfinite(candidate):
                z = candidate
    except (TypeError, ValueError, IndexError):
        z = 0.0
    try:
        if len(raw) > 3 and raw[3] is not None:
            candidate = float(raw[3])
            if math.isfinite(candidate):
                vis = candidate
    except (TypeError, ValueError, IndexError):
        vis = None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return (x, y, z, vis)


def _mid(a, b):
    if a is None or b is None:
        return None
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def _dist(a, b, aspect=1.0):
    """2D distance in height-normalised units.

    MediaPipe normalises x by image width and y by image height, so a raw
    ``hypot(dx, dy)`` is only physically meaningful on a square frame.  Scaling
    dx by ``aspect = width / height`` puts both axes in units of frame height,
    which is what makes ``eye_squint`` (vertical aperture over horizontal eye
    width) comparable across 16:9 and 4:3 feeds.
    """
    if a is None or b is None:
        return None
    return math.hypot((b[0] - a[0]) * aspect, b[1] - a[1])


def _clamp01(value):
    if value is None:
        return None
    if not math.isfinite(value):
        return None
    if abs(value) < 1e-9:
        return 0.0  # keep float noise out of JSON and out of "furrow > 0" reads
    return float(max(0.0, min(1.0, value)))


def _mean_visibility(landmarks, visibility=None):
    """Mean visibility over the mesh, or ``None`` when none was supplied."""
    values = []
    if visibility is not None:
        try:
            iterator = iter(visibility)
        except TypeError:
            iterator = iter(())
        for raw in iterator:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                values.append(value)
    else:
        try:
            count = len(landmarks)
        except TypeError:
            count = 0
        for index in range(count):
            point = _point(landmarks, index)
            if point is not None and point[3] is not None:
                values.append(point[3])
    if not values:
        return None
    return _clamp01(sum(values) / len(values))


# --------------------------------------------------------------------- main

def face_metrics(landmarks, visibility=None, aspect=1.0):
    """Expression proxies for one 478-point face mesh.

    ``landmarks`` is a sequence of ``[x, y, z]`` (optionally ``[x, y, z,
    visibility]``) in normalised image units.  ``visibility`` may be a parallel
    sequence when the caller keeps visibility separate.  ``aspect`` is the
    frame's ``width / height`` - pass it whenever the frame is not square, so
    horizontal and vertical distances are compared in the same physical units
    (it only moves ``eye_squint`` and, mildly, the brow/mouth distances).

    Returns a dict with exactly :data:`PROXIES` as keys.  Every value is a
    float in [0, 1] or ``None`` when its landmarks are missing - see the module
    docstring for what each one means and for the "not pain detection" limit.
    """
    try:
        aspect = float(aspect)
    except (TypeError, ValueError):
        aspect = 1.0
    if not math.isfinite(aspect) or aspect <= 0:
        aspect = 1.0
    out = {key: None for key in PROXIES}
    points = landmarks if landmarks is not None else []
    out["visible"] = _mean_visibility(points, visibility)
    if not points:
        return out

    forehead = _point(points, FOREHEAD_TOP)
    chin = _point(points, CHIN)
    face_h = None
    if forehead is not None and chin is not None:
        span = abs(chin[1] - forehead[1])
        if span > 1e-6:
            face_h = span

    # ---- mouth: inner lip gap, normalised by face height
    upper = _point(points, LIP_UPPER_INNER)
    lower = _point(points, LIP_LOWER_INNER)
    gap = _dist(upper, lower, aspect)
    if gap is not None and face_h is not None:
        ratio = gap / face_h
        out["mouth_open"] = _clamp01(
            (ratio - MOUTH_NEUTRAL) / MOUTH_SPAN)

    # ---- eye geometry (corners drive both the eye line and the width)
    eye_corners = [_point(points, index) for index in
                   (L_EYE_OUTER, L_EYE_INNER, R_EYE_OUTER, R_EYE_INNER)]
    if all(point is not None for point in eye_corners):
        eye_line_y = sum(point[1] for point in eye_corners) / 4.0
    else:
        eye_line_y = None

    brow_l = _point(points, L_BROW_INNER)
    brow_r = _point(points, R_BROW_INNER)

    # ---- brow raise: inner-brow height above the eye line / face height
    if (face_h is not None and eye_line_y is not None
            and brow_l is not None and brow_r is not None):
        brow_y = (brow_l[1] + brow_r[1]) / 2.0
        raw = (eye_line_y - brow_y) / face_h
        out["brow_raise"] = _clamp01(
            (raw - BROW_RAISE_NEUTRAL) / BROW_RAISE_SPAN)

    # ---- brow furrow: the inner brows closing toward each other
    left_center = _mid(eye_corners[0], eye_corners[1])
    right_center = _mid(eye_corners[3], eye_corners[2])
    inter_eye = _dist(left_center, right_center, aspect)
    brow_gap = _dist(brow_l, brow_r, aspect)
    if inter_eye is not None and brow_gap is not None and inter_eye > 1e-9:
        ratio = brow_gap / inter_eye
        out["brow_furrow"] = _clamp01(
            (BROW_GAP_NEUTRAL - ratio) / BROW_GAP_SPAN)

    # ---- eye squint: aperture / width, averaged over the eyes we can see
    ratios = []
    for outer, inner, upper_lid, lower_lid in (
            (L_EYE_OUTER, L_EYE_INNER, L_EYE_UPPER, L_EYE_LOWER),
            (R_EYE_OUTER, R_EYE_INNER, R_EYE_UPPER, R_EYE_LOWER)):
        width = _dist(_point(points, outer), _point(points, inner), aspect)
        aperture = _dist(_point(points, upper_lid), _point(points, lower_lid),
                         aspect)
        if width is not None and aperture is not None and width > 1e-9:
            ratios.append(aperture / width)
    if ratios:
        mean_ratio = sum(ratios) / len(ratios)
        out["eye_squint"] = _clamp01((EYE_NEUTRAL - mean_ratio) / EYE_SPAN)

    return out


def pain_proxy_triggered(face, furrow=PAIN_PROXY_FURROW, raise_=PAIN_PROXY_RAISE):
    """True when furrow AND brow raise both exceed their proxy thresholds.

    This is the rule behind the worker's ``face_pain_proxy`` event.  It means
    "the brows are lowered and pulled together" - a guarded/effortful look.
    It is NOT a pain detector; see the module docstring.
    """
    if not isinstance(face, dict):
        return False
    try:
        furrow_value = float(face.get("brow_furrow"))
        raise_value = float(face.get("brow_raise"))
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(furrow_value) and math.isfinite(raise_value)):
        return False
    return furrow_value > float(furrow) and raise_value > float(raise_)
