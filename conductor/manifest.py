"""Session manifest schema v1 — local multi-modal Session Conductor (roadmap Phase 6A).

One manifest ASSOCIATES three independent, separately-graded evaluations into a
single session record. It never fuses them, never scores anything, and carries
no diagnostic meaning — it is bookkeeping for a physician-facing summary
(see research/NEURO-BERTEC-INTEGRATION.md, Phase 6A).

Schema v1, stored as UTF-8 JSON::

    {
      "schema": "lab-session/1",
      "id": "<YYYYMMDDTHHMMSS>_<4hex>",            # e.g. "20261016T143002_7f3a"
      "created_at": "2026-10-16T14:30:02.123456+00:00",  # ISO-8601, UTC
      "athlete_id": "A-0123",                      # non-empty string
      "operator_notes": "free text",               # untrusted text; escape on render
      "blocks": [
        {
          "app": "bertec" | "neurosync" | "runtrack",
          "artifacts": ["report.json", "raw.csv"], # relative to the session dir;
                                                   # never absolute, no '..' parts
          "notes": "free text"
        }
      ],
      "markers": [
        {"t_s": 12.5, "name": "eeg_baseline_start", "source": "conductor" | "app"}
      ],
      "version": 1
    }

File contract (research doc §3): ``sessions/<id>/manifest.json`` plus per-app
raw artifacts, all on local disk — no cloud, no external accounts. The default
session root is ``SESSIONS_ROOT`` (``<repo>/sessions/``); any other root may be
passed to :func:`session_dir` (tests use temp dirs).

Usage::

    m = new_manifest('A-0123', 'pre-season baseline')
    add_block(m, 'bertec', ['report.json'], notes='double-leg EC battery')
    add_marker(m, 0.0, 'session_start')
    save(m, session_dir(SESSIONS_ROOT, m) / MANIFEST_FILENAME)

All writers here are atomic (temp file + ``os.replace``) and deterministic.
:func:`validate` raises ``ValueError`` naming the first offending field.
Pure standard library. Research aid — requires physician review for any
clinical interpretation.
"""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import secrets

SCHEMA_NAME = 'lab-session/1'
SCHEMA_VERSION = 1
APPS = ('bertec', 'neurosync', 'runtrack')
MARKER_SOURCES = ('conductor', 'app')
ID_PATTERN = re.compile(r'[0-9]{8}T[0-9]{6}_[0-9a-f]{4}')
MANIFEST_FILENAME = 'manifest.json'

#: Default session root: the repo's ``sessions/`` directory. Not created at import time.
SESSIONS_ROOT = Path(__file__).resolve().parents[1] / 'sessions'

_REQUIRED_FIELDS = ('schema', 'id', 'created_at', 'athlete_id', 'operator_notes',
                    'blocks', 'markers', 'version')


def _typename(value):
    return type(value).__name__


def new_id(moment=None):
    """Return a fresh session id ``<YYYYMMDDTHHMMSS>_<4hex>``.

    The timestamp is UTC (``moment`` may override it for tests); the 4 hex
    characters come from ``secrets``. 4 hex = 65536 values per second, which is
    collision-safe for human-paced lab sessions; do not programmatically create
    more than a handful of sessions per second per directory.
    """
    moment = moment or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%S') + '_' + secrets.token_hex(2)


def is_valid_id(value):
    """True when ``value`` matches the v1 session id format exactly."""
    return isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None


_DRIVE_PATTERN = re.compile(r'^[A-Za-z]:')


def artifact_path_violation(artifact):
    """Return a human-readable violation string for an artifact path, or None.

    Artifact paths are relative to the session directory, and a manifest is an
    exchange artifact: a hostile or shared manifest must not be able to point a
    reader at a file outside its own session. Rejected here: absolute paths
    (POSIX or Windows), drive letters, UNC/rooted prefixes and any ``..``
    component — including the backslash spelling ``..\\``. The summary reader
    re-checks containment after resolving, so both layers must pass.
    """
    if not isinstance(artifact, str):
        return 'must be a path string, got %s' % _typename(artifact)
    text = artifact.strip()
    if not text:
        return 'must be a non-empty path string'
    if text.startswith(('/', '\\')):
        return ('must be a path relative to the session directory, got the absolute '
                'path %r' % artifact)
    if _DRIVE_PATTERN.match(text):
        return 'must not carry a drive letter, got %r' % artifact
    if '..' in text.replace('\\', '/').split('/'):
        return "must not contain a '..' component, got %r" % artifact
    return None


def new_manifest(athlete_id, operator_notes=''):
    """Create an empty schema-v1 manifest.

    ``athlete_id`` must be a non-empty string (stored stripped); ``operator_notes``
    must be a string (kept verbatim — it is untrusted text and must be escaped by
    any renderer).
    """
    if not isinstance(athlete_id, str) or not athlete_id.strip():
        raise ValueError('athlete_id must be a non-empty string, got %r' % (athlete_id,))
    if not isinstance(operator_notes, str):
        raise ValueError('operator_notes must be a string, got %s' % _typename(operator_notes))
    return {
        'schema': SCHEMA_NAME,
        'id': new_id(),
        'created_at': datetime.now(timezone.utc).isoformat(),
        'athlete_id': athlete_id.strip(),
        'operator_notes': operator_notes,
        'blocks': [],
        'markers': [],
        'version': SCHEMA_VERSION,
    }


