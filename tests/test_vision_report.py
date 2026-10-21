"""TEST ONLY: the vision report section, on synthetic session fixtures.

No camera, no plate, no service import: every fixture is a plain dict built in
this file.  The assertions pin the section's contract - it renders the numbers
it is given, escapes every untrusted string, refuses to invent a verdict from a
non-transferable norm, and returns an empty string when there is neither vision
data nor a COP-COM payload.

Regression coverage added for the QC review of 2026-10-21: C1 (boolean worker
flags are presence, not counts), H2 (uncertainty wording, no valgus claim),
H7 (one row per stored segment), H9 (COP-COM without a camera), M1 (MDC keyed
by stance), M2 (low-pass COP-COM naming), M5 (audio/operator-mark labels),
M7 (no camera weight-bearing vs force-corridor comparison) and L3/M10
(processing rate / inference-encode latency labels).
"""
import unittest

from vision import vision_report

EM_DASH = '\u2014'
EN_DASH = '\u2013'


def metric(mean, sd=None):
    payload = {'mean': mean}
    if sd is not None:
        payload['sd'] = sd
    return payload


def summary(condition='eo_firm', **overrides):
    base = {
        'frames': 600,
        'pose_coverage_pct': 93.4,
        'fps_mean': 29.5,
        'fps_p95_latency_ms': 62.0,
        'sync': {
            'plate_span_s': 30.0,
            'vision_span_s': 29.4,
            'offset_ms_estimate': 42.0,
            'jitter_ms_sd': 8.1,
            'method': 'software',
        },
        'angles': {
            'left_knee': metric(12.3, 2.1),
            'right_knee': metric(11.9, 2.4),
            'left_hip': metric(9.8, 1.7),
            'right_hip': metric(10.1, 1.9),
            'left_ankle': metric(4.2, 1.1),
            'right_ankle': metric(4.6, 1.3),
            'trunk_lean_deg': metric(3.4, 1.2),
            'pelvic_tilt_deg': metric(2.0, 0.9),
        },
        'sway': {'trunk_sway_x_range': 0.021, 'trunk_sway_y_range': 0.034},
        # knee_valgus_delta is deliberately present: the worker computes no
        # valgus metric, so the report must ignore it (H2).
        'asymmetry': {'knee_valgus_delta': 3.2, 'hip_angle_delta': 1.4,
                      'weight_bearing_est': 50.0},
        'flags': ['low_light'],
        'notes': [],
    }
    base.update(overrides)
    return base


def cop_com(**overrides):
    payload = {
        'available': True,
        'method': 'gravity-line projection (low-pass COP)',
        'cutoff_hz': 0.5,
        'fs_hz': 100.0,
        'samples': 2600,
        'cop_com_rms_mm': 3.21,
        'cop_com_velocity_mm_s': 12.4,
        'cop_com_ap_rms_mm': 2.44,
        'com_rms_mm': 7.10,
        'note': 'absolute values depend on cutoff',
    }
    payload.update(overrides)
    return payload


def second_summary():
    return summary(
        frames=620,
        pose_coverage_pct=88.0,
        fps_mean=29.0,
        angles={'trunk_lean_deg': metric(5.5, 1.8)},
        sway={'trunk_sway_x_range': 0.03, 'trunk_sway_y_range': 0.05},
        asymmetry={'weight_bearing_est': {'left_pct': 41.0}},
        flags=[],
    )


def session(age=45, sex='female'):
    return {
        'athlete_age': age,
        'athlete_sex': sex,
        'trials': [
            {'condition': 'eo_firm', 'vision_summary': summary(),
             'vision_rows': [], 'cop_com': cop_com()},
            {'condition': 'ec_firm', 'vision_summary': second_summary(),
             'vision_rows': [], 'cop_com': {'available': False}},
        ],
    }


COP_HEADING = ('COP' + EN_DASH + 'COM (low-pass estimate; not validated '
               'GLP)')


