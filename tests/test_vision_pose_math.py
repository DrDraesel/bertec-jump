"""TEST ONLY: synthetic landmark fixtures for vision.pose_math.

Every landmark array here is generated in-process from hand-computed geometry
(no camera, no mediapipe, no recorded data).  Expected angles follow from the
vectors given in each test, not from the implementation's output.
"""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from vision import pose_math
except ImportError:
    pose_math = None


def standing():
    """Front-ish standing person, normalised image units, all joints visible.

    Shoulders y=0.25, hips y=0.50, knees y=0.70, ankles y=0.90, feet y=0.95;
    nose y=0.10 -> body height 0.85.  Legs and arms are straight (180 deg).
    """
    lms = [[0.5, 0.5, 0.0] for _ in range(33)]

    def put(index, x, y, vis=0.99):
        lms[index] = [x, y, vis]

    put(0, 0.50, 0.10)
    put(7, 0.47, 0.11)
    put(8, 0.53, 0.11)
    put(11, 0.45, 0.25)
    put(12, 0.55, 0.25)
    put(13, 0.42, 0.36)
    put(14, 0.58, 0.36)
    put(15, 0.40, 0.46)
    put(16, 0.60, 0.46)
    put(23, 0.45, 0.50)
    put(24, 0.55, 0.50)
    put(25, 0.45, 0.70)
    put(26, 0.55, 0.70)
    put(27, 0.45, 0.90)
    put(28, 0.55, 0.90)
    put(29, 0.45, 0.93)
    put(30, 0.55, 0.93)
    put(31, 0.45, 0.95)
    put(32, 0.55, 0.95)
    return lms


def metrics_frame(**overrides):
    """Minimal metrics dict as the worker would emit after frame_metrics()."""
    base = {"trunk_x": 0.50, "trunk_y": 0.375, "com_x": 0.50, "com_y": 0.375,
            "body_height_norm": 0.80, "left_knee": 180.0}
    base.update(overrides)
    return base


class AngleTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(pose_math, "vision.pose_math must import stdlib-only")

    def test_straight_leg_is_180(self):
        self.assertAlmostEqual(
            pose_math.angle_deg([0.4, 0.5], [0.4, 0.7], [0.4, 0.9]), 180.0,
            places=9)

    def test_right_angle_at_knee(self):
        # hip above knee, ankle level with the knee -> 90 deg.
        self.assertAlmostEqual(
            pose_math.angle_deg([0.40, 0.50], [0.40, 0.70], [0.50, 0.70]),
            90.0, places=9)

    def test_bent_leg_expected_value(self):
        # 135 deg: the shank folds 45 deg away from the straight thigh line.
        angle = pose_math.angle_deg([0.40, 0.50], [0.40, 0.70],
                                    [0.40 + 0.2 * math.sin(math.radians(45)),
                                     0.70 + 0.2 * math.cos(math.radians(45))])
        self.assertAlmostEqual(angle, 135.0, places=6)

    def test_degenerate_and_missing_points(self):
        self.assertIsNone(pose_math.angle_deg([0.4, 0.5], [0.4, 0.5], [0.5, 0.6]))
        self.assertIsNone(pose_math.angle_deg(None, [0.4, 0.5], [0.5, 0.6]))
        self.assertIsNone(pose_math.angle_deg([float("nan"), 0.5],
                                              [0.4, 0.5], [0.5, 0.6]))

    def test_joint_angle_honours_visibility_gate(self):
        lms = standing()
        lms[25][2] = 0.49
        self.assertIsNone(pose_math.joint_angle(lms, 23, 25, 27))
        lms[25][2] = 0.50
        self.assertIsNotNone(pose_math.joint_angle(lms, 23, 25, 27))
        self.assertTrue(pose_math.visible(lms, 25))
        self.assertFalse(pose_math.visible(lms, 25, min_vis=0.99))

    def test_missing_visibility_is_treated_as_visible(self):
        self.assertAlmostEqual(
            pose_math.angle_deg([0.0, 0.0], [0.0, 1.0], [1.0, 1.0]), 90.0)


