"""TEST ONLY: pins the reference-table semantics, not the literature values.

The numbers here are transcriptions of research/NORMS-AND-NEURO-INTERPRETATION.md;
these tests check that the table SELECTS them correctly (band, sex, no
interpolation, no cross-device transfer) and that the MDC helpers never turn a
change below the threshold into a claim.
"""
import math
import unittest

from vision import norms

REQUIRED_KEYS = {
    'measure', 'unit', 'age_min', 'age_max', 'sex', 'mean', 'sd', 'lo', 'hi',
    'device', 'condition', 'source_url', 'year', 'transferable', 'note',
}

REQUIRED_MEASURES = {
    'cop_path_length_20s',        # BTrackS 20 s eyes-closed decades
    'cop_velocity_60s',           # Kistler 60 s EO/EC
    'single_leg_cop_velocity',    # 35.0 +/- 7.9 mm/s
    'single_leg_cop_area',        # 2,958 +/- 957 mm^2
    'single_leg_hold_s_eo',       # Springer, eyes open
    'single_leg_hold_s_ec',       # Springer, eyes closed
    'weight_bearing_left_pct',    # 43-57 % corridor
    'mctsib_sway_velocity',       # i-mCTSIB control anchors
}

VERDICTS = {'no measurable change', 'change exceeds provisional MDC'}


class TestTableShape(unittest.TestCase):
    """Every entry carries the full schema and internally consistent values."""

    def test_every_entry_has_the_documented_schema(self):
        for entry in norms.ENTRIES:
            self.assertEqual(set(entry), REQUIRED_KEYS, entry.get('measure'))

    def test_value_types_and_ordering(self):
        for entry in norms.ENTRIES:
            label = entry['measure']
            self.assertIsInstance(entry['unit'], str)
            self.assertIsInstance(entry['device'], str)
            self.assertIsInstance(entry['note'], str)
            self.assertIsInstance(entry['year'], int)
            self.assertTrue(1000 < entry['year'] <= 2030, label)
            self.assertTrue(str(entry['source_url']).startswith('https://'),
                            label)
            self.assertIn(entry['sex'], (None, 'female', 'male'), label)
            self.assertIsInstance(entry['transferable'], bool)
            for key in ('mean', 'sd'):
                value = entry[key]
                self.assertTrue(value is None or math.isfinite(value), label)
            low, high = entry['lo'], entry['hi']
            if low is not None or high is not None:
                self.assertIsNotNone(low, label)
                self.assertIsNotNone(high, label)
                self.assertLessEqual(low, high, label)
            low_age, high_age = entry['age_min'], entry['age_max']
            if low_age is not None or high_age is not None:
                self.assertIsNotNone(low_age, label)
                self.assertIsNotNone(high_age, label)
                self.assertLess(low_age, high_age, label)
                self.assertIsNotNone(entry['mean'], label + ' needs a mean')

    def test_required_measures_are_present(self):
        self.assertTrue(REQUIRED_MEASURES <= set(norms.MEASURES))

    def test_mctisib_mdc_values_are_encoded(self):
        firm = norms.mdc_for('mctsib_sway_velocity_firm')
        foam_eo = norms.mdc_for('mctsib_sway_velocity_foam_eo')
        foam_ec = norms.mdc_for('mctsib_sway_velocity_foam_ec')
        self.assertEqual(firm['mode'], 'absolute')
        self.assertEqual(firm['range'], (0.140, 0.285))
        self.assertEqual(foam_eo['range'], (0.140, 0.285))
        self.assertEqual(foam_ec['value'], 1.12)

    def test_table_version_is_stamped(self):
        self.assertRegex(norms.NORM_TABLE_VERSION, r'^norms-\d{4}-\d{2}-v\d+$')


