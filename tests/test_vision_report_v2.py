"""TEST ONLY: the plate report's v2 'Session observations' block.

The vision section keeps its v1 contract (565-style fixtures still render, and
a session without audio/face/events is unchanged).  These tests pin the
additive v2 block: operator marks, audio activity, face-landmark proxies and
worker events appear only when the session carries them, every value is
labelled observational, and every untrusted string is escaped.

No camera, no plate, no service import: the fixtures are plain dicts.
"""
import unittest

from vision import norms, vision_report

EM_DASH = '\u2014'


def metric(mean, sd=None):
    payload = {'mean': mean}
    if sd is not None:
        payload['sd'] = sd
    return payload


def vision_summary(**overrides):
    """A v1 summary - no audio/face/events unless a test adds them."""
    base = {
        'frames': 600,
        'pose_coverage_pct': 93.4,
        'fps_mean': 29.5,
        'angles': {
            'left_knee': metric(12.3, 2.1),
            'right_knee': metric(11.9, 2.4),
            'trunk_lean_deg': metric(3.4, 1.2),
        },
        'sway': {'trunk_sway_x_range': 0.021, 'trunk_sway_y_range': 0.034},
        'flags': [],
        'notes': [],
    }
    base.update(overrides)
    return base


def v2_summary(**overrides):
    base = vision_summary(
        audio={'windows': 120, 'speech_windows': 18,
               'mean_dbfs': -38.2, 'max_dbfs': -11.4,
               'stt': {'state': 'on', 'text': 'me duele la rodilla',
                       'flags': ['pain']}},
        face={'frames_visible': 412, 'mean_mouth_open': 0.21,
              'mean_brow_furrow': 0.4},
        events=[
            {'event': 'low_visibility', 'severity': 'low',
             'detail': 'left ankle', 't_rel': 4.2},
            {'event': 'audio_loud', 'severity': 'medium',
             'detail': '-11 dBFS', 't_rel': 9.0},
        ],
        event_counts={'low_visibility': 3, 'pose_lost': 1},
    )
    base.update(overrides)
    return base


def session(summary=None, **overrides):
    payload = {
        'athlete_age': 45,
        'athlete_sex': 'female',
        'trials': [{'condition': 'eo_firm',
                    'vision_summary': summary if summary is not None
                    else v2_summary(),
                    'vision_rows': []}],
    }
    payload.update(overrides)
    return payload


class TestBlockAbsentWithoutObservations(unittest.TestCase):
    """A v1 session (or one with no observations) renders exactly as before."""

    def setUp(self):
        self.html = vision_report.render_vision_section(
            session(summary=vision_summary()))

    def test_no_observations_block(self):
        self.assertNotIn('Session observations', self.html)
        self.assertNotIn('Operator marks', self.html)
        self.assertNotIn('Audio activity', self.html)
        self.assertNotIn('Face-landmark proxies', self.html)
        self.assertNotIn('Automatic observations', self.html)

    def test_v1_sections_still_render(self):
        self.assertIn('Per-test kinematics', self.html)
        self.assertIn('Reference comparison (age/sex)', self.html)
        self.assertIn('Longitudinal change (MDC)', self.html)
        self.assertIn('What this section does not claim', self.html)
        self.assertEqual(self.html.count('<li>Do not'), 14)

    def test_trial_with_only_angles_has_no_block(self):
        payload = {'trials': [{'condition': 'eo_firm',
                               'vision_summary': {'frames': 100}}]}
        html = vision_report.render_vision_section(payload)
        self.assertNotIn('Session observations', html)


