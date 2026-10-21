"""Integration: the printable protocol report renders the camera-vision section.

Covers the CTO wiring between stored trial records (vision_summary / cop_com),
build_protocol_report() and render_protocol_report_page(): a session that
stored vision data gains the 'Camera-derived kinematics' section; a session
without it stays unchanged (no empty section, no stray heading).
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import service  # noqa: E402

from metrics import PROCESSING_VERSION  # noqa: E402

SID = "20261021T160000_abcdef12"

ANGLES = {
    "left_knee": {"mean": 171.2, "sd": 1.4, "min": 168.9, "max": 173.7},
    "right_knee": {"mean": 170.4, "sd": 1.1, "min": 168.2, "max": 172.9},
    "left_hip": {"mean": 176.7, "sd": 0.8, "min": 175.1, "max": 178.0},
    "right_hip": {"mean": 175.9, "sd": 0.9, "min": 174.0, "max": 177.4},
    "left_ankle": {"mean": 104.3, "sd": 2.0, "min": 100.1, "max": 108.2},
    "right_ankle": {"mean": 103.8, "sd": 1.9, "min": 99.7, "max": 107.5},
    "trunk_lean_deg": {"mean": 2.6, "sd": 0.7, "min": 1.4, "max": 3.9},
    "pelvic_tilt_deg": {"mean": 1.1, "sd": 0.5, "min": 0.2, "max": 2.0},
}

VISION_SUMMARY = {
    "available": True,
    "frames": 240,
    "pose_coverage_pct": 94.5,
    "fps_mean": 29.6,
    "angles": ANGLES,
    "sway": {"trunk_sway_x_range": 0.012, "trunk_sway_y_range": 0.021},
    "latency_ms": {"mean": 11.2, "p95": 18.4},
    "sync": {"method": "software", "offset_ms_estimate": 12.5,
             "jitter_ms_sd": 3.1, "plate_span_s": 30.0, "vision_span_s": 29.7},
    "flags": [],
    "notes": [],
}

COP_COM = {
    "available": True,
    "method": "gravity-line projection (low-pass COP)",
    "cutoff_hz": 0.5,
    "fs_hz": 100.0,
    "samples": 2600,
    "cop_com_rms_mm": 3.2,
    "cop_com_ap_rms_mm": 2.8,
    "com_rms_mm": 4.5,
    "cop_com_velocity_mm_s": 1.7,
}


def trial_document(with_vision=True):
    document = {
        "condition_id": "c1_eo_firm",
        "condition_index": 0,
        "trial_index": 0,
        "n_samples": 3000,
        "duration_s": 30.0,
        "flags": [],
        "assessable_raw": True,
        "metrics": {"assessable": True, "duration_s": 30.0, "n_samples": 3000,
                    "path_length_mm": 118.2, "mean_velocity_mm_s": 3.94,
                    "range_ap_mm": 11.1, "range_ml_mm": 8.4,
                    "rms_ap_mm": 1.9, "rms_ml_mm": 1.6,
                    "ellipse95_area_mm2": 72.5},
        "notes": "",
        "kept": True,
        "session_id": SID,
        "protocol_id": "mctsib",
        "processing_version": PROCESSING_VERSION,
    }
    if with_vision:
        document["vision_summary"] = VISION_SUMMARY
        document["cop_com"] = COP_COM
    return document


def session_document(with_vision=True, age=34, sex="female"):
    return {
        "id": SID,
        "protocol_id": "mctsib",
        "processing_version": PROCESSING_VERSION,
        "protocol_label": "TEST ONLY mCTSIB session",
        "athlete": "TEST ONLY athlete",
        "athlete_age": age,
        "athlete_sex": sex,
        "started_at": "2026-10-21T16:00:00+00:00",
        "completed_at": "2026-10-21T16:05:00+00:00",
        "quality": "complete",
        "runner_status": "complete",
        "kept_trials": [{"file": "trial_01.json", "condition_id": "c1_eo_firm",
                         "condition_index": 0, "trial_index": 0, "n_samples": 3000,
                         "duration_s": 30.0}],
        "summary": {},
        "safety": {"message": "TEST ONLY safety note", "weight_limit_kg": 227.0},
        "methodology": {"processing": "TEST ONLY processing note",
                        "cop": "TEST ONLY cop note", "rates": "TEST ONLY rates note"},
    }


class VisionReportIntegrationTests(unittest.TestCase):
    def _build(self, with_vision=True, age=34, sex="female"):
        tmp = tempfile.mkdtemp(prefix="rt_vision_report_")
        root = Path(tmp) / "protocols"
        folder = root / SID
        folder.mkdir(parents=True)
        (folder / "trial_01.json").write_text(
            json.dumps(trial_document(with_vision)), encoding="utf-8")
        (folder / "session.json").write_text(
            json.dumps(session_document(with_vision, age, sex)), encoding="utf-8")
        report = service.build_protocol_report(root, SID)
        page = service.render_protocol_report_page(report, SID)
        return report, page

    def test_vision_section_is_rendered(self):
        report, page = self._build()
        vision = report.get("vision") or {}
        self.assertEqual(len(vision.get("trials") or []), 1, vision)
        self.assertEqual(vision["trials"][0]["condition"],
                         "Condition 1 - eyes open, firm surface")
        self.assertEqual(vision["athlete_age"], 34)
        self.assertEqual(vision["athlete_sex"], "female")
        self.assertIn("Camera-derived kinematics", page)
        self.assertIn("Per-test kinematics", page)
        self.assertIn("Knee L", page)
        self.assertIn("COP\u2013COM (low-pass estimate; not validated GLP)",
                      page)
        self.assertIn("Longitudinal change (MDC)", page)
        self.assertIn("What this section does not claim", page)
        self.assertIn("not a diagnosis", page)

    def test_session_without_vision_stays_unchanged(self):
        report, page = self._build(with_vision=False)
        self.assertEqual(report.get("vision"), {})
        self.assertNotIn("Camera-derived kinematics", page)

    def test_vision_without_age_sex_skips_reference_comparison(self):
        _report, page = self._build(age=None, sex=None)
        self.assertIn("Camera-derived kinematics", page)
        self.assertIn("Age and sex were not supplied", page)
        self.assertNotIn("within published reference band", page)

    def test_plate_only_cop_com_renders_without_camera(self):
        """H9: a plate-only trial with COP-COM still renders that block."""
        tmp = tempfile.mkdtemp(prefix="rt_vision_copcom_")
        root = Path(tmp) / "protocols"
        folder = root / SID
        folder.mkdir(parents=True)
        trial = trial_document(with_vision=False)
        trial["cop_com"] = COP_COM
        (folder / "trial_01.json").write_text(json.dumps(trial), encoding="utf-8")
        (folder / "session.json").write_text(
            json.dumps(session_document(with_vision=False)), encoding="utf-8")
        report = service.build_protocol_report(root, SID)
        page = service.render_protocol_report_page(report, SID)
        self.assertEqual(len(report.get("vision", {}).get("trials") or []), 1)
        self.assertIn("COP\u2013COM", page)
        self.assertIn("low-pass COP", page)
        self.assertNotIn("Camera-derived kinematics", page)

    def test_other_agents_trial_fields_untouched(self):
        """The additive vision keys must not disturb the stored metric view."""
        report, _page = self._build(with_vision=True)
        condition = next(c for c in report["conditions"]
                         if c["condition_id"] == "c1_eo_firm")
        trial = condition["trials"][0]
        self.assertEqual(trial["metrics"]["mean_velocity_mm_s"], 3.94)
        self.assertEqual(trial["n_samples"], 3000)
        self.assertIn("vision_summary", trial)


if __name__ == "__main__":
    unittest.main()
