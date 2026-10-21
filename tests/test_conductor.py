"""Phase 6A conductor tests: session manifest schema + doctor-facing summary.

TEST ONLY synthetic fixtures. No hardware, no real athlete data, no network.
Run all:  .venv/Scripts/python.exe -m unittest discover -s tests
Or just this file: .venv/Scripts/python.exe -m unittest discover -s tests -p "test_conductor.py" -v
"""
from datetime import datetime, timedelta, timezone
import html
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conductor import manifest as cm
from conductor import summary as cs

INJECTION = '<script>alert(1)</script>'


def base_manifest(athlete='A-1', notes=''):
    m = cm.new_manifest(athlete, notes)
    cm.add_block(m, 'bertec', ['report.json'], notes='double-leg battery')
    cm.add_marker(m, 0.0, 'session_start')
    return m


def three_block_manifest(notes=''):
    m = cm.new_manifest('A-77', notes)
    cm.add_block(m, 'bertec', ['report.json', 'raw.csv'], notes='static battery')
    cm.add_block(m, 'neurosync', ['eeg.csv'], notes='eyes-closed rest')
    cm.add_block(m, 'runtrack', ['form-session.json'])
    cm.add_marker(m, 0.0, 'session_start')
    cm.add_marker(m, 30.5, 'eeg_start', source='app')
    return m


def fake_readers(calls=None):
    def make(app):
        def reader(paths):
            if calls is not None:
                calls.append((app, list(paths)))
            return {'app': app, 'artifact_count': len(paths)}
        return reader
    return {app: make(app) for app in ('bertec', 'neurosync', 'runtrack')}


class TempDirMixin:
    def tmpdir(self):
        handle = tempfile.TemporaryDirectory()
        self.addCleanup(handle.cleanup)
        return Path(handle.name)


class NewIdTests(unittest.TestCase):
    def test_new_id_matches_format_and_embeds_utc_timestamp(self):
        moment = datetime(2026, 10, 16, 14, 30, 2, 123456, tzinfo=timezone.utc)
        sid = cm.new_id(moment)
        self.assertTrue(sid.startswith('20261016T143002_'))
        self.assertIsNotNone(cm.ID_PATTERN.fullmatch(sid))
        other_zone = datetime(2026, 10, 16, 16, 30, 2, tzinfo=timezone(timedelta(hours=2)))
        self.assertTrue(cm.new_id(other_zone).startswith('20261016T143002_'))

    def test_new_id_suffix_is_four_lowercase_hex(self):
        sid = cm.new_id()
        self.assertIsNotNone(re.fullmatch(r'[0-9a-f]{4}', sid.split('_')[1]))

    def test_new_id_uses_current_utc_time_when_moment_omitted(self):
        before = datetime.now(timezone.utc).replace(microsecond=0)
        sid = cm.new_id()
        after = datetime.now(timezone.utc)
        stamp = datetime.strptime(sid[:15], '%Y%m%dT%H%M%S').replace(tzinfo=timezone.utc)
        self.assertLessEqual(before - timedelta(seconds=1), stamp)
        self.assertLessEqual(stamp, after + timedelta(seconds=1))

    def test_is_valid_id_accepts_and_rejects(self):
        self.assertTrue(cm.is_valid_id('20261016T143002_7f3a'))
        for bad in ('20261016T143002_7F3A', '20261016T143002_7f3', '20261016T143002',
                    '20261016T143002_7f3g', '2026-10-16T14:30:02_7f3a', '', None, 12345):
            with self.subTest(bad=bad):
                self.assertFalse(cm.is_valid_id(bad))


class NewManifestTests(unittest.TestCase):
    def test_new_manifest_core_fields(self):
        m = cm.new_manifest('A-1')
        self.assertEqual(m['schema'], 'lab-session/1')
        self.assertEqual(m['version'], 1)
        self.assertEqual(m['athlete_id'], 'A-1')
        self.assertEqual(m['operator_notes'], '')
        self.assertIsNotNone(cm.ID_PATTERN.fullmatch(m['id']))
        self.assertIsInstance(datetime.fromisoformat(m['created_at']).tzinfo, timezone)

    def test_new_manifest_strips_surrounding_whitespace_from_athlete(self):
        self.assertEqual(cm.new_manifest('  A-9  ')['athlete_id'], 'A-9')

    def test_new_manifest_rejects_blank_or_non_string_athlete(self):
        for bad in ('', '   ', 42, None):
            with self.subTest(athlete=bad):
                with self.assertRaises(ValueError):
                    cm.new_manifest(bad)

    def test_new_manifest_rejects_non_string_notes(self):
        for bad in (42, None, ['x']):
            with self.subTest(notes=bad):
                with self.assertRaises(ValueError):
                    cm.new_manifest('A-1', bad)

    def test_new_manifest_starts_empty_and_validates(self):
        m = cm.new_manifest('A-1', 'baseline')
        self.assertEqual(m['blocks'], [])
        self.assertEqual(m['markers'], [])
        self.assertIsNone(cm.validate(m))


