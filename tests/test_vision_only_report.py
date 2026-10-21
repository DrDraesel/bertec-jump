"""TEST ONLY: the printable vision-only report.

Fixtures are plain dicts in the frozen v2 session.json shape.  The assertions
pin the page contract: complete standalone HTML with no JavaScript and no
external assets, every section present, every missing value an em dash, every
untrusted string escaped, no invented numbers, the 14 do-not-claim caveats and
an explicit 'not a diagnosis' footer.

No camera, no plate, no service import.
"""
import re
import unittest

from vision import norms
from vision import vision_only_report

EM_DASH = '\u2014'


def full_session():
    return {
        'id': '20261021T160000_abcdef12',
        'label': 'Ana <b>trial</b>',
        'started_at': '2026-10-21T16:00:00+00:00',
        'ended_at': '2026-10-21T16:06:30+00:00',
        'view': 'side',
        'age': 34,
        'sex': 'female',
        'notes': 'Athlete reported no pain.\nSecond line <script>alert(1)'
                 '</script>',
        'frames_count': 11700,
        'summary': {
            'frames': 11700,
            'pose_coverage_pct': 94.2,
            'fps_mean': 29.8,
            'angles': {
                'left_knee': {'mean': 9.4, 'sd': 1.2,
                              'min': 7.1, 'max': 11.9},
                'right_knee': {'mean': 9.1, 'sd': 1.1,
                               'min': 7.0, 'max': 11.2},
                'trunk_lean_deg': {'mean': 2.2, 'sd': 0.6},
            },
            'sway': {'trunk_sway_x_range': 0.021,
                     'trunk_sway_y_range': 0.034},
            'events': [
                {'event': 'low_visibility', 'severity': 'low',
                 'detail': 'left ankle', 't_rel': 4.2},
                {'event': 'operator_pain', 'detail': 'left knee',
                 't_rel': 12.5},
                {'type': 'note', 'note': 'needed a break', 't_rel': 20.0},
            ],
            'event_counts': {'low_visibility': 3, 'pose_lost': 1},
            'audio': {
                'windows': 660, 'speech_windows': 84,
                'mean_dbfs': -41.2, 'max_dbfs': -9.8,
                'stt': {'state': 'on',
                        'text': 'me duele un poco la rodilla',
                        'flags': ['pain']},
                'transcript_excerpts': ['\u00bfOtra vez?',
                                        'me duele un poco'],
            },
            'face': {
                'frames_visible': 11300,
                'mean_mouth_open': 0.12,
                'mean_brow_furrow': 0.31,
                'max_brow_furrow': 0.55,
            },
        },
    }


