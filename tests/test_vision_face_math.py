"""TEST ONLY: pure-stdlib tests for vision.face_math.

The fixtures are synthetic 478-point meshes: every landmark used by the module
is placed at hand-chosen coordinates, so the expected proxy follows from the
geometry in the builder (face height 0.80, inter-eye 0.12, ...) rather than
from the implementation's output.  No camera, no mediapipe, no model file.
"""

import math
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vision import face_math  # noqa: E402

# Geometry of the synthetic face (normalised image units, y grows downward).
FOREHEAD_Y = 0.10
CHIN_Y = 0.90
FACE_H = CHIN_Y - FOREHEAD_Y          # 0.80
EYE_Y = 0.40
EYE_L_OUT, EYE_L_IN = 0.42, 0.50      # left eye corners (x)
EYE_R_IN, EYE_R_OUT = 0.54, 0.62      # right eye corners (x)
INTER_EYE = 0.58 - 0.46               # eye-centre distance = 0.12
MOUTH_Y = 0.72


def mesh(mouth=0.008, brow_gap=0.075, brow_rise=0.092, aperture=0.030,
         visibility=None, aspect=1.0):
    """Build a 478-point face mesh with the named expression parameters.

    ``mouth``      inner-lip vertical gap in normalised units
    ``brow_gap``   distance between the two inner-brow points
    ``brow_rise``  inner-brow height above the eye line (0.092 = 0.115 of the
                   0.80 face height, i.e. the module's resting reference)
    ``aperture``   eyelid opening (upper-to-lower lid distance)
    ``visibility`` optional per-point visibility appended as a 4th element
    ``aspect``     when != 1.0, x coordinates are divided by it, simulating a
                   frame whose width/height is ``aspect`` (pass the same value
                   to :func:`face_math.face_metrics`)
    """
    landmarks = [[0.5, 0.5, 0.0] for _ in range(478)]

    def put(index, x, y):
        point = [x / aspect, y, 0.0]
        if visibility is not None:
            point.append(visibility)
        landmarks[index] = point

    put(face_math.FOREHEAD_TOP, 0.5, FOREHEAD_Y)
    put(face_math.CHIN, 0.5, CHIN_Y)

    put(face_math.L_EYE_OUTER, EYE_L_OUT, EYE_Y)
    put(face_math.L_EYE_INNER, EYE_L_IN, EYE_Y)
    put(face_math.R_EYE_INNER, EYE_R_IN, EYE_Y)
    put(face_math.R_EYE_OUTER, EYE_R_OUT, EYE_Y)
    put(face_math.L_EYE_UPPER, 0.46, EYE_Y - aperture / 2.0)
    put(face_math.L_EYE_LOWER, 0.46, EYE_Y + aperture / 2.0)
    put(face_math.R_EYE_UPPER, 0.58, EYE_Y - aperture / 2.0)
    put(face_math.R_EYE_LOWER, 0.58, EYE_Y + aperture / 2.0)

    brow_y = EYE_Y - brow_rise
    put(face_math.L_BROW_INNER, 0.5 - brow_gap / 2.0, brow_y)
    put(face_math.R_BROW_INNER, 0.5 + brow_gap / 2.0, brow_y)

    put(face_math.LIP_UPPER_INNER, 0.5, MOUTH_Y)
    put(face_math.LIP_LOWER_INNER, 0.5, MOUTH_Y + mouth)
    return landmarks


class MouthTests(unittest.TestCase):
    def test_closed_mouth_is_zero(self):
        value = face_math.face_metrics(mesh(mouth=0.008))["mouth_open"]
        self.assertIsNotNone(value)
        self.assertLess(value, 0.05)

    def test_open_mouth_is_high(self):
        value = face_math.face_metrics(mesh(mouth=0.10))["mouth_open"]
        self.assertGreater(value, 0.9)

    def test_mouth_opens_monotonically(self):
        values = [face_math.face_metrics(mesh(mouth=gap))["mouth_open"]
                  for gap in (0.008, 0.03, 0.06, 0.09)]
        self.assertEqual(values, sorted(values))
        self.assertLess(values[0], values[-1])

    def test_missing_lip_landmark_is_none(self):
        landmarks = mesh()
        landmarks[face_math.LIP_LOWER_INNER] = None
        self.assertIsNone(face_math.face_metrics(landmarks)["mouth_open"])