class ValidationTests(unittest.TestCase):
    def test_validate_returns_none_for_valid_manifest(self):
        self.assertIsNone(cm.validate(base_manifest()))

    def test_validate_rejects_non_dict(self):
        for bad in ([], 'manifest', None, 42):
            with self.subTest(kind=type(bad).__name__):
                with self.assertRaises(ValueError):
                    cm.validate(bad)

    def test_validate_error_names_each_missing_field(self):
        for field in ('schema', 'id', 'created_at', 'athlete_id', 'operator_notes',
                      'blocks', 'markers', 'version'):
            m = base_manifest()
            del m[field]
            with self.subTest(field=field):
                with self.assertRaises(ValueError) as ctx:
                    cm.validate(m)
                self.assertIn("missing required field '%s'" % field, str(ctx.exception))

    def test_validate_rejects_wrong_schema(self):
        m = base_manifest()
        m['schema'] = 'lab-session/2'
        with self.assertRaises(ValueError) as ctx:
            cm.validate(m)
        self.assertIn("manifest['schema']", str(ctx.exception))

    def test_validate_rejects_bad_version(self):
        for bad in (2, '1', True, None):
            m = base_manifest()
            m['version'] = bad
            with self.subTest(version=bad):
                with self.assertRaises(ValueError) as ctx:
                    cm.validate(m)
                self.assertIn("manifest['version']", str(ctx.exception))

    def test_validate_rejects_bad_id_formats(self):
        for bad in ('20261016T143002', '20261016T143002_7f3a5', '20261016T143002_7F3A',
                    '2026-10-16T14:30:02_7f3a', '20261016T143002_7f3g', 1234, None):
            m = base_manifest()
            m['id'] = bad
            with self.subTest(mid=bad):
                with self.assertRaises(ValueError) as ctx:
                    cm.validate(m)
                self.assertIn("manifest['id']", str(ctx.exception))

    def test_validate_rejects_bad_created_at(self):
        for bad in ('not-a-date', '2026-13-01T00:00:00', '', 1234, None):
            m = base_manifest()
            m['created_at'] = bad
            with self.subTest(created_at=bad):
                with self.assertRaises(ValueError):
                    cm.validate(m)

    def test_validate_accepts_zulu_and_naive_iso_timestamps(self):
        for good in ('2026-10-16T14:30:02Z', '2026-10-16T14:30:02', '2026-10-16T14:30:02.5+00:00'):
            m = base_manifest()
            m['created_at'] = good
            with self.subTest(created_at=good):
                self.assertIsNone(cm.validate(m))

    def test_validate_rejects_blank_or_non_string_athlete(self):
        for bad in ('', '   ', 42, None):
            m = base_manifest()
            m['athlete_id'] = bad
            with self.subTest(athlete=bad):
                with self.assertRaises(ValueError):
                    cm.validate(m)

    def test_validate_rejects_non_string_operator_notes(self):
        for bad in (None, 42, {'note': 'x'}):
            m = base_manifest()
            m['operator_notes'] = bad
            with self.subTest(notes=bad):
                with self.assertRaises(ValueError):
                    cm.validate(m)

    def test_validate_rejects_blocks_not_a_list(self):
        for bad in ('x', None, 42, {'app': 'bertec'}):
            m = base_manifest()
            m['blocks'] = bad
            with self.subTest(blocks=bad):
                with self.assertRaises(ValueError) as ctx:
                    cm.validate(m)
                self.assertIn("manifest['blocks']", str(ctx.exception))

    def test_validate_rejects_block_not_a_dict(self):
        for bad in ('bertec', None, 42):
            m = base_manifest()
            m['blocks'] = [bad]
            with self.subTest(block=bad):
                with self.assertRaises(ValueError):
                    cm.validate(m)

    def test_validate_rejects_block_missing_fields(self):
        for field in ('app', 'artifacts', 'notes'):
            m = base_manifest()
            del m['blocks'][0][field]
            with self.subTest(field=field):
                with self.assertRaises(ValueError) as ctx:
                    cm.validate(m)
                self.assertIn("missing required field '%s'" % field, str(ctx.exception))

    def test_validate_rejects_unknown_app(self):
        for bad in ('foo', 'BERtEC', '', None):
            m = base_manifest()
            m['blocks'][0]['app'] = bad
            with self.subTest(app=bad):
                with self.assertRaises(ValueError) as ctx:
                    cm.validate(m)
                self.assertIn("manifest['blocks'][0]['app']", str(ctx.exception))

    def test_validate_rejects_artifacts_not_a_list(self):
        for bad in ('report.json', 42, None):
            m = base_manifest()
            m['blocks'][0]['artifacts'] = bad
            with self.subTest(artifacts=bad):
                with self.assertRaises(ValueError) as ctx:
                    cm.validate(m)
                self.assertIn("['artifacts']", str(ctx.exception))

    def test_validate_rejects_bad_artifact_entries(self):
        for bad in ([['']], [[None]], [[3]], [['ok', '']]):
            m = base_manifest()
            m['blocks'][0]['artifacts'] = bad
            with self.subTest(artifacts=bad):
                with self.assertRaises(ValueError):
                    cm.validate(m)

    def test_validate_rejects_absolute_and_traversal_artifact_paths(self):
        cases = ['../../secret.json', '..', '../secret.json', 'a/../../b.json',
                 '/etc/passwd', r'C:\Users\someone\secret.json',
                 'C:/Users/someone/secret.json', 'C:secret.json',
                 r'..\..\secret.json', r'..\secret.json', r'\\server\share\x.json',
                 'eeg/../../out.json']
        for bad in cases:
            m = base_manifest()
            m['blocks'][0]['artifacts'] = [bad]
            with self.subTest(artifact=bad):
                with self.assertRaises(ValueError) as ctx:
                    cm.validate(m)
                self.assertIn("['artifacts'][0]", str(ctx.exception))

    def test_validate_accepts_plain_nested_relative_paths(self):
        for good in ('report.json', 'eeg/raw.csv', './report.json', 'a/b/c.json',
                     r'eeg\raw.csv'):
            m = base_manifest()
            m['blocks'][0]['artifacts'] = [good]
            with self.subTest(artifact=good):
                self.assertIsNone(cm.validate(m))

    def test_validate_rejects_block_notes_non_string(self):
        for bad in (42, None):
            m = base_manifest()
            m['blocks'][0]['notes'] = bad
            with self.subTest(notes=bad):
                with self.assertRaises(ValueError):
                    cm.validate(m)

    def test_validate_rejects_markers_not_a_list(self):
        for bad in ('x', None, 42):
            m = base_manifest()
            m['markers'] = bad
            with self.subTest(markers=bad):
                with self.assertRaises(ValueError) as ctx:
                    cm.validate(m)
                self.assertIn("manifest['markers']", str(ctx.exception))

    def test_validate_rejects_marker_not_a_dict(self):
        for bad in ('session_start', 42, None):
            m = base_manifest()
            m['markers'] = [bad]
            with self.subTest(marker=bad):
                with self.assertRaises(ValueError):
                    cm.validate(m)

    def test_validate_rejects_marker_missing_fields(self):
        for field in ('t_s', 'name', 'source'):
            m = base_manifest()
            del m['markers'][0][field]
            with self.subTest(field=field):
                with self.assertRaises(ValueError) as ctx:
                    cm.validate(m)
                self.assertIn("missing required field '%s'" % field, str(ctx.exception))

    def test_validate_rejects_bad_marker_t_s(self):
        for bad in ('1.5', True, None, float('nan'), float('inf')):
            m = base_manifest()
            m['markers'][0]['t_s'] = bad
            with self.subTest(t_s=bad):
                with self.assertRaises(ValueError) as ctx:
                    cm.validate(m)
                self.assertIn("['t_s']", str(ctx.exception))

    def test_validate_rejects_bad_marker_name(self):
        for bad in ('', '   ', 42, None):
            m = base_manifest()
            m['markers'][0]['name'] = bad
            with self.subTest(name=bad):
                with self.assertRaises(ValueError):
                    cm.validate(m)

    def test_validate_rejects_bad_marker_source(self):
        for bad in ('operator', '', None):
            m = base_manifest()
            m['markers'][0]['source'] = bad
            with self.subTest(source=bad):
                with self.assertRaises(ValueError) as ctx:
                    cm.validate(m)
                self.assertIn("['source']", str(ctx.exception))

    def test_validate_allows_unknown_extra_keys(self):
        m = base_manifest()
        m['extra_top'] = {'anything': [1, 2]}
        m['blocks'][0]['extra_block'] = 'forward-compat'
        m['markers'][0]['extra_marker'] = True
        self.assertIsNone(cm.validate(m))