def segmented_summary():
    """A v2 summary storing three per-test segments (H7)."""
    return summary(
        frames=900,
        segments=[
            {'test_id': 'double_leg_eo', 'limb': 'both', 'trial': 1,
             'frames': 300, 'pose_coverage_pct': 96.0,
             'angles': {'left_knee': metric(10.0, 1.0),
                        'trunk_lean_deg': metric(2.0, 0.5)},
             'sway': {'trunk_sway_x_range': 0.010,
                      'trunk_sway_y_range': 0.020}},
            {'test_id': 'single_leg_left', 'limb': 'left', 'trial': 2,
             'frames': 300, 'pose_coverage_pct': 88.0,
             'angles': {'left_knee': metric(14.0, 2.0)}, 'sway': {}},
            {'test_id': 'single_leg_right', 'limb': 'right', 'trial': 1,
             'frames': 300, 'pose_coverage_pct': 91.0,
             'angles': {'right_knee': metric(16.0, 2.5)}, 'sway': {}},
        ])


class TestEmpty(unittest.TestCase):
    """No vision data and no COP-COM -> empty string, concatenate blind."""

    def test_no_session(self):
        self.assertEqual(vision_report.render_vision_section(None), '')
        self.assertEqual(vision_report.render_vision_section('nonsense'), '')

    def test_no_trials(self):
        self.assertEqual(vision_report.render_vision_section({}), '')
        self.assertEqual(vision_report.render_vision_section({'trials': []}),
                         '')

    def test_trials_without_vision_or_cop_com(self):
        payload = {'trials': [{'condition': 'eo_firm'},
                              {'condition': 'ec_firm',
                               'vision_summary': None},
                              {'condition': 'c1', 'vision_summary': {}}]}
        self.assertEqual(vision_report.render_vision_section(payload), '')

    def test_non_dict_summary_ignored(self):
        payload = {'trials': [{'condition': 'eo_firm',
                               'vision_summary': 'frames=600'}]}
        self.assertEqual(vision_report.render_vision_section(payload), '')