class BrowTests(unittest.TestCase):
    def test_neutral_brow_raise_is_low(self):
        value = face_math.face_metrics(mesh())["brow_raise"]
        self.assertIsNotNone(value)
        self.assertLess(value, 0.05)

    def test_raised_brows_are_high(self):
        value = face_math.face_metrics(mesh(brow_rise=0.14))["brow_raise"]
        self.assertGreater(value, 0.9)

    def test_neutral_furrow_is_zero(self):
        value = face_math.face_metrics(mesh())["brow_furrow"]
        self.assertIsNotNone(value)
        self.assertLess(value, 0.05)

    def test_knitted_brows_furrow(self):
        value = face_math.face_metrics(mesh(brow_gap=0.03))["brow_furrow"]
        self.assertGreater(value, 0.6)

    def test_wide_brow_gap_does_not_furrow(self):
        value = face_math.face_metrics(mesh(brow_gap=0.11))["brow_furrow"]
        self.assertEqual(value, 0.0)

    def test_missing_brow_is_none(self):
        landmarks = mesh()
        landmarks[face_math.R_BROW_INNER] = None
        proxies = face_math.face_metrics(landmarks)
        self.assertIsNone(proxies["brow_raise"])
        self.assertIsNone(proxies["brow_furrow"])

    def test_missing_chin_removes_height_based_proxies(self):
        landmarks = mesh()
        landmarks[face_math.CHIN] = None
        proxies = face_math.face_metrics(landmarks)
        # mouth_open and brow_raise are normalised by face height -> gone.
        self.assertIsNone(proxies["mouth_open"])
        self.assertIsNone(proxies["brow_raise"])
        # furrow/squint are ratio-based and survive a missing chin.
        self.assertIsNotNone(proxies["brow_furrow"])
        self.assertIsNotNone(proxies["eye_squint"])


class EyeTests(unittest.TestCase):
    def test_relaxed_eye_is_not_squinting(self):
        value = face_math.face_metrics(mesh(aperture=0.030))["eye_squint"]
        self.assertIsNotNone(value)
        self.assertLess(value, 0.1)

    def test_narrow_eye_squints(self):
        value = face_math.face_metrics(mesh(aperture=0.010))["eye_squint"]
        self.assertGreater(value, 0.9)

    def test_wide_eye_clamps_to_zero(self):
        value = face_math.face_metrics(mesh(aperture=0.060))["eye_squint"]
        self.assertEqual(value, 0.0)

    def test_missing_eye_uses_the_other_eye(self):
        landmarks = mesh(aperture=0.010)
        landmarks[face_math.L_EYE_UPPER] = None
        value = face_math.face_metrics(landmarks)["eye_squint"]
        self.assertIsNotNone(value)
        self.assertGreater(value, 0.9)


class AspectTests(unittest.TestCase):
    """MediaPipe normalises x by width and y by height: ratios need aspect."""

    def test_non_square_frame_reproduces_square_proxies(self):
        square = face_math.face_metrics(mesh(aperture=0.020))
        wide = face_math.face_metrics(mesh(aperture=0.020, aspect=1.6),
                                      aspect=1.6)
        for key in ("mouth_open", "brow_raise", "brow_furrow", "eye_squint"):
            self.assertAlmostEqual(wide[key], square[key], places=6, msg=key)

    def test_ignoring_aspect_skews_squint(self):
        # A tall frame (aspect < 1) compresses x, so an uncorrected aperture
        # ratio looks small and the eye looks more squinted than it is.
        tall_no_aspect = face_math.face_metrics(mesh(aperture=0.020,
                                                     aspect=0.6))
        square = face_math.face_metrics(mesh(aperture=0.020))
        self.assertGreater(tall_no_aspect["eye_squint"],
                           square["eye_squint"] + 0.1)

    def test_bad_aspect_falls_back_to_square(self):
        base = face_math.face_metrics(mesh())
        for bad in (0, -2, float("nan"), "wide", None):
            self.assertEqual(face_math.face_metrics(mesh(), aspect=bad), base)


class ClampTests(unittest.TestCase):
    def test_extreme_openings_stay_in_range(self):
        proxies = face_math.face_metrics(
            mesh(mouth=0.9, brow_gap=0.0, brow_rise=0.5, aperture=0.0))
        for key in face_math.PROXIES:
            value = proxies[key]
            if value is None:
                continue
            self.assertGreaterEqual(value, 0.0, key)
            self.assertLessEqual(value, 1.0, key)
        self.assertEqual(proxies["mouth_open"], 1.0)
        self.assertEqual(proxies["brow_furrow"], 1.0)

    def test_all_values_are_floats_or_none(self):
        for key, value in face_math.face_metrics(mesh()).items():
            if value is not None:
                self.assertIsInstance(value, float, key)


class VisibilityTests(unittest.TestCase):
    def test_fourth_element_becomes_visible(self):
        proxies = face_math.face_metrics(mesh(visibility=0.9))
        self.assertAlmostEqual(proxies["visible"], 0.9, places=6)

    def test_visibility_argument_overrides_points(self):
        landmarks = mesh()
        proxies = face_math.face_metrics(landmarks, visibility=[0.2, 0.4])
        self.assertAlmostEqual(proxies["visible"], 0.3, places=6)

    def test_no_visibility_is_none(self):
        self.assertIsNone(face_math.face_metrics(mesh())["visible"])