class PersistenceTests(TempDirMixin, unittest.TestCase):
    def test_save_load_roundtrip(self):
        m = cm.new_manifest('A-1', 'line one\nline two \u00fcnicode \u03a9')
        cm.add_block(m, 'neurosync', ['eeg/raw.csv', 'eeg/notes.txt'], notes='rest EC')
        cm.add_marker(m, 1.5, 'eeg_start', source='app')
        path = self.tmpdir() / 'session' / 'manifest.json'
        cm.save(m, path)
        loaded = cm.load(path)
        self.assertEqual(loaded, m)
        self.assertIsNone(cm.validate(loaded))

    def test_save_creates_missing_parent_directories(self):
        target = self.tmpdir() / 'sessions' / 'abc' / 'manifest.json'
        cm.save(base_manifest(), target)
        self.assertTrue(target.is_file())

    def test_save_leaves_no_temp_file_behind(self):
        directory = self.tmpdir()
        target = directory / 'manifest.json'
        cm.save(base_manifest(), target)
        self.assertEqual(sorted(p.name for p in directory.iterdir()), ['manifest.json'])

    def test_save_overwrites_existing_file(self):
        target = self.tmpdir() / 'manifest.json'
        cm.save(cm.new_manifest('A-1'), target)
        cm.save(cm.new_manifest('B-2'), target)
        self.assertEqual(cm.load(target)['athlete_id'], 'B-2')

    def test_save_refuses_invalid_manifest_and_writes_nothing(self):
        m = base_manifest()
        del m['blocks']
        target = self.tmpdir() / 'manifest.json'
        with self.assertRaises(ValueError):
            cm.save(m, target)
        self.assertFalse(target.exists())

    def test_save_output_is_utf8_lf_json(self):
        target = self.tmpdir() / 'manifest.json'
        cm.save(base_manifest(notes='d\u00e9j\u00e0 vu'), target)
        raw = target.read_bytes()
        self.assertNotIn(b'\r\n', raw)
        self.assertEqual(json.loads(raw.decode('utf-8'))['operator_notes'], 'd\u00e9j\u00e0 vu')

    def test_load_missing_file_raises_oserror(self):
        with self.assertRaises(OSError):
            cm.load(self.tmpdir() / 'nope.json')

    def test_load_rejects_invalid_json(self):
        target = self.tmpdir() / 'manifest.json'
        target.write_text('{ not json', encoding='utf-8')
        with self.assertRaises(ValueError):
            cm.load(target)

    def test_load_rejects_valid_json_that_is_not_a_manifest(self):
        target = self.tmpdir() / 'manifest.json'
        target.write_text(json.dumps({'hello': 'world'}), encoding='utf-8')
        with self.assertRaises(ValueError):
            cm.load(target)