class TestFullSession(unittest.TestCase):
    """A complete session renders every documented block."""

    def setUp(self):
        self.html = vision_report.render_vision_section(session())

    def test_section_and_header(self):
        self.assertIn('<section class="report-section vision-section"', self.html)
        self.assertIn('Camera-derived kinematics (pose estimate)', self.html)
        self.assertIn('Markerless pose estimation', self.html)
        self.assertIn('Frames: 1220', self.html)
        self.assertIn('Pose coverage: 90.7 % of frames', self.html)
        self.assertIn('Mean processing rate: 29.2 fps', self.html)
        # L3: the rate is the processing rate, not the camera capture rate.
        self.assertIn("not the camera's capture rate", self.html)
        self.assertNotIn('Mean camera rate', self.html)
        self.assertIn('offset 42.0 ms', self.html)
        self.assertIn('jitter 8.1 ms SD', self.html)

    def test_angle_table(self):
        for header in ('Test', 'Limb', 'Trial', 'Frames', 'Coverage %',
                       'Knee L (deg)', 'Knee R (deg)', 'Hip L (deg)',
                       'Hip R (deg)', 'Ankle L (deg)', 'Ankle R (deg)',
                       'Trunk lean (deg)', 'Pelvic tilt (deg)',
                       'Trunk sway X range', 'Trunk sway Y range'):
            self.assertIn('<th>{}</th>'.format(header), self.html)
        self.assertIn('12.3 \u00b1 2.1', self.html)
        self.assertIn('5.5 \u00b1 1.8', self.html)
        self.assertIn('0.021', self.html)
        self.assertIn('eo_firm', self.html)
        self.assertIn('ec_firm', self.html)
        self.assertIn('Per-test kinematics', self.html)

    def test_uncertainty_wording_is_honest(self):
        """H2: 15.49 deg static RMSE / ICC 0.136, quiet standing is worst."""
        self.assertIn('15.49', self.html)
        self.assertIn('ICC of 0.136', self.html)
        self.assertIn('WORST case', self.html)
        self.assertIn('ESTIMATE', self.html)
        self.assertNotIn('5\u201310 deg uncertainty band', self.html)
        self.assertNotIn('degrees when a camera scale was available',
                         self.html)
        self.assertNotIn('4\u20137 deg in gait', self.html)

    def test_no_valgus_change_score_claim(self):
        """H2: no valgus metric exists, so no valgus claim may be printed."""
        self.assertNotIn('change scores only', self.html)
        self.assertNotIn('knee valgus delta', self.html)
        self.assertNotIn('+3.2 deg', self.html)
        self.assertIn('frontal-plane knee valgus is not computed at all',
                      self.html)

    def test_asymmetry_and_flags(self):
        self.assertIn('hip angle difference L' + EN_DASH + 'R +1.4 deg',
                      self.html)
        self.assertIn('low_light', self.html)

    def test_weight_bearing_is_not_compared_to_a_force_corridor(self):
        """M7: the camera proxy gets a 'not comparable' note, never a band."""
        self.assertIn('Camera weight-bearing estimate (eo_firm): 50 % left',
                      self.html)
        self.assertIn('Not comparable', self.html)
        self.assertIn('per-limb force is not measurable here', self.html)
        self.assertNotIn('43\u201357 % body weight', self.html)

    def test_cop_com_block(self):
        self.assertIn(COP_HEADING, self.html)
        self.assertIn('<td>3.21</td>', self.html)
        self.assertIn('<td>2.44</td>', self.html)
        self.assertIn('<td>7.10</td>', self.html)
        self.assertIn('<td>12.40</td>', self.html)
        self.assertIn('<td>0.50</td>', self.html)
        self.assertIn('least reliable in single-leg stance', self.html)
        # M2: GLP is the upgrade path, never the name of what was computed.
        self.assertNotIn('COP\u2013COM (gravity-line projection)', self.html)
        self.assertIn('NOT a validated gravity-line projection', self.html)
        self.assertIn('upgrade path', self.html)

    def test_reference_comparison(self):
        self.assertIn('Reference comparison (age/sex)', self.html)
        self.assertIn('No published, transferable reference band applies',
                      self.html)
        self.assertNotIn('within published reference band', self.html)
        self.assertIn('per-limb force is not measurable', self.html)

    def test_mdc_statement_is_stance_keyed(self):
        """M1: this fixture is double-leg, so no COP threshold is applied."""
        self.assertIn('Longitudinal change (MDC)', self.html)
        self.assertIn('No published double-leg COP MDC exists', self.html)
        self.assertNotIn('30 % of baseline', self.html)
        self.assertIn('0.140\u20130.285 deg/s', self.html)
        self.assertIn('1.12 deg/s', self.html)

    def test_limitations(self):
        self.assertIn('Limitations', self.html)
        self.assertIn('15.49 deg', self.html)
        self.assertIn('ICC of 0.136', self.html)
        self.assertIn('no 3-D reconstruction', self.html)
        self.assertIn('frontal-plane knee valgus is not computed at all',
                      self.html)
        self.assertIn('Foot-lift timing is not scored', self.html)
        self.assertIn('hold time, repetition counts and gait events', self.html)
        self.assertIn('does not measure the centre of pressure', self.html)
        self.assertIn('not a diagnosis', self.html)
        self.assertIn('physician-led', self.html)

    def test_caveat_footer(self):
        self.assertIn('What this section does not claim', self.html)
        self.assertEqual(self.html.count('<li>Do not'), 14)

    def test_section_is_closed(self):
        self.assertTrue(self.html.rstrip().endswith('</section>'))