class AspectTests(unittest.TestCase):
    """H1 (QC 2026-10-21): angles must be aspect-corrected.

    MediaPipe normalises x by frame width and y by frame height, so a raw
    ``hypot(dx, dy)``/``atan2`` on normalised coordinates is only physical on a
    square frame.  Each fixture below is built in PIXELS and then divided by
    the frame size, so the expected angle is the physical one by construction.
    """

    WIDTH, HEIGHT = 1920, 1080

    def normalized(self, points):
        lms = [[0.5, 0.5, 0.0] for _ in range(33)]
        for index, (x_px, y_px) in points.items():
            lms[index] = [x_px / float(self.WIDTH),
                          y_px / float(self.HEIGHT), 0.99]
        return lms

    def test_physical_45_degree_knee_on_16_9_frame(self):
        knee = (600.0, 500.0)
        hip = (600.0, 200.0)
        shank = 300.0
        ankle = (knee[0] + shank * math.sin(math.radians(135.0)),
                 knee[1] + shank * math.cos(math.radians(135.0)))
        lms = self.normalized({23: hip, 25: knee, 27: ankle})

        corrected = pose_math.frame_metrics(lms, width=self.WIDTH,
                                            height=self.HEIGHT)
        self.assertAlmostEqual(corrected["left_knee"], 45.0, places=6)

        # The pre-fix behaviour, pinned so the regression is visible: the same
        # physical 45 deg knee read 29.36 deg on a 16:9 frame (the QC reported
        # 29.33 for its fixture).
        uncorrected = pose_math.frame_metrics(lms)
        self.assertAlmostEqual(uncorrected["left_knee"], 29.36, delta=0.05)

    def test_physical_10_degree_trunk_lean_on_16_9_frame(self):
        hip = (900.0, 700.0)
        length = 400.0
        shoulder = (hip[0] + length * math.sin(math.radians(10.0)),
                    hip[1] - length * math.cos(math.radians(10.0)))
        lms = self.normalized({23: hip, 24: hip, 11: shoulder, 12: shoulder})

        corrected = pose_math.frame_metrics(lms, width=self.WIDTH,
                                            height=self.HEIGHT)
        self.assertAlmostEqual(corrected["trunk_lean_deg"], 10.0, places=6)

        # Pre-fix reading on 16:9: 5.66 deg for a true 10 deg lean.
        uncorrected = pose_math.frame_metrics(lms)
        self.assertAlmostEqual(uncorrected["trunk_lean_deg"], 5.66, delta=0.02)

    def test_square_frame_is_unchanged(self):
        square = pose_math.frame_metrics(standing(), width=1080, height=1080)
        self.assertEqual(square, pose_math.frame_metrics(standing()))
        self.assertEqual(square["left_knee"], 180.0)

    def test_aspect_kwarg_matches_width_over_height(self):
        knee = (600.0, 500.0)
        hip = (600.0, 200.0)
        ankle = (812.13, 287.87)
        lms = self.normalized({23: hip, 25: knee, 27: ankle})
        by_pixels = pose_math.frame_metrics(lms, width=self.WIDTH,
                                            height=self.HEIGHT)
        by_ratio = pose_math.frame_metrics(
            lms, aspect=self.WIDTH / float(self.HEIGHT))
        self.assertEqual(by_pixels, by_ratio)
        # Angle between b->a = (0, -1) and b->c = (1, -0.5): atan2(1, 0.5)
        # without the aspect scaling, atan2(2, 0.5) with aspect = 2.
        self.assertAlmostEqual(pose_math.angle_deg(
            [0.0, 0.0], [0.0, 1.0], [1.0, 0.5], aspect=1.0),
            math.degrees(math.atan2(1.0, 0.5)), places=6)
        self.assertAlmostEqual(pose_math.angle_deg(
            [0.0, 0.0], [0.0, 1.0], [1.0, 0.5], aspect=2.0),
            math.degrees(math.atan2(2.0, 0.5)), places=6)

    def test_bad_dimensions_fall_back_to_square(self):
        base = pose_math.frame_metrics(standing())
        for kwargs in ({"width": 0, "height": 1080},
                       {"width": 1920, "height": None},
                       {"aspect": -1.0}, {"aspect": "wide"},
                       {"aspect": float("nan")}):
            self.assertEqual(pose_math.frame_metrics(standing(), **kwargs),
                             base, kwargs)


class FrameMetricTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(pose_math, "vision.pose_math must import stdlib-only")

    def test_standing_frame_metrics(self):
        m = pose_math.frame_metrics(standing())
        self.assertAlmostEqual(m["left_knee"], 180.0, places=6)
        self.assertAlmostEqual(m["right_knee"], 180.0, places=6)
        self.assertAlmostEqual(m["left_hip"], 180.0, places=6)
        self.assertAlmostEqual(m["left_ankle"], 180.0, places=6)
        self.assertAlmostEqual(m["trunk_lean_deg"], 0.0, places=6)
        self.assertAlmostEqual(m["shoulder_tilt"], 0.0, places=6)
        self.assertAlmostEqual(m["pelvic_tilt_deg"], 0.0, places=6)
        self.assertAlmostEqual(m["head_tilt"], 0.0, places=6)
        self.assertAlmostEqual(m["shoulder_width_norm"], 0.10, places=9)
        self.assertAlmostEqual(m["hip_width_norm"], 0.10, places=9)
        self.assertAlmostEqual(m["body_height_norm"], 0.85, places=9)
        self.assertAlmostEqual(m["com_x"], 0.50, places=6)
        # 0.35*0.50 + 0.35*0.25 + 0.30*0.375 = 0.375
        self.assertAlmostEqual(m["com_y"], 0.375, places=6)
        self.assertAlmostEqual(m["visibility_min"], 0.99, places=9)

    def test_trunk_lean_sign_is_camera_right_positive(self):
        lms = standing()
        for index in (11, 12):
            lms[index][0] += 0.08
        self.assertGreater(pose_math.frame_metrics(lms)["trunk_lean_deg"], 0.0)
        lean = pose_math.frame_metrics(lms)["trunk_lean_deg"]
        # atan2(0.08, 0.25) in degrees
        self.assertAlmostEqual(lean, math.degrees(math.atan2(0.08, 0.25)),
                               places=6)

        other = standing()
        for index in (11, 12):
            other[index][0] -= 0.08
        self.assertLess(pose_math.frame_metrics(other)["trunk_lean_deg"], 0.0)

    def test_pelvic_tilt_sign_right_hip_lower_positive(self):
        lms = standing()
        lms[24][1] = 0.54  # right hip drops 0.04 in image y
        tilt = pose_math.frame_metrics(lms)["pelvic_tilt_deg"]
        self.assertAlmostEqual(tilt, math.degrees(math.atan2(0.04, 0.10)),
                               places=6)
        self.assertGreater(tilt, 0.0)

        opposite = standing()
        opposite[23][1] = 0.54
        self.assertLess(pose_math.frame_metrics(opposite)["pelvic_tilt_deg"], 0.0)

    def test_occluded_joint_is_omitted_not_zeroed(self):
        lms = standing()
        lms[25][2] = 0.10
        m = pose_math.frame_metrics(lms)
        self.assertNotIn("left_knee", m)
        self.assertNotIn("left_hip", m)
        self.assertIn("right_knee", m)
        self.assertAlmostEqual(m["visibility_min"], 0.10, places=9)

    def test_side_view_collapsed_line_has_no_tilt(self):
        # Left/right collapse onto the same x: a tilt would be numerical noise.
        lms = standing()
        for left, right in ((11, 12), (23, 24), (7, 8)):
            lms[right][0] = lms[left][0]
            lms[right][1] = lms[left][1] + 0.01
        m = pose_math.frame_metrics(lms)
        self.assertNotIn("shoulder_tilt", m)
        self.assertNotIn("pelvic_tilt_deg", m)
        self.assertNotIn("head_tilt", m)

    def test_nonfinite_inputs_are_dropped(self):
        lms = standing()
        lms[0] = [float("nan"), 0.10, 0.99]
        m = pose_math.frame_metrics(lms)
        self.assertNotIn("body_height_norm", m)
        for key, value in m.items():
            self.assertIsInstance(value, float, key)
            self.assertTrue(math.isfinite(value), key)

    def test_empty_input(self):
        self.assertEqual(pose_math.frame_metrics(None), {})
        self.assertEqual(pose_math.frame_metrics([]), {})

    def test_pose_math_imports_no_imaging_deps(self):
        source = Path(pose_math.__file__).read_text(encoding="utf-8")
        for forbidden in ("import numpy", "import cv2", "import mediapipe",
                          "from numpy", "from cv2", "from mediapipe"):
            self.assertNotIn(forbidden, source)


class SwayTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(pose_math, "vision.pose_math must import stdlib-only")

    def test_none_until_ten_history_frames(self):
        history = [metrics_frame() for _ in range(9)]
        result = pose_math.sway_metrics(history, metrics_frame(trunk_x=0.60))
        self.assertEqual(result, {"trunk_sway_x": None, "trunk_sway_y": None})
        self.assertIsNone(pose_math.baseline_point(history))

    def test_relative_sway_scales_by_body_height(self):
        history = [metrics_frame(trunk_x=0.50, trunk_y=0.40)
                   for _ in range(10)]
        current = metrics_frame(trunk_x=0.58, trunk_y=0.36,
                                body_height_norm=0.80)
        result = pose_math.sway_metrics(history, current)
        self.assertAlmostEqual(result["trunk_sway_x"], 0.08 / 0.80, places=9)
        self.assertAlmostEqual(result["trunk_sway_y"], -0.04 / 0.80, places=9)

    def test_baseline_uses_only_last_150_frames(self):
        # 100 far-away frames followed by 150 on-baseline frames: the far ones
        # must have scrolled out of the window.
        history = [metrics_frame(trunk_x=0.20) for _ in range(100)]
        history += [metrics_frame(trunk_x=0.50) for _ in range(150)]
        base = pose_math.baseline_point(history)
        self.assertAlmostEqual(base[0], 0.50, places=9)

    def test_worker_frame_form_is_accepted(self):
        history = [{"metrics": metrics_frame()} for _ in range(10)]
        result = pose_math.sway_metrics(history, {"metrics": metrics_frame(
            trunk_x=0.54, body_height_norm=0.80)})
        self.assertAlmostEqual(result["trunk_sway_x"], 0.04 / 0.80, places=9)

    def test_missing_body_height_gives_no_sway(self):
        history = [metrics_frame() for _ in range(10)]
        current = metrics_frame()
        current.pop("body_height_norm")
        self.assertIsNone(pose_math.sway_metrics(history, current)["trunk_sway_x"])

    def test_sway_never_nan(self):
        history = [metrics_frame(trunk_x=float("inf")) for _ in range(12)]
        result = pose_math.sway_metrics(history, metrics_frame())
        self.assertEqual(result, {"trunk_sway_x": None, "trunk_sway_y": None})


class SummarizeTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(pose_math, "vision.pose_math must import stdlib-only")

    def clip(self, count, pose_every=1, start=0.50, drift=0.0, latency=True):
        rows = []
        for i in range(count):
            found = pose_every > 0 and i % pose_every == 0
            metrics = {} if not found else {
                "left_knee": 170.0 + i * 0.1,
                "right_knee": 168.0,
                "left_hip": 175.0,
                "right_hip": 176.0,
                "left_ankle": 100.0,
                "right_ankle": 101.0,
                "trunk_lean_deg": 1.0,
                "pelvic_tilt_deg": -2.0,
                "com_x": 0.5 + drift * i,
                "com_y": 0.4 + drift * i,
                "trunk_x": start + drift * i,
                "trunk_y": 0.4,
                "body_height_norm": 0.8,
            }
            row = {"frame_id": i, "t_mono_ms": i * 16, "pose_found": found,
                   "fps": 60.0, "metrics": metrics}
            if latency:
                row["latency_ms"] = 5.0 + i
            rows.append(row)
        return rows

    def test_short_clip_is_not_available(self):
        result = pose_math.summarize_frames(self.clip(5))
        self.assertEqual(result["frames"], 5)
        self.assertFalse(result["available"])
        self.assertIn("insufficient frames", result["notes"])
        self.assertTrue(result["estimates_only"])
        self.assertIn("mdc_note", result)
        self.assertNotIn("angles", result)

    def test_empty_clip(self):
        result = pose_math.summarize_frames([])
        self.assertFalse(result["available"])
        self.assertEqual(result["frames"], 0)

    def test_full_clip_summary(self):
        result = pose_math.summarize_frames(self.clip(120, drift=0.001))
        self.assertTrue(result["available"])
        self.assertEqual(result["pose_coverage_pct"], 100.0)
        self.assertAlmostEqual(result["fps_mean"], 60.0)
        self.assertIn("left_knee", result["angles"])
        stats = result["angles"]["left_knee"]
        self.assertAlmostEqual(stats["mean"], 170.0 + 0.1 * 119 / 2, places=6)
        self.assertGreater(stats["sd"], 0.0)
        self.assertAlmostEqual(stats["min"], 170.0, places=6)
        self.assertAlmostEqual(stats["max"], 170.0 + 0.1 * 119, places=6)
        self.assertGreater(result["sway"]["trunk_sway_x_range"], 0.0)
        self.assertIn("trunk_sway_y_range", result["sway"])
        self.assertIn("com_x_range", result["sway"])
        self.assertGreater(result["sway"]["com_x_range"], 0.0)
        self.assertAlmostEqual(result["latency_ms"]["mean"], 64.5, places=6)
        self.assertEqual(result["latency_ms"]["p95"], 118.0)
        self.assertTrue(result["estimates_only"])
        self.assertIn("markerless", result["mdc_note"].lower())
        self.assertNotIn("pose coverage below 70% - results descriptive only",
                         result["notes"])

    def test_low_coverage_note(self):
        rows = self.clip(100, pose_every=2)  # every other frame has a pose
        result = pose_math.summarize_frames(rows)
        self.assertAlmostEqual(result["pose_coverage_pct"], 50.0)
        self.assertIn("pose coverage below 70% - results descriptive only",
                      result["notes"])

    def test_missing_metrics_are_omitted(self):
        rows = [{"frame_id": i, "pose_found": False, "fps": 60.0,
                 "metrics": {}} for i in range(20)]
        result = pose_math.summarize_frames(rows)
        self.assertTrue(result["available"])
        self.assertEqual(result["pose_coverage_pct"], 0.0)
        self.assertNotIn("angles", result)
        self.assertNotIn("sway", result)
        self.assertNotIn("latency_ms", result)

    def test_every_number_is_finite_float(self):
        rows = self.clip(60)
        rows[3]["metrics"]["left_knee"] = float("nan")
        rows[4]["metrics"]["right_knee"] = float("inf")
        rows[5]["latency_ms"] = float("nan")
        result = pose_math.summarize_frames(rows)

        def walk(value, key=""):
            if isinstance(value, dict):
                for name, item in value.items():
                    walk(item, name)
            elif isinstance(value, list):
                for item in value:
                    walk(item, key)
            elif isinstance(value, bool) or value is None:
                return
            elif key == "frames":
                self.assertIsInstance(value, int)
            elif isinstance(value, (int, float)):
                self.assertIsInstance(value, float, value)
                self.assertTrue(math.isfinite(value), value)
            elif isinstance(value, str):
                return
            else:
                self.fail(f"unexpected type {type(value)}")

        walk(result)
        self.assertIn("left_knee", result["angles"])
        self.assertTrue(math.isfinite(result["angles"]["left_knee"]["mean"]))
        self.assertTrue(math.isfinite(result["angles"]["right_knee"]["mean"]))
        # A metric that is non-finite for EVERY frame is omitted entirely.
        for row in rows:
            row["metrics"]["left_hip"] = float("nan")
        cleaned = pose_math.summarize_frames(rows)
        self.assertNotIn("left_hip", cleaned["angles"])

    def test_single_sample_has_no_sd(self):
        rows = self.clip(12)
        for row in rows[1:]:
            row["pose_found"] = False
            row["metrics"] = {}
        result = pose_math.summarize_frames(rows)
        self.assertIn("left_knee", result["angles"])
        self.assertIsNone(result["angles"]["left_knee"]["sd"])