class TestLookup(unittest.TestCase):
    """Band and sex selection: exact keys, no interpolation, no borrowing."""

    def test_btracks_decade_and_sex(self):
        entry = norms.lookup('cop_path_length_20s', age=25, sex='male')
        self.assertEqual((entry['age_min'], entry['age_max']), (20, 29))
        self.assertEqual(entry['sex'], 'male')
        self.assertEqual(entry['mean'], 25.2)
        self.assertEqual(entry['condition'], 'EC')
        self.assertIn('BTrackS', entry['device'])
        self.assertFalse(entry['transferable'])

    def test_sex_aliases(self):
        upper = norms.lookup('cop_path_length_20s', age=25, sex='F')
        lower = norms.lookup('cop_path_length_20s', age=25, sex='female')
        self.assertEqual(upper['mean'], 22.6)
        self.assertEqual(lower['mean'], 22.6)

    def test_decade_boundaries(self):
        self.assertEqual(
            norms.lookup('cop_path_length_20s', age=29, sex='male')['mean'],
            25.2)
        self.assertEqual(
            norms.lookup('cop_path_length_20s', age=30, sex='male')['mean'],
            24.7)

    def test_no_interpolation_below_the_first_band(self):
        self.assertIsNone(norms.lookup('cop_path_length_20s', age=19,
                                       sex='male'))

    def test_no_interpolation_above_the_last_band(self):
        entry = norms.lookup('cop_path_length_20s', age=85, sex='female')
        self.assertEqual(entry['mean'], 44.4)
        self.assertEqual(entry['age_max'], 120)

    def test_sex_stratified_measure_needs_a_known_sex(self):
        self.assertIsNone(norms.lookup('cop_path_length_20s', age=25))
        self.assertIsNone(norms.lookup('cop_path_length_20s', age=25,
                                       sex='unknown'))

    def test_banded_measure_needs_a_known_age(self):
        self.assertIsNone(norms.lookup('cop_path_length_20s', sex='male'))

    def test_unbanded_entry_matches_any_age(self):
        for age in (18, 45, 70):
            entry = norms.lookup('single_leg_cop_velocity', age=age,
                                 sex='male')
            self.assertEqual(entry['mean'], 35.0)
            self.assertIsNone(entry['age_min'])

    def test_mctsib_bands(self):
        younger = norms.lookup('mctsib_sway_velocity', age=45)
        older = norms.lookup('mctsib_sway_velocity', age=65)
        self.assertEqual((younger['age_min'], younger['age_max']), (40, 59))
        self.assertEqual((older['age_min'], older['age_max']), (60, 69))
        self.assertEqual(younger['condition'], 'firm_eo')
        self.assertIsNone(norms.lookup('mctsib_sway_velocity', age=30))

    def test_springer_is_transferable_and_sex_specific(self):
        entry = norms.lookup('single_leg_hold_s_eo', age=30, sex='male')
        self.assertEqual(entry['mean'], 43.2)
        self.assertTrue(entry['transferable'])
        self.assertEqual(entry['unit'], 's')

    def test_corridor_entry_has_limits_not_a_mean(self):
        entry = norms.lookup('weight_bearing_left_pct')
        self.assertEqual((entry['lo'], entry['hi']), (43.0, 57.0))
        self.assertIsNone(entry['mean'])
        self.assertTrue(entry['transferable'])

    def test_unknown_measure_and_non_string(self):
        self.assertIsNone(norms.lookup('does_not_exist', age=30, sex='male'))
        self.assertIsNone(norms.lookup(None))
        self.assertIsNone(norms.lookup('cop_path_length_20s', age='forty',
                                       sex='male'))

    def test_lookup_returns_a_copy(self):
        entry = norms.lookup('cop_path_length_20s', age=25, sex='male')
        entry['mean'] = -1.0
        again = norms.lookup('cop_path_length_20s', age=25, sex='male')
        self.assertEqual(again['mean'], 25.2)

    def test_narrowest_band_wins(self):
        candidates = [
            {'measure': 'x', 'age_min': 18, 'age_max': 80, 'sex': None},
            {'measure': 'x', 'age_min': 40, 'age_max': 49, 'sex': None},
            {'measure': 'x', 'age_min': 40, 'age_max': 70, 'sex': None},
        ]
        chosen = norms.select_entry(candidates, age=45)
        self.assertEqual((chosen['age_min'], chosen['age_max']), (40, 49))

    def test_sex_specific_preferred_over_pooled(self):
        candidates = [
            {'measure': 'x', 'age_min': 20, 'age_max': 60, 'sex': None},
            {'measure': 'x', 'age_min': 20, 'age_max': 60, 'sex': 'female'},
        ]
        self.assertEqual(norms.select_entry(candidates, age=30,
                                            sex='female')['sex'], 'female')
        self.assertIsNone(norms.select_entry(candidates, age=30,
                                             sex='male')['sex'])

    def test_pooled_fallback_when_source_did_not_stratify(self):
        candidates = [
            {'measure': 'x', 'age_min': 18, 'age_max': 80, 'sex': None},
        ]
        self.assertIsNone(norms.select_entry(candidates, age=30,
                                             sex='male')['sex'])

    def test_empty_candidates(self):
        self.assertIsNone(norms.select_entry([], age=30, sex='male'))