class TestFlagCounting(unittest.TestCase):
    """C1: the worker's boolean flag dict is presence, not a count."""

    def test_real_keywords_output_counts_one_pain_only(self):
        from vision.audio_math import keywords

        flags = keywords('me duele la rodilla')
        self.assertTrue(flags['pain'])
        self.assertFalse(flags['comprehension'])
        self.assertFalse(flags['stop'])
        self.assertEqual([hit['category'] for hit in flags['matches']],
                         ['pain'])

        merged = vision_report._merge_audio(
            [{'windows': 1, 'stt': {'state': 'on', 'flags': flags}}])
        self.assertEqual(merged['flag_counts'], {'pain': 1.0})

        payload = {'trials': [{
            'condition': 'single_leg_left',
            'vision_summary': summary(
                audio={'windows': 1, 'speech_windows': 1,
                       'stt': {'state': 'on',
                               'text': 'me duele la rodilla',
                               'flags': flags}})}]}
        html = vision_report.render_vision_section(payload)
        self.assertIn('keyword match: pain (local STT, may be wrong) x1', html)
        self.assertNotIn('comprehension/confusion phrase keyword match', html)
        self.assertNotIn('keyword match: stop', html)

    def test_false_flags_contribute_nothing(self):
        from vision.audio_math import empty_flags

        merged = vision_report._merge_audio(
            [{'windows': 1, 'stt': {'flags': empty_flags()}}])
        self.assertEqual(merged['flag_counts'], {})

    def test_matches_list_is_never_a_count(self):
        counts = {}
        vision_report._absorb_flags(
            {'pain': True, 'matches': [{'category': 'pain',
                                        'phrase': 'me duele'}]}, counts)
        self.assertEqual(counts, {'pain': 1.0})

    def test_non_bool_non_numeric_values_are_ignored(self):
        counts = {}
        vision_report._absorb_flags({'pain': 'yes', 'stop': None,
                                     'comprehension': [1]}, counts)
        self.assertEqual(counts, {})

    def test_legacy_numeric_counts_still_work(self):
        counts = {}
        vision_report._absorb_flags({'pain': 2}, counts)
        self.assertEqual(counts, {'pain': 2.0})


class TestSegmentedSummary(unittest.TestCase):
    """H7: one row per stored segment, never a silent session merge."""

    def _html(self):
        payload = {'athlete_age': 45, 'athlete_sex': 'female',
                   'trials': [{'condition': 'battery',
                               'vision_summary': segmented_summary(),
                               'vision_rows': []}]}
        return vision_report.render_vision_section(payload)

    def test_one_row_per_segment_with_test_limb_trial(self):
        html = self._html()
        self.assertEqual(html.count('<tr>'), 4)      # header + 3 segments
        for test_id in ('double_leg_eo', 'single_leg_left',
                        'single_leg_right'):
            self.assertIn('<td>{}</td>'.format(test_id), html)
        self.assertIn('<td>both</td>', html)
        self.assertIn('<td>left</td>', html)
        self.assertIn('<td>right</td>', html)
        self.assertIn('<td>300</td>', html)
        self.assertIn('One row per stored test segment', html)
        self.assertIn('Segments are never pooled', html)
        self.assertNotIn('No per-test segmentation was stored', html)

    def test_segment_angles_replace_the_merged_summary(self):
        html = self._html()
        self.assertIn('10.0 \u00b1 1.0', html)       # double_leg_eo knee
        self.assertIn('14.0 \u00b1 2.0', html)       # single_leg_left knee
        self.assertIn('16.0 \u00b1 2.5', html)       # single_leg_right knee
        # The clip-level mean (12.3) must never be shown as a segment row.
        self.assertNotIn('12.3 \u00b1 2.1', html)

    def test_absent_segments_say_the_clip_was_one_block(self):
        payload = {'trials': [
            {'condition': 'eo_firm', 'vision_summary': summary()},
            {'condition': 'ec_firm', 'vision_summary': second_summary()},
        ]}
        html = vision_report.render_vision_section(payload)
        self.assertIn('No per-test segmentation was stored', html)
        self.assertIn('whole clip was summarised as one block', html)
        self.assertEqual(html.count('<tr>'), 3)      # header + 2 trials