class AssemblyTests(TempDirMixin, unittest.TestCase):
    def test_add_block_appends_and_returns_same_manifest(self):
        m = cm.new_manifest('A-1')
        returned = cm.add_block(m, 'bertec', ['report.json'], notes='battery')
        self.assertIs(returned, m)
        self.assertEqual(m['blocks'][0]['app'], 'bertec')
        self.assertEqual(m['blocks'][0]['artifacts'], ['report.json'])
        self.assertEqual(m['blocks'][0]['notes'], 'battery')

    def test_add_block_accepts_tuple_artifacts_and_default_notes(self):
        m = cm.new_manifest('A-1')
        cm.add_block(m, 'runtrack', ('a.json', 'b.json'))
        self.assertEqual(m['blocks'][0]['artifacts'], ['a.json', 'b.json'])
        self.assertEqual(m['blocks'][0]['notes'], '')
        self.assertIsInstance(m['blocks'][0]['artifacts'], list)

    def test_add_block_rejects_bad_app(self):
        m = cm.new_manifest('A-1')
        with self.assertRaises(ValueError) as ctx:
            cm.add_block(m, 'myapp', ['x.json'])
        self.assertIn("'myapp'", str(ctx.exception))
        self.assertEqual(m['blocks'], [])

    def test_add_block_rejects_single_string_artifacts(self):
        m = cm.new_manifest('A-1')
        with self.assertRaises(ValueError):
            cm.add_block(m, 'bertec', 'report.json')

    def test_add_block_rejects_bad_artifact_entries_and_notes(self):
        m = cm.new_manifest('A-1')
        with self.assertRaises(ValueError):
            cm.add_block(m, 'bertec', [''])
        with self.assertRaises(ValueError):
            cm.add_block(m, 'bertec', ['ok.json', 42])
        with self.assertRaises(ValueError):
            cm.add_block(m, 'bertec', ['ok.json'], notes=42)
        self.assertEqual(m['blocks'], [])

    def test_add_block_rejects_absolute_and_traversal_artifact_paths(self):
        for bad in ('../../secret.json', '/etc/passwd', r'..\..\secret.json',
                    'C:\\tmp\\secret.json'):
            m = cm.new_manifest('A-1')
            with self.subTest(artifact=bad):
                with self.assertRaises(ValueError) as ctx:
                    cm.add_block(m, 'bertec', [bad])
                self.assertIn('artifacts[0]', str(ctx.exception))
                self.assertEqual(m['blocks'], [])

    def test_add_block_requires_a_block_list(self):
        with self.assertRaises(ValueError):
            cm.add_block({}, 'bertec', ['report.json'])

    def test_add_marker_defaults_to_conductor_source(self):
        m = cm.new_manifest('A-1')
        returned = cm.add_marker(m, 0.25, 'zero_done')
        self.assertIs(returned, m)
        self.assertEqual(m['markers'][0], {'t_s': 0.25, 'name': 'zero_done', 'source': 'conductor'})

    def test_add_marker_accepts_app_source_and_zero_time(self):
        m = cm.new_manifest('A-1')
        cm.add_marker(m, 0.0, 'stream_start', source='app')
        self.assertEqual(m['markers'][0]['source'], 'app')

    def test_add_marker_rejects_bad_inputs(self):
        m = cm.new_manifest('A-1')
        with self.assertRaises(ValueError):
            cm.add_marker(m, 'soon', 'x')
        with self.assertRaises(ValueError):
            cm.add_marker(m, True, 'x')
        with self.assertRaises(ValueError):
            cm.add_marker(m, float('nan'), 'x')
        with self.assertRaises(ValueError):
            cm.add_marker(m, 1.0, '')
        with self.assertRaises(ValueError):
            cm.add_marker(m, 1.0, 'x', source='operator')
        self.assertEqual(m['markers'], [])

    def test_add_marker_preserves_insertion_order(self):
        m = cm.new_manifest('A-1')
        cm.add_marker(m, 5.0, 'c')
        cm.add_marker(m, 1.0, 'a')
        cm.add_marker(m, 3.0, 'b')
        self.assertEqual([item['name'] for item in m['markers']], ['c', 'a', 'b'])

    def test_session_dir_returns_created_path_and_is_idempotent(self):
        root = self.tmpdir() / 'sessions'
        m = cm.new_manifest('A-1')
        first = cm.session_dir(root, m)
        second = cm.session_dir(root, m)
        self.assertEqual(first, root / m['id'])
        self.assertTrue(first.is_dir())
        self.assertEqual(first, second)

    def test_session_dir_rejects_hostile_id_before_creating_anything(self):
        root = self.tmpdir() / 'sessions'
        m = cm.new_manifest('A-1')
        m['id'] = '../evil'
        with self.assertRaises(ValueError):
            cm.session_dir(root, m)
        self.assertFalse(root.exists())
        self.assertFalse((root.parent / 'evil').exists())

    def test_session_dir_end_to_end_save_and_load(self):
        root = self.tmpdir() / 'sessions'
        m = base_manifest()
        session = cm.session_dir(root, m)
        cm.save(m, session / cm.MANIFEST_FILENAME)
        self.assertEqual(cm.load(session / cm.MANIFEST_FILENAME)['id'], m['id'])