class TestFullSession(unittest.TestCase):
    """A complete session renders every documented block."""

    def setUp(self):
        self.html = vision_only_report.render_vision_only_report(full_session())

    def test_complete_standalone_document(self):
        self.assertTrue(self.html.startswith('<!DOCTYPE html>'))
        self.assertTrue(self.html.rstrip().endswith('</html>'))
        self.assertIn('<main class="report">', self.html)
        self.assertIn('<style>', self.html)

    def test_header_metadata(self):
        self.assertIn('Vision-only assessment \u2013 Ana &lt;b&gt;trial'
                      '&lt;/b&gt;', self.html)
        self.assertIn('<h1>', self.html)
        self.assertIn('2026-10-21 16:00:00', self.html)
        self.assertIn('2026-10-21 16:06:30', self.html)
        self.assertIn('<dd>side</dd>', self.html)
        self.assertIn('<dd>34</dd>', self.html)
        self.assertIn('<dd>female</dd>', self.html)
        self.assertIn('Research and training use only', self.html)
        self.assertIn('not a medical device', self.html)

    def test_quality_block(self):
        self.assertIn('<h2>Recording quality</h2>', self.html)
        self.assertIn('<td>Frames processed</td><td class="num">11700</td>',
                      self.html)
        self.assertIn('94.2 % of frames', self.html)
        self.assertIn('29.8 fps', self.html)
        self.assertIn('390.0 s', self.html)

    def test_kinematics_table(self):
        self.assertIn('<h2>Kinematics (pose estimate)</h2>', self.html)
        for header in ('Measure', 'Mean \u00b1 SD', 'Min', 'Max'):
            self.assertIn('<th>{}</th>'.format(header), self.html)
        self.assertIn('<td>Left knee</td>', self.html)
        self.assertIn('<td class="num">9.4 \u00b1 1.2</td>', self.html)
        self.assertIn('<td class="num">7.1</td>', self.html)
        self.assertIn('<td class="num">11.9</td>', self.html)
        self.assertIn('<td>Trunk lean</td>', self.html)

    def test_sway_ranges(self):
        self.assertIn('Trunk sway X range', self.html)
        self.assertIn('0.021', self.html)
        self.assertIn('Trunk sway Y range', self.html)
        self.assertIn('0.034', self.html)

    def test_audio_block(self):
        self.assertIn('<h2>Audio observations</h2>', self.html)
        self.assertIn('<td>Audio windows</td><td class="num">660</td>',
                      self.html)
        self.assertIn('<td>Windows with speech activity</td>'
                      '<td class="num">84</td>', self.html)
        self.assertIn('12.7 %', self.html)
        self.assertIn('-41.2 dBFS', self.html)
        self.assertIn('-9.8 dBFS', self.html)
        self.assertIn('<td>Speech-to-text state</td><td class="num">on</td>',
                      self.html)
        self.assertIn('me duele un poco la rodilla', self.html)
        self.assertIn('\u00bfOtra vez?', self.html)
        self.assertIn('keyword match: pain (local STT, may be wrong) '
                      '× 1', self.html)
        self.assertIn('Local machine transcription', self.html)

    def test_face_block(self):
        self.assertIn('<h2>Face-landmark proxies (front view)</h2>', self.html)
        self.assertIn('<td>Frames with a face</td>'
                      '<td class="num">11300</td>', self.html)
        self.assertIn('96.6 %', self.html)
        self.assertIn('<td>Mean mouth opening</td>'
                      '<td class="num">0.120</td>', self.html)
        self.assertIn('<td>Mean brow furrow</td>'
                      '<td class="num">0.310</td>', self.html)
        self.assertIn('<td>Brow furrow (max)</td>'
                      '<td class="num">0.550</td>', self.html)
        self.assertIn('not an emotion measurement, not a pain measure',
                      self.html)

    def test_observation_log(self):
        self.assertIn('<h2>Observation log</h2>', self.html)
        self.assertIn('<th>Severity</th>', self.html)
        self.assertIn('Low landmark visibility', self.html)
        self.assertIn('4.2 s', self.html)
        self.assertIn('left ankle', self.html)
        self.assertIn('Recorded observation counts: Low landmark visibility '
                      '3; Pose lost 1', self.html)

    def test_operator_marks(self):
        self.assertIn('<h2>Operator marks</h2>', self.html)
        self.assertIn('operator mark: pain', self.html)
        self.assertIn('left knee', self.html)
        self.assertIn('12.5 s', self.html)
        self.assertIn('operator mark: note', self.html)
        self.assertIn('needed a break', self.html)
        self.assertIn('They are not measurements and they are not a '
                      'diagnosis', self.html)

    def test_notes_are_escaped_and_line_broken(self):
        self.assertIn('<h2>Session notes</h2>', self.html)
        self.assertIn('Athlete reported no pain.<br>Second line', self.html)
        self.assertNotIn('<script>', self.html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', self.html)

    def test_limitations_and_caveats(self):
        self.assertIn('<h2>Limitations</h2>', self.html)
        self.assertIn('15.49 deg', self.html)
        self.assertIn('ICC of 0.136', self.html)
        self.assertIn('no 3-D reconstruction', self.html)
        self.assertIn('not a diagnosis', self.html)
        self.assertIn('physician-led', self.html)
        self.assertIn('do not detect pain', self.html)
        self.assertIn('<h2>What this report does not claim</h2>', self.html)
        self.assertEqual(self.html.count('<li>Do not'), 14)
        self.assertEqual(len(norms.caveats()), 14)

    def test_explicit_footer(self):
        self.assertIn('<footer class="report-footer">', self.html)
        self.assertIn('This report is not a diagnosis.', self.html)
        self.assertIn('does not clear anyone for sport', self.html)
        self.assertIn('No data leaves this computer', self.html)


class TestPartialAndEmpty(unittest.TestCase):
    """Missing fields become em dashes, sections stay in place."""

    def test_partial_summary(self):
        html = vision_only_report.render_vision_only_report(
            {'id': 'x', 'label': 'Partial', 'summary': {'frames': 600}})
        for heading in ('Recording quality', 'Kinematics (pose estimate)',
                        'Audio observations',
                        'Face-landmark proxies (front view)',
                        'Observation log', 'Operator marks', 'Limitations',
                        'What this report does not claim'):
            self.assertIn('<h2>{}</h2>'.format(heading), html)
        self.assertIn('<td>Frames processed</td><td class="num">600</td>',
                      html)
        # H3 residual: a session without any audio key must not imply audio was
        # recorded; the page states that no audio data is stored.
        self.assertIn('No audio data is stored', html)
        self.assertIn('No face data was recorded for this session', html)
        self.assertIn('No automatic observations were logged', html)
        self.assertIn('No operator marks were recorded', html)
        self.assertIn(EM_DASH, html)
        self.assertEqual(html.count('<li>Do not'), 14)

    def test_empty_dict_and_none(self):
        for payload in ({}, None, 'nonsense', []):
            html = vision_only_report.render_vision_only_report(payload)
            self.assertTrue(html.startswith('<!DOCTYPE html>'))
            self.assertTrue(html.rstrip().endswith('</html>'))
            self.assertIn('>{}<'.format(EM_DASH), html)
            self.assertEqual(html.count('<li>Do not'), 14)
            self.assertNotIn('None', html)
            self.assertIn('This report is not a diagnosis.', html)

    def test_junk_shapes_do_not_crash(self):
        html = vision_only_report.render_vision_only_report({
            'label': {'nested': 'label'},
            'age': 'unknown', 'sex': 7, 'view': ['side'],
            'notes': 42,
            'summary': {'frames': 'many', 'angles': 'none',
                        'sway': [1, 2], 'events': 'event',
                        'audio': 'on', 'face': ['face']},
        })
        self.assertTrue(html.rstrip().endswith('</html>'))
        self.assertIn(EM_DASH, html)
        self.assertNotIn('nested', html)
        self.assertNotIn('None', html)
        self.assertEqual(html.count('<li>Do not'), 14)

    def test_no_invented_numbers_for_an_empty_summary(self):
        html = vision_only_report.render_vision_only_report({})
        self.assertNotIn('0.0 fps', html)
        self.assertNotIn('0.0 %', html)
        self.assertIn('<td>Mean processing rate</td><td class="num">{}</td>'
                      .format(EM_DASH), html)


class TestAudioEdgeCases(unittest.TestCase):
    """Speech share is only computed from two recorded counts."""

    def _html(self, audio):
        return vision_only_report.render_vision_only_report(
            {'summary': {'frames': 100, 'audio': audio}})

    def test_zero_windows_does_not_divide(self):
        html = self._html({'windows': 0, 'speech_windows': 0})
        self.assertIn('<td>Speech share</td><td class="num">{}</td>'
                      .format(EM_DASH), html)

    def test_speech_share_math(self):
        html = self._html({'windows': 200, 'speech_windows': 50})
        self.assertIn('<td>Speech share</td><td class="num">25.0 %</td>', html)

    def test_stt_unavailable_state(self):
        html = self._html({'windows': 10, 'speech_windows': 1,
                           'stt': {'state': 'unavailable'}})
        self.assertIn('<td class="num">unavailable</td>', html)
        self.assertIn('No transcript excerpts were stored', html)

    def test_flags_as_counts(self):
        html = self._html({'windows': 10, 'speech_windows': 1,
                           'flags': {'stop': 2}})
        self.assertIn('keyword match: stop (local STT, may be wrong) '
                      '× 2', html)

    def test_no_flags_message(self):
        html = self._html({'windows': 10, 'speech_windows': 1})
        self.assertIn('No keyword matches were recorded', html)


class TestEscaping(unittest.TestCase):
    """Every untrusted string is escaped; nothing executes."""

    def test_all_text_fields_escaped(self):
        payload = {
            'label': '<script>alert("label")</script>',
            'view': '<svg onload=alert(1)>',
            'notes': '<img src=x onerror=alert(1)>',
            'summary': {
                'events': [{'event': 'low_visibility',
                            'detail': '<script>alert("event")</script>',
                            't_rel': 1.0}],
                'audio': {'windows': 10, 'speech_windows': 1,
                          'transcript_excerpts': ['<b>hola</b>']},
            },
            'operator_marks': [{'type': 'note',
                                'note': '<iframe src=javascript:alert(1)>'}],
        }
        html = vision_only_report.render_vision_only_report(payload)
        self.assertNotIn('<script', html)
        self.assertNotIn('<svg', html)
        self.assertNotIn('<img', html)
        self.assertNotIn('<iframe', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;', html)
        self.assertIn('&lt;b&gt;hola&lt;/b&gt;', html)

    def test_no_javascript_and_no_external_assets(self):
        html = vision_only_report.render_vision_only_report(full_session())
        self.assertNotIn('<script', html)
        self.assertNotIn('<link', html)
        self.assertNotIn('src=', html)
        self.assertNotIn('href=', html)
        self.assertNotIn('http://', html)
        self.assertNotIn('https://', html)

    def test_no_diagnostic_vocabulary(self):
        html = vision_only_report.render_vision_only_report(full_session())
        self.assertNotIn('normal', html.lower())
        self.assertNotIn('abnormal', html.lower())
        self.assertNotIn('impaired', html.lower())
        self.assertNotIn('pathological', html.lower())


class TestDurationAndDates(unittest.TestCase):
    """Duration comes from a recorded field or the two timestamps only."""

    def test_explicit_duration_wins(self):
        html = vision_only_report.render_vision_only_report({
            'started_at': '2026-10-21T16:00:00+00:00',
            'ended_at': '2026-10-21T16:06:30+00:00',
            'summary': {'frames': 10, 'duration_s': 30.0},
        })
        self.assertIn('<td class="num">30.0 s</td>', html)
        self.assertNotIn('390.0 s', html)

    def test_missing_timestamps_are_a_dash(self):
        html = vision_only_report.render_vision_only_report(
            {'summary': {'frames': 10}})
        self.assertIn('<td>Recorded duration</td><td class="num">{}</td>'
                      .format(EM_DASH), html)
        self.assertIn('<dt>Started</dt><dd>{}</dd>'.format(EM_DASH), html)

    def test_end_before_start_is_a_dash(self):
        html = vision_only_report.render_vision_only_report({
            'started_at': '2026-10-21T16:06:30+00:00',
            'ended_at': '2026-10-21T16:00:00+00:00',
            'summary': {'frames': 10},
        })
        self.assertIn('<td>Recorded duration</td><td class="num">{}</td>'
                      .format(EM_DASH), html)


class TestStoredSessionShape(unittest.TestCase):
    """The stored session.json shape (frames int, events_log, type keys)."""

    def test_events_log_and_type_keys(self):
        session = {
            'id': '20261021T160000_abcdef12',
            'frames': 900,
            'events': 2,
            'event_counts': {'low_visibility': 1, 'pain': 1},
            'summary': {'angles': {'left_knee': {'mean': 9.0, 'sd': 1.0}}},
            'events_log': [
                {'type': 'low_visibility', 't_rel': 1.2,
                 'detail': 'left ankle', 'source': 'worker'},
                {'type': 'pain', 'note': 'left knee', 't_rel': 20.0,
                 'source': 'operator'},
            ],
        }
        html = vision_only_report.render_vision_only_report(session)
        self.assertIn('<td>Frames processed</td><td class="num">900</td>',
                      html)
        self.assertIn('Low landmark visibility', html)
        self.assertIn('left ankle', html)
        self.assertIn('operator mark: pain', html)
        self.assertIn('left knee', html)
        self.assertIn('Recorded observation counts', html)
        self.assertEqual(html.count('Low landmark visibility'), 2)  # log+counts

    def test_events_count_integer_is_not_a_list(self):
        session = {'frames': 10, 'events': 7, 'events_log': [],
                   'summary': {'frames': 10}}
        html = vision_only_report.render_vision_only_report(session)
        self.assertIn('No automatic observations were logged', html)
        self.assertNotIn('7.0 s', html)


class TestStructure(unittest.TestCase):
    """Section anchors are stable and unique for print/export links."""

    def test_section_ids(self):
        html = vision_only_report.render_vision_only_report(full_session())
        for anchor in ('quality', 'kinematics', 'audio', 'face',
                       'observations', 'operator-marks', 'notes',
                       'limitations', 'caveats'):
            self.assertEqual(html.count('id="{}"'.format(anchor)), 1)
        self.assertEqual(len(re.findall(r'<section class="report-section"',
                                        html)), 9)


class TestSegmentedSession(unittest.TestCase):
    """H7: a segmented vision-only session renders one row per segment."""

    def _session(self):
        return {'label': 'Battery', 'summary': {
            'frames': 900, 'fps_mean': 30.0,
            'segments': [
                {'test_id': 'double_leg_eo', 'limb': 'both', 'trial': 1,
                 'frames': 300, 'pose_coverage_pct': 96.0,
                 'angles': {'left_knee': {'mean': 10.0, 'sd': 1.0}},
                 'sway': {'trunk_sway_x_range': 0.01}},
                {'test_id': 'single_leg_left', 'limb': 'left', 'trial': 2,
                 'frames': 300, 'pose_coverage_pct': 88.0,
                 'angles': {'left_knee': {'mean': 14.0, 'sd': 2.0}},
                 'sway': {}},
            ]}}

    def test_one_row_per_segment_with_test_limb_trial(self):
        html = vision_only_report.render_vision_only_report(self._session())
        for header in ('Test', 'Limb', 'Trial', 'Frames', 'Coverage %',
                       'Left knee', 'Trunk lean'):
            self.assertIn('<th>{}</th>'.format(header), html)
        self.assertIn('<td>double_leg_eo</td>', html)
        self.assertIn('<td>single_leg_left</td>', html)
        self.assertIn('<td>left</td>', html)
        self.assertIn('<td class="num">10.0 \u00b1 1.0</td>', html)
        self.assertIn('<td class="num">14.0 \u00b1 2.0</td>', html)
        self.assertIn('One row per stored test segment', html)
        self.assertNotIn('whole clip was summarised as one block', html)

    def test_unsegmented_session_says_one_block(self):
        html = vision_only_report.render_vision_only_report(full_session())
        self.assertIn('No per-test segmentation was stored', html)
        self.assertIn('whole clip was summarised as one block', html)


class TestHonestLabels(unittest.TestCase):
    """H2 / L3 / M5: no valgus claim, processing rate, labelled keyword hits."""

    def test_rate_is_processing_not_camera(self):
        html = vision_only_report.render_vision_only_report(full_session())
        self.assertIn('<td>Mean processing rate</td>', html)
        self.assertNotIn('Mean camera rate', html)
        self.assertIn('not the camera\'s capture rate', html)

    def test_no_valgus_row_or_claim(self):
        html = vision_only_report.render_vision_only_report(full_session())
        self.assertNotIn('Knee valgus', html)
        self.assertNotIn('change scores only', html)
        self.assertIn('frontal-plane knee valgus is not computed at all', html)

    def test_comprehension_flag_is_labelled_and_false_pain_is_not_counted(self):
        flags = {'pain': False, 'comprehension': True, 'stop': False,
                 'matches': [{'category': 'comprehension',
                              'phrase': 'otra vez'}]}
        html = vision_only_report.render_vision_only_report(
            {'summary': {'frames': 100,
                         'audio': {'windows': 10, 'speech_windows': 2,
                                   'stt': {'state': 'on', 'flags': flags}}}})
        self.assertIn('comprehension/confusion phrase keyword match '
                      '(local STT, may be wrong) \u00d7 1', html)
        self.assertNotIn('keyword match: pain', html)
        self.assertNotIn('keyword match: stop', html)


if __name__ == '__main__':
    unittest.main()
