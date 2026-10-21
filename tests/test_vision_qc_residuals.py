"""Residuals from the independent re-review (2026-10-21).

Covers: the worker's FLOAT `face.visible` presence proxy (1.0) must count as a
detection, zero keyword categories must not print "x0", sessions without any
audio key must not imply audio was recorded, short segments must be described
honestly, sync must state "not measured" instead of disappearing, and face
aggregate bookkeeping keys must never appear as face-proxy rows.
"""
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import service  # noqa: E402
from vision import vision_report as VR  # noqa: E402
from vision import vision_only_report as VO  # noqa: E402


class FaceVisibleTests(unittest.TestCase):
    def test_float_presence_proxy_counts_as_visible(self):
        rows = [{'face': {'visible': 1.0 if i < 3 else 0.0,
                          'mouth_open': 0.2 if i < 3 else None}}
                for i in range(5)]
        agg = service._face_aggregate(rows)
        self.assertTrue(agg['recorded'])
        self.assertEqual(agg['frames_with_face_data'], 5)
        self.assertEqual(agg['frames_visible'], 3)
        self.assertAlmostEqual(agg['mouth_open_mean'], 0.2)
        self.assertAlmostEqual(agg['visible_pct'], 60.0)

    def test_boolean_visibility_still_supported(self):
        rows = [{'face': {'visible': True, 'mouth_open': 0.1}},
                {'face': {'visible': False, 'mouth_open': None}}]
        agg = service._face_aggregate(rows)
        self.assertEqual(agg['frames_visible'], 1)

    def test_missing_visible_is_not_a_detection(self):
        agg = service._face_aggregate([{'face': {'mouth_open': 0.3}}])
        self.assertEqual(agg['frames_visible'], 0)
        self.assertIsNone(agg['mouth_open_mean'])


class ReportResidualTests(unittest.TestCase):
    def _vision_only(self, summary):
        return VO.render_vision_only_report(
            {'id': 'TESTONLY', 'label': 'residual test', 'summary': summary})

    def test_zero_keyword_categories_are_not_printed(self):
        summary = {'frames': 10, 'angle': {},
                   'segments': [], 'segments_recorded': 0,
                   'audio': {'recorded': True, 'windows': 4,
                             'speech_windows': 0,
                             'keyword_counts': {'pain': 0, 'comprehension': 0,
                                                'stop': 0}}}
        html = self._vision_only(summary)
        self.assertNotIn('\u00d7 0', html)
        self.assertIn('No keyword matches', html)

    def test_missing_audio_key_does_not_claim_audio_was_recorded(self):
        html = self._vision_only({'frames': 1})
        self.assertIn('No audio data is stored', html)

    def test_vision_only_report_states_sync_not_measured(self):
        html = self._vision_only({'frames': 1, 'sync': {
            'method': 'software', 'measured': False,
            'note': 'camera only: no plate stream in this session'}})
        self.assertIn('not measured', html.lower())
        self.assertIn('camera-only session', html.lower())

    def test_short_segments_are_described_honestly(self):
        summary = {'frames': 12, 'segments': [], 'segments_recorded': 2}
        html = self._vision_only(summary)
        self.assertIn('none had at least 10 frames', html)

    def test_sync_states_not_measured(self):
        trial = {'condition': 'test', 'vision_summary': {
            'frames': 10, 'pose_coverage_pct': 90.0,
            'angles': {'left_knee': {'mean': 170.0, 'sd': 1.0,
                                     'min': 169.0, 'max': 171.0}},
            'sway': {}, 'events': []}}
        html = VR.render_vision_section({'trials': [trial]})
        self.assertIn('not measured', html.lower())

    def test_face_bookkeeping_never_becomes_a_proxy_row(self):
        trial = {'condition': 'test', 'vision_summary': {
            'frames': 10, 'pose_coverage_pct': 90.0, 'angles': {}, 'sway': {},
            'face': {'frames_visible': 5, 'frames_total': 10,
                     'visible_pct': 50.0, 'frames_with_face_data': 10,
                     'mean_mouth_open': 0.2}}}
        html = VR.render_vision_section({'trials': [trial]})
        self.assertNotIn('frames_total', html)
        self.assertNotIn('visible_pct', html)


if __name__ == '__main__':
    unittest.main()