class SummarySectionsTests(TempDirMixin, unittest.TestCase):
    def test_summary_document_skeleton(self):
        page = cs.build_summary_html(three_block_manifest(), readers=fake_readers())
        self.assertTrue(page.startswith('<!DOCTYPE html>'))
        self.assertIn('<html lang="en">', page)
        self.assertIn('<meta charset="utf-8">', page)
        self.assertIn('<style>', page)
        self.assertIn('</html>', page)

    def test_summary_contains_required_sections(self):
        m = three_block_manifest(notes='pre-season baseline')
        page = cs.build_summary_html(m, readers=fake_readers())
        self.assertIn('Lab Session Summary \u2014 A-77', page)
        self.assertIn(m['id'], page)
        self.assertIn(m['created_at'], page)
        self.assertIn('pre-season baseline', page)
        self.assertIn('<h2>Operator notes</h2>', page)
        self.assertIn('<h2>Blocks</h2>', page)
        self.assertIn('Method &amp; Limitations', page)
        self.assertNotIn('Method & Limitations', page)
        self.assertIn('Requires Physician Review', page)
        self.assertIn('Generated by the Hermes multi-modal conductor', page)
        self.assertIn('lab-session/1', page)

    def test_summary_lists_artifacts_and_block_notes(self):
        page = cs.build_summary_html(three_block_manifest(), readers=fake_readers())
        self.assertIn('<code>report.json</code>', page)
        self.assertIn('<code>raw.csv</code>', page)
        self.assertIn('static battery', page)
        self.assertIn('eyes-closed rest', page)
        self.assertIn('None supplied.', page)

    def test_summary_renders_facts_from_injected_readers(self):
        page = cs.build_summary_html(three_block_manifest(), readers=fake_readers())
        self.assertIn('<th scope="row">artifact_count</th>', page)
        self.assertIn('<td>2</td>', page)
        self.assertIn('<td>1</td>', page)

    def test_summary_reader_receives_artifact_paths_in_block_order(self):
        calls = []
        cs.build_summary_html(three_block_manifest(), readers=fake_readers(calls))
        self.assertEqual([app for app, _ in calls], ['bertec', 'neurosync', 'runtrack'])
        self.assertEqual(calls[0][1], ['report.json', 'raw.csv'])
        self.assertEqual(calls[1][1], ['eeg.csv'])
        self.assertEqual(calls[2][1], ['form-session.json'])

    def test_summary_missing_app_reader_degrades(self):
        page = cs.build_summary_html(three_block_manifest(), readers={})
        self.assertIn('no reader available; artifact recorded', page)

    def test_summary_reader_exception_degrades_without_raising(self):
        def exploding(paths):
            raise RuntimeError('boom')

        page = cs.build_summary_html(three_block_manifest(), readers={'bertec': exploding})
        self.assertIn('reader failed; artifact recorded', page)
        self.assertIn('Requires Physician Review', page)

    def test_summary_reader_returning_none_degrades(self):
        page = cs.build_summary_html(three_block_manifest(), readers={'bertec': lambda paths: None})
        self.assertIn('artifact recorded', page)

    def test_summary_requires_physician_review_lists_blocks_in_order(self):
        page = cs.build_summary_html(three_block_manifest(), readers=fake_readers())
        review = page[page.index('Requires Physician Review'):]
        self.assertLess(review.index('Bertec Balance Plate'), review.index('NeuroSync EEG'))
        self.assertLess(review.index('NeuroSync EEG'), review.index('RunTrack Running Form'))
        self.assertIn('Nothing in this summary clears an athlete for participation.', page)

    def test_summary_empty_manifest_renders_placeholders(self):
        page = cs.build_summary_html(cm.new_manifest('A-1'))
        self.assertIn('No blocks recorded in this session.', page)
        self.assertIn('No markers recorded.', page)
        self.assertIn('nothing to review here', page)

    def test_summary_markers_section_lists_markers(self):
        page = cs.build_summary_html(three_block_manifest(), readers=fake_readers())
        self.assertIn('<td>0</td><td>session_start</td><td>conductor</td>', page)
        self.assertIn('<td>30.5</td><td>eeg_start</td><td>app</td>', page)

    def test_summary_without_markers_shows_placeholder(self):
        m = cm.new_manifest('A-1')
        cm.add_block(m, 'bertec', ['report.json'])
        page = cs.build_summary_html(m, readers=fake_readers())
        self.assertIn('No markers recorded.', page)

    def test_summary_generated_at_defaults_to_manifest_created_at(self):
        m = three_block_manifest()
        page = cs.build_summary_html(m, readers=fake_readers())
        self.assertIn('Generated at %s' % m['created_at'], page)

    def test_summary_generated_at_override_appears_in_footer(self):
        page = cs.build_summary_html(three_block_manifest(), readers=fake_readers(),
                                     generated_at='2030-01-01T00:00:00+00:00')
        self.assertIn('Generated at 2030-01-01T00:00:00+00:00.', page)

    def test_summary_is_deterministic(self):
        m = three_block_manifest(notes='same input')
        first = cs.build_summary_html(m, readers=fake_readers())
        second = cs.build_summary_html(m, readers=fake_readers())
        self.assertEqual(first, second)

    def test_summary_does_not_mutate_manifest(self):
        m = three_block_manifest()
        snapshot = json.loads(json.dumps(m))
        cs.build_summary_html(m, readers=fake_readers())
        self.assertEqual(m, snapshot)

    def test_summary_rejects_invalid_manifest(self):
        with self.assertRaises(ValueError):
            cs.build_summary_html({'schema': 'lab-session/1'})