class TestCopComWithoutCamera(unittest.TestCase):
    """H9: the plate-only COP-COM block never depends on a camera."""

    def test_plate_only_session_renders_the_block(self):
        payload = {'trials': [
            {'condition': 'c1_eo_firm', 'cop_com': cop_com()},
            {'condition': 'c2_ec_firm', 'cop_com': {'available': False}},
        ]}
        html = vision_report.render_vision_section(payload)
        self.assertIn(COP_HEADING, html)
        self.assertIn('<td>3.21</td>', html)
        self.assertIn('No camera data was stored for this session', html)
        self.assertIn('least reliable in single-leg stance', html)
        self.assertIn('What this section does not claim', html)
        self.assertNotIn('Per-test kinematics', html)
        self.assertNotIn('Camera-derived kinematics (pose estimate)', html)

    def test_plate_only_session_without_cop_com_stays_empty(self):
        payload = {'trials': [{'condition': 'c1_eo_firm',
                               'cop_com': {'available': False}}]}
        self.assertEqual(vision_report.render_vision_section(payload), '')

    def test_camera_session_keeps_the_block(self):
        html = vision_report.render_vision_section(session())
        self.assertIn(COP_HEADING, html)


class TestAudioAndMarkLabels(unittest.TestCase):
    """M5: keyword hits and operator marks are labelled, never bare."""

    def _event_html(self, event):
        payload = session()
        payload['events'] = [event]
        return vision_report.render_vision_section(payload)

    def test_pain_phrase_event_shows_the_matched_phrase(self):
        html = self._event_html({
            'event': 'audio_pain_phrase', 'severity': 'warning',
            'detail': 'possible pain phrase: "me duele" - local STT, may be '
                      'wrong', 't_rel': 3.0})
        self.assertIn('keyword match: pain (local STT, may be wrong)', html)
        self.assertIn('me duele', html)
        self.assertNotIn('Pain or discomfort phrase heard', html)

    def test_unknown_phrase_is_a_comprehension_match(self):
        html = self._event_html({
            'event': 'audio_unknown_phrase', 'severity': 'notice',
            'detail': 'possible comprehension phrase: "otra vez"', 't_rel': 4.0})
        self.assertIn('comprehension/confusion phrase keyword match '
                      '(local STT, may be wrong)', html)
        self.assertIn('otra vez', html)
        self.assertNotIn('Unrecognised phrase heard', html)

    def test_operator_pain_mark_is_qualified(self):
        payload = session()
        payload['operator_marks'] = [{'type': 'pain', 'note': 'left knee',
                                      't_rel': 12.5}]
        html = vision_report.render_vision_section(payload)
        self.assertIn('operator mark: pain', html)
        self.assertNotIn('Pain (operator mark)', html)
        self.assertNotIn('<td>Pain</td>', html)

    def test_event_count_tally_never_says_bare_pain(self):
        payload = session()
        payload['trials'][0]['vision_summary']['event_counts'] = {
            'low_visibility': 2, 'pain': 1}
        html = vision_report.render_vision_section(payload)
        self.assertIn('Recorded observation counts:', html)
        self.assertIn('operator mark: pain 1', html)
        self.assertIn('Low landmark visibility 2', html)


class TestVerdictVocabulary(unittest.TestCase):
    """No diagnostic words, ever."""

    def test_no_normal_or_abnormal(self):
        html = vision_report.render_vision_section(session())
        self.assertNotIn('normal', html.lower())
        self.assertNotIn('abnormal', html.lower())

    def test_only_the_allowed_verdict_strings(self):
        html = vision_report.render_vision_section(session())
        self.assertNotIn('within normal', html.lower())
        self.assertNotIn('impaired', html.lower())
        self.assertNotIn('pathological', html.lower())
        self.assertIn('This section is not a diagnosis', html)