def validate(manifest):
    """Validate a manifest against schema v1; raise ValueError on the first violation.

    Checks, in order: object type, required fields, schema name, version, id
    format, ISO-8601 ``created_at``, non-empty ``athlete_id``, string
    ``operator_notes``, every block (``app`` enum, artifact path strings relative
    to the session directory — absolute paths, drive letters and ``..``
    components are rejected — notes type) and every marker (finite numeric
    ``t_s``, non-empty ``name``, ``source`` enum). Unknown extra keys are allowed
    for forward compatibility. Returns None when the manifest is valid.
    """
    if not isinstance(manifest, dict):
        raise ValueError('manifest must be a JSON object (dict), got %s' % _typename(manifest))

    for field in _REQUIRED_FIELDS:
        if field not in manifest:
            raise ValueError("manifest is missing required field '%s'" % field)

    if manifest['schema'] != SCHEMA_NAME:
        raise ValueError('manifest[\'schema\'] must be %r, got %r' % (SCHEMA_NAME, manifest['schema']))

    if isinstance(manifest['version'], bool) or manifest['version'] != SCHEMA_VERSION:
        raise ValueError('manifest[\'version\'] must be the integer %d, got %r'
                         % (SCHEMA_VERSION, manifest['version']))

    if not is_valid_id(manifest['id']):
        raise ValueError("manifest['id'] must match <YYYYMMDDTHHMMSS>_<4hex> "
                         "(e.g. '20261016T143002_7f3a'), got %r" % (manifest['id'],))

    created_at = manifest['created_at']
    if not isinstance(created_at, str):
        raise ValueError("manifest['created_at'] must be an ISO-8601 string, got %s"
                         % _typename(created_at))
    try:
        datetime.fromisoformat(created_at)
    except ValueError:
        raise ValueError("manifest['created_at'] is not a valid ISO-8601 timestamp: %r"
                         % (created_at,)) from None

    athlete_id = manifest['athlete_id']
    if not isinstance(athlete_id, str) or not athlete_id.strip():
        raise ValueError("manifest['athlete_id'] must be a non-empty string, got %r" % (athlete_id,))

    if not isinstance(manifest['operator_notes'], str):
        raise ValueError("manifest['operator_notes'] must be a string, got %s"
                         % _typename(manifest['operator_notes']))

    if not isinstance(manifest['blocks'], list):
        raise ValueError("manifest['blocks'] must be a list, got %s" % _typename(manifest['blocks']))
    for index, block in enumerate(manifest['blocks']):
        _validate_block(block, index)

    if not isinstance(manifest['markers'], list):
        raise ValueError("manifest['markers'] must be a list, got %s" % _typename(manifest['markers']))
    for index, marker in enumerate(manifest['markers']):
        _validate_marker(marker, index)

    return None


def _validate_block(block, index):
    where = "manifest['blocks'][%d]" % index
    if not isinstance(block, dict):
        raise ValueError('%s must be an object (dict), got %s' % (where, _typename(block)))
    for field in ('app', 'artifacts', 'notes'):
        if field not in block:
            raise ValueError("%s is missing required field '%s'" % (where, field))
    if block['app'] not in APPS:
        raise ValueError("%s['app'] must be one of %s, got %r"
                         % (where, ', '.join(repr(app) for app in APPS), block['app']))
    if not isinstance(block['artifacts'], list):
        raise ValueError("%s['artifacts'] must be a list of relative path strings, got %s"
                         % (where, _typename(block['artifacts'])))
    for path_index, artifact in enumerate(block['artifacts']):
        if not isinstance(artifact, str) or not artifact.strip():
            raise ValueError("%s['artifacts'][%d] must be a non-empty path string, got %r"
                             % (where, path_index, artifact))
        problem = artifact_path_violation(artifact)
        if problem is not None:
            raise ValueError("%s['artifacts'][%d] %s" % (where, path_index, problem))
    if not isinstance(block['notes'], str):
        raise ValueError("%s['notes'] must be a string, got %s" % (where, _typename(block['notes'])))