class SummaryEscapingTests(unittest.TestCase):
    def test_operator_notes_injection_is_escaped_never_raw(self):
        page = cs.build_summary_html(base_manifest(notes=INJECTION), readers={})
        self.assertIn(html.escape(INJECTION), page)
        self.assertNotIn(INJECTION, page)
        self.assertNotIn('<script', page)

    def test_block_notes_artifact_paths_and_marker_names_are_escaped(self):
        m = cm.new_manifest('A-1', 'plain')
        cm.add_block(m, 'runtrack', ['form<img src=x onerror=y>.json'], notes=INJECTION)
        cm.add_marker(m, 3.0, '<b>boom</b>')
        page = cs.build_summary_html(m, readers={})
        self.assertIn('&lt;img src=x onerror=y&gt;', page)
        self.assertIn(html.escape(INJECTION), page)
        self.assertIn('&lt;b&gt;boom&lt;/b&gt;', page)
        self.assertNotIn('<img', page)
        self.assertNotIn('<b>boom</b>', page)

    def test_athlete_id_is_escaped_in_heading_and_title(self):
        athlete = '<img src=x onerror=alert(1)>'
        page = cs.build_summary_html(base_manifest(athlete=athlete), readers={})
        self.assertIn(html.escape(athlete), page)
        self.assertNotIn('<img', page)

    def test_marker_source_values_are_escaped_in_page(self):
        m = cm.new_manifest('A-1')
        cm.add_block(m, 'bertec', ['report.json'])
        cm.add_marker(m, 1.0, 'cue', source='app')
        page = cs.build_summary_html(m, readers={})
        self.assertNotIn('<script', page)
        self.assertEqual(page.count('Requires Physician Review'), 1)