class TestReferenceGate(unittest.TestCase):
    """Reference comparison only with age + sex and a transferable norm."""

    def test_no_age_or_sex_means_no_comparison(self):
        for age, sex in ((None, 'female'), (45, None), (None, None),
                         ('unknown', '')):
            html = vision_report.render_vision_section(session(age=age,
                                                               sex=sex))
            self.assertNotIn('within published reference band', html)
            self.assertNotIn('outside published band', html)
            self.assertNotIn('<th>Measure</th>', html)
            self.assertIn('no age/sex reference comparison is made', html)

    def test_non_transferable_norm_never_yields_a_band(self):
        payload = {
            'athlete_age': 30,
            'athlete_sex': 'male',
            'trials': [{
                'condition': 'eo_firm',
                'vision_summary': summary(
                    asymmetry={},
                    angles={'trunk_lean_deg': metric(14.0, 2.0)}),
                'vision_rows': [],
            }],
        }
        html = vision_report.render_vision_section(payload)
        self.assertIn('Published comparators exist', html)
        self.assertIn('Trunk lean (deg, pose estimate)', html)
        self.assertNotIn('within published reference band', html)
        self.assertNotIn('outside published band', html)
        self.assertNotIn('<th>Measure</th>', html)

    def test_transferable_mean_only_norm(self):
        payload = {
            'athlete_age': 65,
            'athlete_sex': 'female',
            'trials': [{
                'condition': 'single_leg_left',
                'vision_summary': {'frames': 300, 'hold_s_left': 20.0},
                'vision_rows': [],
            }],
        }
        html = vision_report.render_vision_section(payload)
        self.assertIn('60\u201369', html)
        self.assertIn('mean 25.1 s', html)
        self.assertIn('reference mean only (no published band)', html)
        self.assertIn('pubmed.ncbi.nlm.nih.gov/19839175', html)
        self.assertIn('Stopwatch-timed unipedal stance', html)
        self.assertIn('operator-timed', html)

    def test_oldest_band_renders_as_open_ended(self):
        payload = {
            'athlete_age': 82,
            'athlete_sex': 'female',
            'trials': [{
                'condition': 'single_leg_left',
                'vision_summary': {'hold_s_left': 6.0},
                'vision_rows': [],
            }],
        }
        html = vision_report.render_vision_section(payload)
        self.assertIn('80+', html)
        self.assertIn('mean 7.4 s', html)
        self.assertNotIn('80\u2013120', html)

    def test_no_transferable_measure_prints_a_plain_note(self):
        payload = {
            'athlete_age': 45,
            'athlete_sex': 'female',
            'trials': [{'condition': 'eo_firm',
                        'vision_summary': {'frames': 100}}],
        }
        html = vision_report.render_vision_section(payload)
        self.assertIn('No published, transferable reference band applies',
                      html)


class TestMdcByStance(unittest.TestCase):
    """M1: the report states a threshold only for a known stance."""

    def _html(self, condition, **summary_overrides):
        payload = {'trials': [{'condition': condition,
                               'vision_summary': summary(
                                   **summary_overrides)}]}
        return vision_report.render_vision_section(payload)

    def test_single_leg_uses_the_absolute_mdc_table_value(self):
        html = self._html('single_leg_left')
        self.assertIn('Single-leg COP velocity: MDC 9.0 mm/s absolute', html)
        self.assertIn('cross-check', html)
        self.assertIn('Single-leg COP area: about 50 % of baseline', html)

    def test_double_leg_applies_no_cop_threshold(self):
        html = self._html('double_leg_eo')
        self.assertIn('No published double-leg COP MDC exists', html)
        self.assertNotIn('MDC 9.0 mm/s', html)
        self.assertNotIn('50 % of baseline', html)

    def test_unknown_stance_says_mdc_requires_a_known_stance(self):
        html = self._html('battery')
        self.assertIn('MDC requires a known stance', html)
        self.assertNotIn('MDC 9.0 mm/s', html)

    def test_mixed_session_refuses_a_single_threshold(self):
        payload = {'trials': [
            {'condition': 'single_leg_left',
             'vision_summary': summary()},
            {'condition': 'eo_firm', 'vision_summary': summary()},
        ]}
        html = vision_report.render_vision_section(payload)
        self.assertIn('This session mixes conditions', html)
        self.assertNotIn('MDC 9.0 mm/s', html)