class SwayWindowTests(unittest.TestCase):
    """H10 (QC 2026-10-21): the sway baseline is TIME, not 150 frames."""

    def setUp(self):
        self.assertIsNotNone(pose_math, "vision.pose_math must import stdlib-only")

    def sway_clip(self, fps, seconds=20.0, freq=0.2, amplitude=0.02,
                  timestamps=True):
        rows = []
        for index in range(int(fps * seconds)):
            t = index / float(fps)
            x = 0.5 + amplitude * math.sin(2.0 * math.pi * freq * t)
            row = {"frame_id": index, "pose_found": True,
                   "fps": float(fps),
                   "metrics": {"trunk_x": x, "trunk_y": 0.4,
                               "com_x": x, "com_y": 0.4,
                               "body_height_norm": 0.8}}
            if timestamps:
                row["t_mono_ms"] = int(round(t * 1000.0))
            rows.append(row)
        return rows

    def test_window_is_seconds_times_measured_fps(self):
        slow = pose_math.summarize_frames(self.sway_clip(30.0))
        fast = pose_math.summarize_frames(self.sway_clip(60.0))
        self.assertEqual(slow["sway_baseline_s"], 2.5)
        self.assertEqual(slow["sway_baseline_frames"], 75.0)
        self.assertEqual(fast["sway_baseline_frames"], 150.0)
        self.assertEqual(slow["sway_baseline_fps"], 30.0)
        self.assertEqual(fast["sway_baseline_fps"], 60.0)

    def test_same_physical_sway_at_30_and_60_fps(self):
        slow = pose_math.summarize_frames(self.sway_clip(30.0))["sway"]
        fast = pose_math.summarize_frames(self.sway_clip(60.0))["sway"]
        residual_slow = slow["trunk_sway_x_range"]
        residual_fast = fast["trunk_sway_x_range"]
        self.assertGreater(residual_slow, 0.0)
        # Before H10 the 30 fps clip used a 5 s baseline and the 60 fps clip
        # 2.5 s, so these two ranges differed by the window, not the person.
        self.assertLess(abs(residual_slow - residual_fast) / residual_fast, 0.1,
                        (residual_slow, residual_fast))
        # Total range is reported separately and is fps-independent too.
        self.assertAlmostEqual(slow["trunk_x_range"], 0.04, delta=0.002)
        self.assertAlmostEqual(fast["trunk_x_range"], 0.04, delta=0.002)
        for key in ("trunk_y_range", "com_x_range", "com_y_range"):
            self.assertIn(key, slow, key)

    def test_missing_fps_uses_timestamps_for_the_window(self):
        rows = self.sway_clip(30.0)
        for row in rows:
            row.pop("fps")
        result = pose_math.summarize_frames(rows)
        self.assertAlmostEqual(result["sway_baseline_fps"], 30.0, delta=0.5)
        # t_mono_ms is integer-rounded in the real payload, so the median
        # interval gives 30.3 Hz, not exactly 30: one frame of slack.
        self.assertLess(abs(result["sway_baseline_frames"] - 75.0), 1.5)

    def test_no_rate_at_all_falls_back_and_says_so(self):
        rows = self.sway_clip(30.0, timestamps=False)
        for row in rows:
            row.pop("fps")
        result = pose_math.summarize_frames(rows)
        self.assertEqual(result["sway_baseline_frames"],
                         float(pose_math.SWAY_BASELINE_FRAMES))
        self.assertNotIn("sway_baseline_fps", result)
        self.assertTrue(any("frame rate unknown" in note
                            for note in result["notes"]), result["notes"])

    def test_baseline_frames_helper(self):
        self.assertEqual(pose_math.baseline_frames(2.5, 30.0), 75)
        self.assertEqual(pose_math.baseline_frames(2.5, 60.0), 150)
        self.assertEqual(pose_math.baseline_frames(2.5, None),
                         pose_math.SWAY_BASELINE_FRAMES)
        self.assertEqual(pose_math.baseline_frames(2.5, 0.0),
                         pose_math.SWAY_BASELINE_FRAMES)
        # Never below the minimum-frames gate, even for a tiny window.
        self.assertEqual(pose_math.baseline_frames(0.01, 60.0),
                         pose_math.SWAY_MIN_FRAMES)

    def test_sway_metrics_accepts_baseline_s_and_fps(self):
        history = [metrics_frame(trunk_x=0.50) for _ in range(40)]
        current = metrics_frame(trunk_x=0.58, body_height_norm=0.80)
        # 2.5 s at 16 fps = 40 frames, so the whole history is the baseline.
        result = pose_math.sway_metrics(history, current, baseline_s=2.5,
                                        fps=16.0)
        self.assertAlmostEqual(result["trunk_sway_x"], 0.08 / 0.80, places=9)


if __name__ == "__main__":
    unittest.main()