class DefaultReaderTests(TempDirMixin, unittest.TestCase):
    def write_report_json(self, directory, name='report.json', **overrides):
        report = {
            'id': '20260924T182031_75355601',
            'athlete': 'A-77',
            'started_at': '2026-09-24T18:20:31.207677+00:00',
            'duration_s': 5.043,
            'sample_count': 5044,
            'not_a_summary_field': 'ignored',
        }
        report.update(overrides)
        target = directory / name
        target.write_text(json.dumps(report), encoding='utf-8')
        return target

    def test_default_readers_cover_all_apps(self):
        readers = cs.default_readers()
        self.assertEqual(set(readers), set(cm.APPS))
        for reader in readers.values():
            self.assertTrue(callable(reader))

    def test_bertec_reader_surfaces_report_json_scalars(self):
        directory = self.tmpdir()
        self.write_report_json(directory)
        facts = cs.default_readers(session_root=directory)['bertec'](['report.json'])
        self.assertEqual(facts['artifact'], 'report.json')
        self.assertEqual(facts['session_id'], '20260924T182031_75355601')
        self.assertEqual(facts['athlete'], 'A-77')
        self.assertEqual(facts['recorded_at'], '2026-09-24T18:20:31.207677+00:00')
        self.assertEqual(facts['duration'], 5.043)
        self.assertNotIn('not_a_summary_field', facts)

    def test_bertec_reader_degrades_on_missing_file(self):
        facts = cs.default_readers()['bertec'](['no-such-artifact-anywhere.json'])
        self.assertEqual(facts, {'note': 'artifact recorded'})

    def test_bertec_reader_degrades_on_corrupt_json(self):
        directory = self.tmpdir()
        (directory / 'broken.json').write_text('{ not json', encoding='utf-8')
        facts = cs.default_readers(session_root=directory)['bertec'](['broken.json'])
        self.assertEqual(facts, {'note': 'artifact recorded'})

    def test_bertec_reader_degrades_on_non_object_json(self):
        directory = self.tmpdir()
        (directory / 'array.json').write_text('[1, 2, 3]', encoding='utf-8')
        facts = cs.default_readers(session_root=directory)['bertec'](['array.json'])
        self.assertEqual(facts, {'note': 'artifact recorded'})

    def test_bertec_reader_skips_non_json_and_uses_first_readable_json(self):
        directory = self.tmpdir()
        (directory / 'raw.csv').write_text('t,fz\n0,1\n', encoding='utf-8')
        self.write_report_json(directory, name='second.json', athlete='A-99')
        facts = cs.default_readers(session_root=directory)['bertec'](['raw.csv', 'second.json'])
        self.assertEqual(facts['artifact'], 'second.json')
        self.assertEqual(facts['athlete'], 'A-99')

    def test_generic_reader_reports_size_and_basic_json_fields(self):
        directory = self.tmpdir()
        (directory / 'clip.mp4').write_bytes(b'0123456789')
        (directory / 'form.json').write_text(json.dumps({'id': 'RT-9', 'athlete': 'A-77'}),
                                             encoding='utf-8')
        facts = cs.default_readers(session_root=directory)['runtrack'](['clip.mp4', 'form.json'])
        self.assertEqual(facts['clip.mp4'], '10 bytes')
        self.assertEqual(facts['form.json: id'], 'RT-9')
        self.assertEqual(facts['form.json: athlete'], 'A-77')

    def test_generic_reader_reports_missing_artifact(self):
        facts = cs.default_readers()['neurosync'](['gone.bin'])
        self.assertEqual(facts, {'gone.bin': 'missing'})

    def test_generic_reader_degrades_on_empty_artifact_list(self):
        self.assertEqual(cs.default_readers()['neurosync']([]), {'note': 'recorded'})

    def test_build_summary_resolves_relative_artifacts_against_session_root(self):
        directory = self.tmpdir()
        self.write_report_json(directory, athlete='A-77')
        m = cm.new_manifest('A-99')
        cm.add_block(m, 'bertec', ['report.json'])
        page = cs.build_summary_html(m, session_root=directory)
        self.assertIn('<td>A-77</td>', page)
        self.assertIn('session_id', page)

    def test_build_summary_default_readers_never_raise_on_missing_files(self):
        m = base_manifest()
        m['blocks'][0]['artifacts'] = ['nope.json', 'also-missing.bin']
        cm.add_block(m, 'runtrack', ['nope.mp4'])
        page = cs.build_summary_html(m, session_root=self.tmpdir())
        self.assertIn('artifact recorded', page)
        self.assertIn('missing', page)
        self.assertIn('Requires Physician Review', page)

    def test_build_summary_without_session_root_degrades_cleanly(self):
        m = base_manifest()
        m['blocks'][0]['artifacts'] = ['nope.json']
        page = cs.build_summary_html(m)
        self.assertIn('artifact recorded', page)
        self.assertIn('Lab Session Summary', page)