class TestSecurity(unittest.TestCase):
    """Every untrusted string is escaped at render time."""

    def test_condition_label_is_escaped(self):
        payload = {
            'athlete_age': 45, 'athlete_sex': 'female',
            'trials': [{'condition': '<script>alert("x")</script>',
                        'vision_summary': summary(),
                        'cop_com': cop_com()}],
        }
        html = vision_report.render_vision_section(payload)
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertIn('&quot;x&quot;', html)

    def test_flags_and_notes_are_escaped(self):
        payload = {
            'trials': [{'condition': 'eo_firm',
                        'vision_summary': summary(
                            flags=['<img src=x onerror=alert(1)>'])}],
        }
        html = vision_report.render_vision_section(payload)
        self.assertNotIn('<img', html)
        self.assertIn('&lt;img', html)

    def test_segment_fields_are_escaped(self):
        payload = {'trials': [{
            'condition': 'eo_firm',
            'vision_summary': summary(segments=[{
                'test_id': '<script>alert(1)</script>',
                'limb': '<b>left</b>', 'trial': 1,
                'angles': {'left_knee': metric(10.0, 1.0)}}])}]}
        html = vision_report.render_vision_section(payload)
        self.assertNotIn('<script>', html)
        self.assertNotIn('<b>left</b>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_metric_text_is_never_echoed(self):
        """A non-numeric metric is refused, not escaped-and-shown."""
        payload = {
            'trials': [{'condition': 'eo_firm',
                        'vision_summary': {'angles': {
                            'left_knee': {'mean': '<b>1</b>'}}}}],
        }
        html = vision_report.render_vision_section(payload)
        self.assertNotIn('<b>', html)
        self.assertNotIn('&lt;b&gt;', html)
        self.assertNotIn('1</b>', html)
        self.assertIn('<td>{}</td>'.format(EM_DASH), html)


class TestRobustness(unittest.TestCase):
    """Garbage in, honest dashes out - never an exception, never a number."""

    def test_junk_values_render_as_dashes(self):
        payload = {
            'athlete_age': 'x', 'athlete_sex': 7,
            'trials': [{
                'condition': 'eo_firm',
                'vision_summary': {
                    'frames': 'abc',
                    'pose_coverage_pct': None,
                    'angles': {'left_knee': {'mean': 'oops'},
                               'right_knee': None},
                    'sway': {'trunk_sway_x_range': [1, 2]},
                    'asymmetry': 'nonsense',
                    'flags': 'not-a-list',
                },
            }],
        }
        html = vision_report.render_vision_section(payload)
        self.assertIn('vision-section', html)
        self.assertIn(EM_DASH, html)
        self.assertNotIn('oops', html)

    def test_junk_segments_fall_back_to_one_row(self):
        for segments in ('none', [], [1, 2], None, {'a': 1}):
            payload = {'trials': [{'condition': 'eo_firm',
                                   'vision_summary': summary(
                                       segments=segments)}]}
            html = vision_report.render_vision_section(payload)
            self.assertIn('No per-test segmentation was stored', html)

    def test_cop_com_unavailable_hides_the_block(self):
        payload = {
            'trials': [{'condition': 'eo_firm',
                        'vision_summary': summary(),
                        'cop_com': {'available': False}}],
        }
        html = vision_report.render_vision_section(payload)
        self.assertNotIn(COP_HEADING, html)
        self.assertNotIn('least reliable in single-leg stance', html)

    def test_repeated_condition_gets_trial_numbers(self):
        payload = {
            'trials': [
                {'condition': 'ec_firm', 'vision_summary': summary()},
                {'condition': 'ec_firm', 'vision_summary': summary()},
            ],
        }
        html = vision_report.render_vision_section(payload)
        self.assertIn('ec_firm (trial 1)', html)
        self.assertIn('ec_firm (trial 2)', html)

    def test_low_coverage_warns(self):
        payload = {
            'trials': [{'condition': 'eo_firm',
                        'vision_summary': summary(pose_coverage_pct=72.0)}],
        }
        html = vision_report.render_vision_section(payload)
        self.assertIn('Pose coverage below 80 %', html)

    def test_worker_notes_and_latency_are_rendered(self):
        payload = {
            'trials': [{'condition': 'eo_firm',
                        'vision_summary': summary(
                            notes=['insufficient frames'],
                            latency_ms={'mean': 40.0, 'p95': 62.0})}],
        }
        html = vision_report.render_vision_section(payload)
        self.assertIn('Inference+encode latency: 62.0 ms p95', html)
        self.assertIn('EXCLUDES camera capture', html)
        self.assertNotIn('Pose latency', html)
        self.assertIn('insufficient frames', html)
        self.assertIn('(note)', html)

    def test_renders_a_real_pose_math_summary(self):
        """Cross-module contract: the worker summary shape renders directly."""
        from vision.pose_math import summarize_frames

        frames = []
        for index in range(120):
            frames.append({
                'frame_id': index, 'pose_found': True, 'fps': 30.0,
                'metrics': {
                    'left_knee': 10.0 + 0.1 * index, 'right_knee': 11.0,
                    'left_hip': 8.0, 'right_hip': 8.5,
                    'left_ankle': 4.0, 'right_ankle': 4.5,
                    'trunk_lean_deg': 2.0, 'pelvic_tilt_deg': 1.0,
                    'trunk_x': 0.5 + 0.001 * index, 'trunk_y': 0.6,
                    'com_x': 0.5, 'com_y': 0.5,
                },
            })
        summary_payload = summarize_frames(frames)
        html = vision_report.render_vision_section({
            'athlete_age': 45, 'athlete_sex': 'female',
            'trials': [{'condition': 'eo_firm',
                        'vision_summary': summary_payload}],
        })
        self.assertIn('Pose coverage: 100.0 % of frames', html)
        self.assertIn('Mean processing rate: 30.0 fps', html)
        self.assertIn('Knee L (deg)', html)
        self.assertIn('Trunk lean (deg)', html)
        self.assertIn('<td>eo_firm</td>', html)

    def test_zero_cop_com_is_a_number_not_a_dash(self):
        payload = {
            'trials': [{'condition': 'eo_firm',
                        'vision_summary': summary(),
                        'cop_com': cop_com(cop_com_rms_mm=0.0,
                                           com_rms_mm=0.0,
                                           cop_com_velocity_mm_s=0.0)}],
        }
        html = vision_report.render_vision_section(payload)
        self.assertIn('<td>0.00</td>', html)

    def test_real_cop_com_payload_is_accepted_verbatim(self):
        """Cross-module contract: feed cop_com.summarize() straight through."""
        import math

        from cop_com import summarize

        count = 3000                       # 30 s at 100 Hz
        cop = [5.0 * math.sin(2.0 * math.pi * 0.2 * index / 100.0)
               for index in range(count)]
        payload = {
            'athlete_age': 45, 'athlete_sex': 'female',
            'trials': [{'condition': 'eo_firm', 'vision_summary': summary(),
                        'vision_rows': [],
                        'cop_com': summarize([index / 100.0
                                              for index in range(count)],
                                             cop, [0.0] * count, 100.0)}],
        }
        html = vision_report.render_vision_section(payload)
        self.assertIn(COP_HEADING, html)
        self.assertIn('<td>2600</td>', html)
        self.assertIn('<td>0.50</td>', html)
        self.assertIn('least reliable in single-leg stance', html)


if __name__ == '__main__':
    unittest.main()