class TestBlockPresent(unittest.TestCase):
    """Audio, face, events and marks all reach the printable block."""

    def setUp(self):
        self.html = vision_report.render_vision_section(session())

    def test_heading_and_observational_lede(self):
        self.assertIn('<h3>Session observations</h3>', self.html)
        self.assertIn('Observational context only', self.html)
        self.assertIn('None of these is a measurement and none is a '
                      'diagnosis', self.html)
        self.assertIn('Speech-to-text runs locally and its output may be '
                      'wrong', self.html)

    def test_audio_counts_and_levels(self):
        self.assertIn('Audio windows: 120 (18 with speech activity, '
                      '15.0 %)', self.html)
        self.assertIn('Level: mean -38.2 dBFS, max -11.4 dBFS', self.html)
        self.assertIn('Speech-to-text: on', self.html)
        self.assertIn('keyword match: pain (local STT, may be wrong) x1', self.html)

    def test_transcript_is_labelled_and_rendered(self):
        self.assertIn('Transcript excerpts (local speech-to-text, may be '
                      'wrong)', self.html)
        self.assertIn('me duele la rodilla', self.html)

    def test_face_proxies_are_counts_and_proxy_labelled(self):
        self.assertIn('Frames with a face: 412 of 600 (68.7 %)', self.html)
        self.assertIn('Mean mouth opening: 0.210', self.html)
        self.assertIn('Mean brow furrow: 0.400', self.html)
        self.assertIn('landmark-geometry proxies', self.html)
        self.assertIn('not an emotion measurement and not a pain measure',
                      self.html)

    def test_event_log_and_counts(self):
        self.assertIn('<h4>Automatic observations</h4>', self.html)
        self.assertIn('Low landmark visibility', self.html)
        self.assertIn('Loud audio', self.html)
        self.assertIn('4.2 s', self.html)
        self.assertIn('Recorded observation counts: Low landmark visibility '
                      '3; Pose lost 1', self.html)

    def test_events_are_time_ordered(self):
        self.assertLess(self.html.index('4.2 s'), self.html.index('9.0 s'))

    def test_mark_rendered_as_operator_mark(self):
        payload = session()
        payload['operator_marks'] = [
            {'type': 'pain', 'note': 'left knee', 't_rel': 12.5}]
        html = vision_report.render_vision_section(payload)
        self.assertIn('operator mark: pain', html)
        self.assertIn('left knee', html)

    def test_no_diagnostic_vocabulary(self):
        self.assertNotIn('normal', self.html.lower())
        self.assertNotIn('abnormal', self.html.lower())
        self.assertNotIn('impaired', self.html.lower())


class TestMarkEncodings(unittest.TestCase):
    """Both documented operator-mark encodings reach the same table."""

    def test_plain_mark_dicts(self):
        payload = session()
        payload['operator_marks'] = [
            {'type': 'pain', 'note': 'left knee', 't_rel': 12.5},
            {'type': 'did_not_understand', 'note': 'repeat instructions',
             't_rel': 3.0},
        ]
        html = vision_report.render_vision_section(payload)
        self.assertIn('operator mark: pain', html)
        self.assertIn('operator mark: did not understand', html)
        self.assertIn('repeat instructions', html)
        self.assertIn('12.5 s', html)

    def test_event_encoded_marks(self):
        payload = session()
        payload['events'] = [
            {'event': 'operator_pain', 'detail': 'right ankle', 't_rel': 7.5},
            {'event': 'mark_note', 'detail': 'camera moved', 't_rel': 8.0},
        ]
        html = vision_report.render_vision_section(payload)
        self.assertIn('operator mark: pain', html)
        self.assertIn('right ankle', html)
        self.assertIn('operator mark: note', html)
        self.assertIn('camera moved', html)

    def test_marks_are_not_duplicated_in_the_event_log(self):
        payload = session()
        payload['operator_marks'] = []
        payload['events'] = [{'event': 'operator_pain', 'detail': 'x',
                              't_rel': 1.0}]
        html = vision_report.render_vision_section(payload)
        self.assertEqual(html.count('operator mark: pain'), 1)


class TestEventRowsFallback(unittest.TestCase):
    """Frame rows and the stored events_log are folded in."""

    def test_events_from_vision_rows(self):
        payload = {'trials': [{
            'condition': 'eo_firm',
            'vision_summary': {'frames': 30, 'fps_mean': 30.0},
            'vision_rows': [
                {'t_rel': 0.5, 'metrics': {},
                 'events': [{'event': 'pose_lost', 'severity': 'low'}]},
                {'t_rel': 2.0, 'metrics': {},
                 'events': [{'event': 'pose_regained', 'severity': 'info'}]},
            ],
        }]}
        html = vision_report.render_vision_section(payload)
        self.assertIn('Session observations', html)
        self.assertIn('Pose lost', html)
        self.assertIn('0.5 s', html)
        self.assertIn('Pose regained', html)

    def test_events_log_with_type_keys(self):
        payload = session()
        payload['events'] = 2                      # stored count, not a list
        payload['events_log'] = [
            {'type': 'low_visibility', 't_rel': 1.2, 'detail': 'ankle',
             'source': 'worker'},
            {'type': 'pain', 'note': 'left knee', 't_rel': 20.0,
             'source': 'operator'},
        ]
        html = vision_report.render_vision_section(payload)
        self.assertIn('Low landmark visibility', html)
        self.assertIn('ankle', html)
        self.assertIn('operator mark: pain', html)
        self.assertIn('left knee', html)

    def test_event_cap_is_explicit(self):
        events = [{'event': 'low_visibility', 'severity': 'low',
                   'detail': 'frame {}'.format(index), 't_rel': index * 0.1}
                  for index in range(30)]
        payload = session(summary=vision_summary(events=events))
        html = vision_report.render_vision_section(payload)
        self.assertIn('5 further observation(s) are not shown', html)