class MissingLandmarkTests(unittest.TestCase):
    def test_empty_and_none_landmarks(self):
        for landmarks in ([], None):
            proxies = face_math.face_metrics(landmarks)
            self.assertEqual(set(proxies), set(face_math.PROXIES))
            for key in face_math.PROXIES:
                self.assertIsNone(proxies[key], key)

    def test_short_array_is_none_not_an_exception(self):
        self.assertIsNone(face_math.face_metrics([[0.5, 0.5, 0.0]])["mouth_open"])

    def test_non_finite_coordinates_are_none(self):
        landmarks = mesh()
        landmarks[face_math.LIP_UPPER_INNER] = [float("nan"), 0.72, 0.0]
        self.assertIsNone(face_math.face_metrics(landmarks)["mouth_open"])

    def test_degenerate_face_height_is_none(self):
        landmarks = mesh()
        landmarks[face_math.FOREHEAD_TOP] = [0.5, CHIN_Y, 0.0]
        self.assertIsNone(face_math.face_metrics(landmarks)["mouth_open"])


class FaceVisibilityTests(unittest.TestCase):
    """M10 (QC 2026-10-21): FaceLandmarker has no per-landmark visibility.

    The worker therefore emits ``face.visible`` as a documented presence proxy
    (or ``False`` when a running face layer sees nobody), so the UI's "face not
    visible" state can actually fire.
    """

    def test_presence_proxy_full_mesh_is_one(self):
        self.assertEqual(face_math.presence_proxy(mesh()), 1.0)

    def test_presence_proxy_scales_with_point_count(self):
        self.assertAlmostEqual(
            face_math.presence_proxy([[0.5, 0.5, 0.0]] * 234), 0.5, places=6)

    def test_presence_proxy_empty_is_none(self):
        self.assertIsNone(face_math.presence_proxy([]))
        self.assertIsNone(face_math.presence_proxy(None))

    def test_worker_face_to_list_drops_zero_visibility(self):
        from vision import worker

        class Landmark:
            def __init__(self, x, y, z, visibility):
                self.x, self.y, self.z, self.visibility = x, y, z, visibility

        points = worker.face_to_list([Landmark(0.5, 0.5, 0.0, 0.0),
                                      Landmark(0.4, 0.4, 0.0, 0.8)])
        # A 0.0 visibility from the tasks API means "not populated", not
        # "fully occluded": it must not enter the mean.
        self.assertEqual(len(points[0]), 3)
        self.assertEqual(len(points[1]), 4)
        self.assertAlmostEqual(points[1][3], 0.8)

    def test_worker_derives_presence_when_visibility_is_absent(self):
        from vision import worker

        class Landmark:
            def __init__(self, x, y):
                self.x, self.y, self.z = x, y, 0.0

        points = worker.face_to_list([Landmark(0.5, 0.5)] * 478)
        self.assertIsNone(face_math.face_metrics(points)["visible"])
        self.assertEqual(face_math.presence_proxy(points), 1.0)


class PainProxyTests(unittest.TestCase):
    def test_trigger_requires_both_furrow_and_raise(self):
        both = face_math.face_metrics(mesh(brow_gap=0.02, brow_rise=0.14))
        self.assertGreater(both["brow_furrow"], 0.6)
        self.assertGreater(both["brow_raise"], 0.5)
        self.assertTrue(face_math.pain_proxy_triggered(both))

    def test_furrow_only_does_not_trigger(self):
        furrow_only = face_math.face_metrics(mesh(brow_gap=0.02))
        self.assertFalse(face_math.pain_proxy_triggered(furrow_only))

    def test_raise_only_does_not_trigger(self):
        raise_only = face_math.face_metrics(mesh(brow_rise=0.14))
        self.assertFalse(face_math.pain_proxy_triggered(raise_only))

    def test_resting_face_does_not_trigger(self):
        # Regression guard for the real-face calibration: a neutral mesh must
        # not sit at the top of the range on either component.
        resting = face_math.face_metrics(mesh())
        self.assertLess(resting["brow_raise"], 0.2)
        self.assertLess(resting["brow_furrow"], 0.2)

    def test_neutral_and_missing_do_not_trigger(self):
        self.assertFalse(face_math.pain_proxy_triggered(face_math.face_metrics(mesh())))
        self.assertFalse(face_math.pain_proxy_triggered({}))
        self.assertFalse(face_math.pain_proxy_triggered(None))

    def test_thresholds_are_overridable(self):
        neutral = face_math.face_metrics(mesh())
        self.assertTrue(face_math.pain_proxy_triggered(
            neutral, furrow=-1.0, raise_=-1.0))