def _validate_marker(marker, index):
    where = "manifest['markers'][%d]" % index
    if not isinstance(marker, dict):
        raise ValueError('%s must be an object (dict), got %s' % (where, _typename(marker)))
    for field in ('t_s', 'name', 'source'):
        if field not in marker:
            raise ValueError("%s is missing required field '%s'" % (where, field))
    t_s = marker['t_s']
    if isinstance(t_s, bool) or not isinstance(t_s, (int, float)):
        raise ValueError("%s['t_s'] must be a finite number of seconds, got %r" % (where, t_s))
    if not math.isfinite(t_s):
        raise ValueError("%s['t_s'] must be finite, got %r" % (where, t_s))
    name = marker['name']
    if not isinstance(name, str) or not name.strip():
        raise ValueError("%s['name'] must be a non-empty string, got %r" % (where, name))
    if marker['source'] not in MARKER_SOURCES:
        raise ValueError("%s['source'] must be one of %s, got %r"
                         % (where, ', '.join(repr(source) for source in MARKER_SOURCES), marker['source']))


def save(manifest, path):
    """Atomically write ``manifest`` to ``path`` and return the ``Path``.

    The manifest is validated first, so an invalid manifest is never persisted.
    Parent directories are created on demand. The write goes to
    ``<path>.tmp`` in the same directory and is moved into place with
    ``os.replace`` (atomic on one volume); on any failure the temp file is
    removed. Output is deterministic: UTF-8, LF newlines, 2-space indent.
    """
    validate(manifest)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(manifest, indent=2, ensure_ascii=False, allow_nan=False) + '\n'
    tmp_path = path.parent / (path.name + '.tmp')
    try:
        with open(tmp_path, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path


def load(path):
    """Read and validate a manifest from ``path``.

    Raises ``OSError`` when the file cannot be read, ``ValueError`` when the
    content is not valid JSON or not a valid ``lab-session/1`` manifest.
    """
    path = Path(path)
    text = path.read_text(encoding='utf-8')
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError('%s is not valid JSON: %s' % (path, exc)) from exc
    validate(data)
    return data


def add_block(manifest, app, artifacts, notes=''):
    """Append an app block to ``manifest`` and return the manifest (chainable).

    ``app`` must be one of ``APPS``. ``artifacts`` is a sequence of path strings
    relative to the session directory (existence is not checked here; a single
    path string is rejected as a likely mistake). Absolute paths, drive letters
    and ``..`` components are rejected here *and* in :func:`validate`, so a
    hostile path can never be persisted. Multiple blocks for the same app are
    allowed (e.g. a re-test within one session) and keep insertion order.
    """
    if not isinstance(manifest, dict) or not isinstance(manifest.get('blocks'), list):
        raise ValueError("manifest must be a dict with a 'blocks' list; "
                         'use new_manifest() or load() first')
    if app not in APPS:
        raise ValueError('app must be one of %s, got %r'
                         % (', '.join(repr(candidate) for candidate in APPS), app))
    if isinstance(artifacts, str) or not isinstance(artifacts, (list, tuple)):
        raise ValueError('artifacts must be a list/tuple of relative path strings, got %s '
                         '(a single path string is a common mistake)' % _typename(artifacts))
    artifact_list = list(artifacts)
    for index, artifact in enumerate(artifact_list):
        if not isinstance(artifact, str) or not artifact.strip():
            raise ValueError('artifacts[%d] must be a non-empty path string, got %r'
                             % (index, artifact))
        problem = artifact_path_violation(artifact)
        if problem is not None:
            raise ValueError('artifacts[%d] %s' % (index, problem))
    if not isinstance(notes, str):
        raise ValueError('notes must be a string, got %s' % _typename(notes))
    manifest['blocks'].append({'app': app, 'artifacts': artifact_list, 'notes': notes})
    return manifest


def add_marker(manifest, t_s, name, source='conductor'):
    """Append a timeline marker to ``manifest`` and return the manifest (chainable).

    ``t_s`` is seconds on the conductor's shared (monotonic) clock; it must be a
    finite number. ``name`` is a short non-empty label. ``source`` is
    ``'conductor'`` (default) or ``'app'`` (marker injected by an app's stream).
    Markers keep insertion order — the conductor writes them as events happen.
    """
    if not isinstance(manifest, dict) or not isinstance(manifest.get('markers'), list):
        raise ValueError("manifest must be a dict with a 'markers' list; "
                         'use new_manifest() or load() first')
    if isinstance(t_s, bool) or not isinstance(t_s, (int, float)) or not math.isfinite(t_s):
        raise ValueError('t_s must be a finite number of seconds, got %r' % (t_s,))
    if not isinstance(name, str) or not name.strip():
        raise ValueError('name must be a non-empty string, got %r' % (name,))
    if source not in MARKER_SOURCES:
        raise ValueError('source must be one of %s, got %r'
                         % (', '.join(repr(candidate) for candidate in MARKER_SOURCES), source))
    manifest['markers'].append({'t_s': t_s, 'name': name, 'source': source})
    return manifest


def session_dir(root, manifest):
    """Return ``Path(root) / manifest['id']``, created on demand.

    The manifest is validated first, so a malformed or hostile id (path
    traversal) can never create or escape a directory. One session's files live
    together under this directory: ``manifest.json`` plus per-app artifacts.
    """
    validate(manifest)
    target = Path(root) / manifest['id']
    target.mkdir(parents=True, exist_ok=True)
    return target