class TestSecurityV2(unittest.TestCase):
    """Every v2 string is escaped at render time."""

    def test_mark_note_and_event_detail_are_escaped(self):
        payload = session()
        payload['operator_marks'] = [{
            'type': 'note', 'note': '<img src=x onerror=alert(1)>'}]
        payload['events'] = [{'event': 'low_visibility',
                              'detail': '<script>alert(1)</script>'}]
        html = vision_report.render_vision_section(payload)
        self.assertNotIn('<img', html)
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;img', html)
        self.assertIn('&lt;script&gt;', html)

    def test_transcript_is_escaped(self):
        payload = session(summary=vision_summary(
            audio={'windows': 10, 'speech_windows': 2,
                   'transcript_excerpts': ['<b>hola</b>']}))
        html = vision_report.render_vision_section(payload)
        self.assertNotIn('<b>hola', html)
        self.assertIn('&lt;b&gt;hola&lt;/b&gt;', html)

    def test_junk_audio_and_face_do_not_crash(self):
        payload = session()
        payload['trials'][0]['vision_summary']['audio'] = 'on'
        payload['trials'][0]['vision_summary']['face'] = ['mouth']
        html = vision_report.render_vision_section(payload)
        self.assertIn('Session observations', html)
        self.assertNotIn('Audio activity', html)
        self.assertNotIn('Face-landmark proxies', html)

    def test_short_transcript_and_flag_payloads_are_tolerated(self):
        payload = session(summary=vision_summary(
            audio={'windows': 'many', 'speech_windows': None,
                   'mean_dbfs': 'loud', 'stt': 'unavailable',
                   'flags': {'pain': 2}}))
        html = vision_report.render_vision_section(payload)
        self.assertIn('Speech-to-text: unavailable', html)
        self.assertIn('keyword match: pain (local STT, may be wrong) x2', html)
        self.assertIn('Audio windows: {}'.format(EM_DASH), html)

    def test_audio_at_two_levels_is_not_double_counted(self):
        payload = session()
        payload['audio'] = {'windows': 120, 'speech_windows': 18,
                            'mean_dbfs': -38.2, 'max_dbfs': -11.4}
        html = vision_report.render_vision_section(payload)
        self.assertIn('Audio windows: 120', html)
        self.assertNotIn('Audio windows: 240', html)

    def test_operator_mark_label_is_escaped(self):
        payload = session()
        payload['events'] = [{'event': 'operator_<b>pain</b>',
                              'detail': 'x'}]
        html = vision_report.render_vision_section(payload)
        self.assertNotIn('<b>pain</b>', html)
        self.assertIn('operator mark: &lt;b&gt;pain&lt;/b&gt;', html)

    def test_unknown_mark_type_is_escaped_not_executed(self):
        payload = session()
        payload['operator_marks'] = [{'type': '<b>pain</b>', 'note': 'x'}]
        html = vision_report.render_vision_section(payload)
        self.assertNotIn('<b>', html)
        self.assertIn('&lt;b&gt;pain&lt;/b&gt;', html)


class TestExistingContractUnchanged(unittest.TestCase):
    """The caveat list, empty-string contract and verdict vocabulary hold."""

    def test_empty_sessions_still_return_empty_string(self):
        self.assertEqual(vision_report.render_vision_section(None), '')
        self.assertEqual(vision_report.render_vision_section({}), '')
        self.assertEqual(
            vision_report.render_vision_section({'trials': [
                {'condition': 'eo_firm', 'vision_summary': {}}]}), '')

    def test_caveat_list_is_the_norms_list(self):
        html = vision_report.render_vision_section(session())
        self.assertEqual(len(norms.caveats()), 14)
        for caveat in norms.caveats():
            self.assertIn(vision_report._esc(caveat), html)

    def test_section_is_closed(self):
        html = vision_report.render_vision_section(session())
        self.assertTrue(html.rstrip().endswith('</section>'))


if __name__ == '__main__':
    unittest.main()