class DocstringTests(unittest.TestCase):
    def test_module_says_proxies_not_pain_detection(self):
        text = (face_math.__doc__ or "").lower()
        self.assertIn("not pain detection", text)
        self.assertIn("proxies", text)

    def test_documents_landmark_indices(self):
        for name in ("LIP_UPPER_INNER", "LIP_LOWER_INNER", "L_BROW_INNER",
                     "R_BROW_INNER", "FOREHEAD_TOP", "CHIN"):
            self.assertTrue(hasattr(face_math, name), name)
        self.assertEqual(face_math.LIP_UPPER_INNER, 13)
        self.assertEqual(face_math.LIP_LOWER_INNER, 14)
        self.assertEqual(face_math.CHIN, 152)
        self.assertEqual(face_math.L_BROW_INNER, 107)
        self.assertEqual(face_math.R_BROW_INNER, 336)


class StdlibOnlyTests(unittest.TestCase):
    def test_no_third_party_imports(self):
        source = Path(face_math.__file__).read_text(encoding="utf-8")
        banned = re.compile(
            r"^\s*(?:import|from)\s+(numpy|cv2|mediapipe|sounddevice|"
            r"faster_whisper|scipy)\b", re.MULTILINE)
        self.assertIsNone(banned.search(source),
                          "face_math must stay pure stdlib")

    def test_module_imports_without_math_domain_errors(self):
        self.assertTrue(math.isfinite(face_math.MOUTH_SPAN))


class WorkerEventTests(unittest.TestCase):
    """Debounced event stream + /state contract (stdlib-only worker pieces)."""

    def test_debounce_accepts_once_then_drops(self):
        from vision import worker
        app = worker.App("test", 0)
        self.assertIsNotNone(app.add_event("pose_lost", "warning", "a"))
        self.assertIsNone(app.add_event("pose_lost", "warning", "b"))
        self.assertIsNotNone(app.add_event("low_visibility", "notice", "c"))
        self.assertEqual([event["event"] for event in app.drain_events()],
                         ["pose_lost", "low_visibility"])
        self.assertEqual(app.drain_events(), [])  # drained exactly once

    def test_event_record_shape(self):
        from vision import worker
        app = worker.App("test", 0)
        app.add_event("stt_unavailable", "notice", "no model")
        record = app.events_snapshot()[0]
        self.assertEqual(set(record),
                         {"event", "severity", "detail", "t_mono_ms"})
        self.assertEqual(record["severity"], "notice")
        self.assertIsInstance(record["t_mono_ms"], int)

    def test_unknown_severity_falls_back_to_info(self):
        from vision import worker
        app = worker.App("test", 0)
        app.add_event("odd", "critical", "x")
        self.assertEqual(app.events_snapshot()[0]["severity"], "info")

    def test_state_keeps_only_the_last_hundred_events(self):
        from vision import worker
        app = worker.App("test", 0)
        for index in range(150):
            app.events.add("tick", "info", str(index), debounce=0,
                           now=float(index))
        state = app.state_payload()
        self.assertEqual(len(state["events"]), worker.EVENT_BUFFER)
        self.assertEqual(state["events"][-1]["detail"], "149")

    def test_state_has_v1_keys_plus_additions(self):
        from vision import worker
        app = worker.App("test", 1)
        state = app.state_payload()
        for key in ("state", "error", "frames", "fps", "source", "port",
                    "latest", "model", "audio", "face", "events"):
            self.assertIn(key, state)
        self.assertEqual(state["model"],
                         {"backend": "mediapipe-pose-full", "face": False,
                          "audio": False, "stt": "off"})


class OverlayColorTests(unittest.TestCase):
    """The yellow skeleton is a product requirement - pin the colour parsing.

    vision.worker imports stdlib-only at module level, so this runs in the lab
    venv without cv2/mediapipe.
    """

    def test_default_skeleton_colour_is_yellow_ffd400(self):
        from vision import worker
        self.assertEqual(worker.CLR_SKELETON, (0, 212, 255))

    def test_parse_colour_hex_and_names(self):
        from vision import worker
        self.assertEqual(worker.parse_color("#FFD400"), (0, 212, 255))
        self.assertEqual(worker.parse_color("ffd400"), (0, 212, 255))
        self.assertEqual(worker.parse_color("#0f0"), (0, 255, 0))
        self.assertEqual(worker.parse_color("yellow"), (0, 212, 255))
        # Unparseable input falls back to the default rather than crashing.
        self.assertEqual(worker.parse_color("nonsense"), worker.CLR_SKELETON)
        self.assertEqual(worker.parse_color(""), worker.CLR_SKELETON)


if __name__ == "__main__":
    unittest.main()