class HostilePathTests(TempDirMixin, unittest.TestCase):
    """M7: a manifest is an exchange artifact — readers must stay inside it."""

    def make_decoy(self, directory):
        """Create a session root plus a decoy JSON file OUTSIDE it."""
        outside = directory / 'outside'
        outside.mkdir()
        decoy = outside / 'secret.json'
        decoy.write_text(json.dumps({'id': 'DECOY-ID-42', 'athlete': 'DECOY-ATHLETE',
                                     'recorded_at': 'DECOY-TIME', 'duration_s': 999.0}),
                         encoding='utf-8')
        root = directory / 'session'
        root.mkdir()
        return root, decoy

    def test_bertec_reader_refuses_to_read_outside_the_session_root(self):
        root, decoy = self.make_decoy(self.tmpdir())
        reader = cs.default_readers(session_root=root)['bertec']
        for entry in ('../outside/secret.json', '../../outside/secret.json',
                      str(decoy), r'..\outside\secret.json'):
            with self.subTest(entry=entry):
                facts = reader([entry])
                self.assertEqual(facts, {'note': 'unresolved artifact'})
                self.assertNotIn('DECOY', json.dumps(facts))

    def test_generic_reader_flags_outside_artifacts_without_reading_them(self):
        root, decoy = self.make_decoy(self.tmpdir())
        (root / 'clip.mp4').write_bytes(b'0123456789')
        facts = cs.default_readers(session_root=root)['runtrack'](
            ['clip.mp4', '../outside/secret.json'])
        self.assertEqual(facts['clip.mp4'], '10 bytes')
        self.assertEqual(facts['../outside/secret.json'], 'unresolved artifact')
        self.assertNotIn('DECOY', json.dumps(facts))
        self.assertNotIn(str(decoy.stat().st_size), json.dumps(facts))

    def test_dotdot_is_refused_even_when_it_would_land_back_inside(self):
        root, _decoy = self.make_decoy(self.tmpdir())
        (root / 'report.json').write_text(json.dumps({'id': 'OK-1', 'athlete': 'A-1'}),
                                          encoding='utf-8')
        reader = cs.default_readers(session_root=root)['bertec']
        self.assertEqual(reader(['report.json'])['session_id'], 'OK-1')
        self.assertEqual(reader(['./report.json'])['session_id'], 'OK-1')
        self.assertEqual(reader(['sub/../report.json']),
                         {'note': 'unresolved artifact'})

    def test_hostile_manifest_is_rejected_before_any_render(self):
        root, _decoy = self.make_decoy(self.tmpdir())
        m = base_manifest()
        m['blocks'][0]['artifacts'] = ['../../outside/secret.json']
        with self.assertRaises(ValueError):
            cs.build_summary_html(m, session_root=root)
        # A clean manifest rendered against the same root never shows the decoy.
        page = cs.build_summary_html(base_manifest(), session_root=root)
        for secret in ('DECOY-ID-42', 'DECOY-ATHLETE', 'DECOY-TIME'):
            self.assertNotIn(secret, page)

    def test_load_rejects_a_hostile_manifest_from_disk(self):
        root, _decoy = self.make_decoy(self.tmpdir())
        m = base_manifest()
        m['blocks'][0]['artifacts'] = ['../outside/secret.json']
        path = root / 'manifest.json'
        with self.assertRaises(ValueError):
            cm.save(m, path)
        self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