class TestMdc(unittest.TestCase):
    """MDC rules are keyed by measure AND stance - never borrowed by default.

    The published COP constants are single-leg values; these tests pin the
    stance gate (M1) rather than the old blanket 30 %/50 % mapping.
    """

    def test_velocity_needs_a_known_stance(self):
        self.assertIsNone(norms.mdc_for('cop_velocity'))
        self.assertIsNone(norms.mdc_for('cop_area'))
        self.assertIsNone(norms.mdc_for('cop_velocity', stance='unknown'))
        self.assertIsNone(norms.mdc_for('single_leg_cop_velocity'))

    def test_single_leg_velocity_prefers_the_absolute_mdc_table_value(self):
        import metrics

        rule = norms.mdc_for('cop_velocity', stance='single_leg')
        self.assertEqual(rule['mode'], 'absolute')
        self.assertEqual(rule['value'], 9.0)
        self.assertEqual(rule['value'],
                         metrics.MDC_TABLE['sl_mean_velocity_mm_s'])
        self.assertEqual(rule['unit'], 'mm/s')
        self.assertEqual(rule['stance'], 'single_leg')
        self.assertEqual(rule['percent_of_mean'], 0.30)
        self.assertEqual(rule['percent_range'], (0.28, 0.31))
        # Documented cross-check only: 30 % of the 35.0 mm/s reference.
        self.assertAlmostEqual(rule['percent_of_mean'] * 35.0, 10.5)
        self.assertTrue(rule['provisional'])
        self.assertTrue(rule['source_url'].startswith('https://'))
        self.assertIn('cross-check', rule['basis'])

    def test_single_leg_area_is_percent_of_mean(self):
        rule = norms.mdc_for('cop_area', stance='single_leg')
        self.assertEqual(rule['mode'], 'percent_of_mean')
        self.assertEqual(rule['value'], 0.50)
        self.assertEqual(rule['stance'], 'single_leg')

    def test_stance_aliases_and_double_leg_refusal(self):
        for stance in ('single_leg', 'single-leg', 'SLS', 'unipedal',
                       'one leg'):
            rule = norms.mdc_for('cop_velocity', stance=stance)
            self.assertIsNotNone(rule, stance)
            self.assertEqual(rule['stance'], 'single_leg', stance)
        for stance in ('double_leg', 'double-leg', 'bipedal', 'both',
                       'feet together'):
            self.assertIsNone(norms.mdc_for('cop_velocity', stance=stance),
                              stance)
            self.assertIsNone(norms.mdc_for('cop_area', stance=stance), stance)

    def test_double_leg_change_is_labelled_not_scored(self):
        result = norms.interpret_change('cop_velocity', 10.0, 30.0,
                                        stance='double_leg')
        self.assertIsNone(result['mdc'])
        self.assertEqual(result['verdict'],
                         'no published MDC for this stance; descriptive only')

    def test_unknown_stance_is_refused_not_defaulted(self):
        result = norms.interpret_change('cop_velocity', 35.0, 20.0)
        self.assertIsNone(result['mdc'])
        self.assertEqual(result['verdict'], 'MDC requires a known stance')

    def test_mctsib_rules_are_condition_named(self):
        firm = norms.mdc_for('mctsib_sway_velocity_firm')
        self.assertEqual(firm['mode'], 'absolute')
        self.assertEqual(firm['range'], (0.140, 0.285))
        self.assertIsNone(firm['stance'])

    def test_unrelated_measures_have_no_borrowed_rule(self):
        self.assertIsNone(norms.mdc_for('cop_path_length_20s'))
        self.assertIsNone(norms.mdc_for('cop_range_ap_mm'))
        self.assertIsNone(norms.mdc_for('mctsib_sway_velocity'))
        self.assertIsNone(norms.mdc_for('trunk_lean_deg_sls_squat'))
        self.assertIsNone(norms.mdc_for('does_not_exist'))
        self.assertIsNone(norms.mdc_for(None))

    def test_shape(self):
        rule = norms.mdc_for('cop_velocity', stance='single_leg')
        self.assertTrue({'mode', 'value', 'provisional', 'source_url',
                         'stance', 'measure'} <= set(rule))

    def test_delta_below_mdc_is_no_measurable_change(self):
        result = norms.interpret_change('cop_velocity', 35.0, 30.0,
                                        stance='single_leg')
        self.assertAlmostEqual(result['delta'], -5.0)
        self.assertAlmostEqual(result['mdc'], 9.0)
        self.assertEqual(result['verdict'], 'no measurable change')
        self.assertTrue(result['provisional'])

    def test_delta_above_mdc_exceeds(self):
        result = norms.interpret_change('cop_velocity', 35.0, 20.0,
                                        stance='single_leg')
        self.assertEqual(result['verdict'],
                         'change exceeds provisional MDC')

    def test_equality_is_not_a_change(self):
        result = norms.interpret_change('cop_velocity', 40.0, 31.0,
                                        stance='single_leg')
        self.assertAlmostEqual(result['mdc'], 9.0)
        self.assertEqual(result['verdict'], 'no measurable change')

    def test_area_threshold(self):
        below = norms.interpret_change('single_leg_cop_area', 3000.0, 4400.0,
                                       stance='single_leg')
        self.assertAlmostEqual(below['mdc'], 1500.0)
        self.assertEqual(below['verdict'], 'no measurable change')
        above = norms.interpret_change('single_leg_cop_area', 3000.0, 4600.0,
                                       stance='single_leg')
        self.assertEqual(above['verdict'],
                         'change exceeds provisional MDC')

    def test_absolute_threshold(self):
        below = norms.interpret_change('mctsib_sway_velocity_foam_ec',
                                       1.0, 1.5)
        self.assertAlmostEqual(below['mdc'], 1.12)
        self.assertEqual(below['verdict'], 'no measurable change')
        above = norms.interpret_change('mctsib_sway_velocity_foam_ec',
                                       1.0, 2.5)
        self.assertEqual(above['verdict'],
                         'change exceeds provisional MDC')

    def test_unknown_measure_is_labelled_not_guessed(self):
        result = norms.interpret_change('cop_path_length_20s', 300.0, 400.0)
        self.assertIsNone(result['mdc'])
        self.assertEqual(result['verdict'], 'no published MDC; descriptive only')

    def test_missing_values_are_not_assessable(self):
        for baseline, followup in ((None, 3.0), (3.0, None),
                                   (float('nan'), 3.0)):
            result = norms.interpret_change('cop_velocity', baseline, followup,
                                            stance='single_leg')
            self.assertIsNone(result['delta'])
            self.assertIn('not assessable', result['verdict'])

    def test_zero_baseline_is_not_assessable(self):
        result = norms.interpret_change('cop_area', 0.0, 5.0,
                                        stance='single_leg')
        self.assertIn('not assessable', result['verdict'])

    def test_verdicts_never_contain_a_direction_claim(self):
        cases = [(35.0, 20.0), (35.0, 30.0), (35.0, 50.0), (0.0, 1.0)]
        for baseline, followup in cases:
            verdict = norms.interpret_change('cop_velocity', baseline,
                                             followup,
                                             stance='single_leg')['verdict']
            self.assertNotIn('improv', verdict.lower())
            self.assertNotIn('declin', verdict.lower())
            self.assertNotIn('worse', verdict.lower())

    def test_known_verdicts_are_the_documented_strings(self):
        result = norms.interpret_change('cop_velocity', 35.0, 20.0,
                                        stance='single_leg')
        self.assertIn(result['verdict'], VERDICTS)


class TestCaveats(unittest.TestCase):
    """The 14-point do-not-claim list, as data the report can render."""

    def test_fourteen_entries(self):
        caveats = norms.caveats()
        self.assertIsInstance(caveats, tuple)
        self.assertEqual(len(caveats), 14)
        for item in caveats:
            self.assertIsInstance(item, str)
            self.assertTrue(item.startswith('Do not'), item)
            self.assertGreater(len(item), 30)

    def test_no_verdict_words_in_the_caveat_text(self):
        """The report renders these verbatim and must never print them."""
        for item in norms.caveats():
            self.assertNotIn('normal', item.lower())
            self.assertNotIn('abnormal', item.lower())

    def test_caveats_cover_the_required_topics(self):
        joined = ' '.join(norms.caveats()).lower()
        for topic in ('diagnosis', 'fall risk', 'return to play', 'device',
                      'path length', 'area', 'mctsib', 'romberg',
                      'single-leg', 'mdc'):
            self.assertIn(topic, joined)


if __name__ == '__main__':
    unittest.main()
