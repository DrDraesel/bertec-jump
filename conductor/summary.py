"""Doctor-facing Lab Session Summary — self-contained printable HTML (Phase 6A).

:func:`build_summary_html` renders a ``lab-session/1`` manifest as one light,
print-ready page: session header, one section per app block (artifacts,
best-effort extracted facts, notes), a session marker timeline, a static
Method & Limitations block, an explicit ``Requires Physician Review`` callout,
and a footer.

Honesty rules baked in (research/NEURO-BERTEC-INTEGRATION.md §5):
- blocks are presented side by side; there is no cross-app fusion, score, or
  causal claim — the physician connects the dots, not the software;
- each app keeps its own grading and native export as the authoritative record;
- reference bands are literature-sourced by the originating application;
- clinical decisions (including return-to-play) are physician-led.

Implementation rules: pure standard library; every interpolated value is
``html.escape``d (operator notes are untrusted text); no JavaScript; the output
is self-contained and deterministic for identical inputs — no clocks or
randomness inside (pass ``generated_at`` to stamp a different time).

Reader protocol: a *reader* is ``callable(artifact_paths) -> dict`` where
``artifact_paths`` is the block's list of (usually relative) path strings and
the returned dict maps short labels to scalar-ish values. Readers must never
raise on missing or malformed files — degrade to a note string instead. The
conductor guards reader exceptions anyway and falls back to
``{'note': 'artifact recorded'}``.

Path containment: every artifact entry is resolved against the session root and
must stay under it — absolute paths, drive letters, ``..`` components and
symlinks that leave the session are refused before any file is opened and are
surfaced as ``unresolved artifact``. A hostile manifest therefore cannot expose
files outside its own session directory.
"""
from pathlib import Path
import html
import json

from .manifest import validate, artifact_path_violation

APP_LABELS = {
    'bertec': 'Bertec Balance Plate',
    'neurosync': 'NeuroSync EEG',
    'runtrack': 'RunTrack Running Form',
}

# Safe scalar fields surfaced from a Bertec `report.json` (see service.py
# stop_recording): session id, recorded_at, duration, athlete.
_BERTEC_FIELD_ALIASES = (
    ('session_id', ('id', 'session_id')),
    ('recorded_at', ('recorded_at', 'started_at', 'created_at', 'completed_at')),
    ('duration', ('duration_s', 'duration')),
    ('athlete', ('athlete', 'athlete_id')),
)

# Basic JSON fields a generic reader lists when an artifact is readable JSON.
_GENERIC_FIELD_KEYS = ('id', 'athlete', 'recorded_at', 'started_at', 'duration_s', 'created_at')

_LIMITATIONS = (
    'Each block is an independent evaluation produced by its own application and '
    'grading rules. This summary associates the blocks side by side as context for a '
    'physician; it performs no cross-app fusion, scoring, or causal attribution.',
    'Reference bands and thresholds are literature-sourced by the originating '
    'application. Verify provenance there before quoting any value; this page does '
    'not restate it.',
    'Facts shown above are best-effort extractions from the recorded files and may be '
    'incomplete; each application\'s native export remains the authoritative record.',
    'This document is a research aid, not a medical device output. Clinical decisions '
    '- including concussion management and return-to-play clearance - remain '
    'physician-led (Amsterdam 2022 consensus / SCAT5 context).',
)

_CSS = """
    :root { color-scheme: light; }
    * { box-sizing: border-box; }
    body { font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif; color: #1c1c1c;
           background: #ffffff; margin: 0 auto; max-width: 900px; padding: 32px 28px 48px;
           font-size: 15px; line-height: 1.5; }
    h1 { font-size: 24px; margin: 0 0 6px; border-bottom: 3px solid #1c1c1c; padding-bottom: 8px; }
    h2 { font-size: 18px; margin: 28px 0 8px; }
    h3 { font-size: 16px; margin: 0 0 8px; }
    h4 { font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: #555;
         margin: 16px 0 6px; }
    .meta { color: #555; font-size: 13px; margin: 0 0 12px; }
    .notes { background: #f6f6f6; border: 1px solid #dddddd; border-radius: 6px;
             padding: 10px 14px; white-space: pre-wrap; }
    section.block { border: 1px solid #d8d8d8; border-radius: 8px; padding: 14px 18px;
                    margin: 14px 0; break-inside: avoid; }
    span.app { color: #666; font-weight: normal; font-size: 14px; }
    table.facts { border-collapse: collapse; width: 100%; font-size: 14px; }
    table.facts th, table.facts td { border: 1px solid #d8d8d8; padding: 5px 9px;
                                     text-align: left; vertical-align: top; }
    table.facts th { background: #f3f3f3; font-weight: 600; width: 34%; }
    table.timeline th { width: auto; }
    ul.artifacts { margin: 4px 0 0 18px; padding: 0; }
    ul.artifacts li { margin: 2px 0; }
    code { background: #f3f3f3; padding: 1px 4px; border-radius: 3px; font-size: 13px; }
    .block-notes { font-size: 13px; color: #333; white-space: pre-wrap; }
    section.limits { border-left: 5px solid #555555; background: #f7f7f7;
                     padding: 12px 18px; border-radius: 4px; }
    section.review { border: 2px solid #8a1f11; background: #fdf4f3; padding: 12px 18px;
                     border-radius: 6px; margin: 18px 0; }
    section.review h2 { color: #8a1f11; margin-top: 0; }
    footer { margin-top: 32px; border-top: 1px solid #cccccc; padding-top: 10px;
             color: #666666; font-size: 12px; }
    @media print { body { max-width: none; padding: 0; font-size: 12px; } }
"""


def _read_json_object(path):
    """Best-effort JSON-object read. Never raises; returns a dict or None."""
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _resolve(entry, session_root):
    """Resolve an artifact entry to a path inside ``session_root``, or None.

    A manifest is an exchange artifact, so a hostile one must never steer this
    reader at a file outside its session directory. Entries that are not strings,
    are absolute (POSIX or Windows), carry a drive letter, or contain a ``..``
    component are rejected up front (:func:`conductor.manifest.artifact_path_violation`);
    everything else is resolved and must still land under ``session_root`` (which
    also defeats symlinks). Rejected entries are reported by the callers as
    ``unresolved artifact``. When ``session_root`` is None the entry is resolved
    against the current working directory, as before.
    """
    if not isinstance(entry, str):
        return None
    if artifact_path_violation(entry) is not None:
        return None
    path = Path(entry)
    if session_root is None:
        return path
    root = Path(session_root)
    candidate = path if path.is_absolute() else root / path
    try:
        resolved = candidate.resolve()
        root_resolved = root.resolve()
    except OSError:
        return None
    if resolved == root_resolved or root_resolved in resolved.parents:
        return resolved
    return None


def _bertec_reader(artifact_paths, session_root=None):
    """Surface safe scalar fields from the first readable JSON artifact.

    Reads a Bertec ``report.json``-style JSON object and returns
    ``session_id`` / ``recorded_at`` / ``duration`` / ``athlete`` (plus the
    source artifact name). Falls back to ``{'note': 'artifact recorded'}`` for
    missing, unreadable, or unparsable artifacts, and to
    ``{'note': 'unresolved artifact'}`` when an entry points outside the session
    directory — never raises, and never reads outside ``session_root``.
    """
    source = None
    fields = {}
    rejected = 0
    for entry in artifact_paths:
        candidate = _resolve(entry, session_root)
        if candidate is None:
            rejected += 1
            continue
        if candidate.suffix.lower() != '.json':
            continue
        data = _read_json_object(candidate)
        if data is None:
            continue
        source = str(entry)
        for label, aliases in _BERTEC_FIELD_ALIASES:
            for key in aliases:
                value = data.get(key)
                if isinstance(value, (str, int, float, bool)) and value is not None:
                    fields[label] = value
                    break
        break
    if fields:
        result = {'artifact': source}
        result.update(fields)
        return result
    if rejected:
        return {'note': 'unresolved artifact'}
    return {'note': 'artifact recorded'}


def _generic_reader(artifact_paths, session_root=None):
    """Existence + size for each listed artifact; basic JSON fields if readable.

    Never raises: a missing artifact is reported as ``missing``, an entry that
    points outside the session directory as ``unresolved artifact``, and an empty
    artifact list degrades to ``{'note': 'recorded'}``.
    """
    facts = {}
    for entry in artifact_paths:
        key = str(entry)
        candidate = _resolve(entry, session_root)
        if candidate is None:
            facts[key] = 'unresolved artifact'
            continue
        try:
            size = candidate.stat().st_size
        except OSError:
            facts[key] = 'missing'
            continue
        facts[key] = '%d bytes' % size
        if candidate.suffix.lower() == '.json':
            data = _read_json_object(candidate)
            if data is not None:
                for label in _GENERIC_FIELD_KEYS:
                    value = data.get(label)
                    if isinstance(value, (str, int, float, bool)) and value is not None:
                        facts['%s: %s' % (key, label)] = value
    if not facts:
        facts['note'] = 'recorded'
    return facts


def default_readers(session_root=None):
    """Return the built-in readers dict ``app -> callable(paths) -> dict``.

    ``session_root`` (usually the session directory) resolves relative artifact
    paths; when None, relative paths are tried against the current working
    directory. Every reader degrades gracefully — none raises on missing or
    malformed files.
    """
    def bertec(paths):
        return _bertec_reader(paths, session_root)

    def generic(paths):
        return _generic_reader(paths, session_root)

    return {'bertec': bertec, 'neurosync': generic, 'runtrack': generic}


def _fmt_value(value):
    """Render a fact/marker value as a short deterministic string (pre-escape)."""
    if value is None:
        return 'not recorded'
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    if isinstance(value, float):
        return format(value, '.6g')
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    if len(text) > 200:
        text = text[:197] + '...'
    return text


def _read_facts(readers, block):
    """Extract a facts dict for one block; never raises."""
    reader = readers.get(block['app']) if isinstance(readers, dict) else None
    if not callable(reader):
        return {'note': 'no reader available; artifact recorded'}
    try:
        facts = reader(list(block['artifacts']))
    except Exception:
        return {'note': 'reader failed; artifact recorded'}
    if isinstance(facts, dict) and facts:
        return facts
    return {'note': 'artifact recorded'}


def _facts_table_html(facts):
    esc = html.escape
    rows = []
    for label, value in facts.items():
        rows.append('<tr><th scope="row">%s</th><td>%s</td></tr>'
                    % (esc(str(label)), esc(_fmt_value(value))))
    if not rows:
        return '<p>No facts extracted.</p>'
    return '<table class="facts">%s</table>' % ''.join(rows)


def build_summary_html(manifest, readers=None, generated_at=None, session_root=None):
    """Render a doctor-facing, self-contained printable HTML summary.

    ``manifest`` must be a valid ``lab-session/1`` manifest (it is validated
    first; ValueError otherwise). ``readers`` maps app -> reader callable; when
    omitted, :func:`default_readers` is used, with ``session_root`` resolving
    relative artifact paths. ``generated_at`` defaults to the manifest's
    ``created_at`` so the output is deterministic for identical inputs.

    Every interpolated value is html-escaped; the page contains no JavaScript.
    """
    validate(manifest)
    esc = html.escape
    if generated_at is None:
        generated_at = manifest['created_at']
    if readers is None:
        readers = default_readers(session_root)

    page = []
    page.append('<!DOCTYPE html>')
    page.append('<html lang="en">')
    page.append('<head><meta charset="utf-8">')
    page.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    page.append('<title>Lab Session Summary \u2014 %s</title>' % esc(manifest['athlete_id']))
    page.append('<style>%s</style>' % _CSS)
    page.append('</head>')
    page.append('<body>')

    page.append('<header>')
    page.append('<h1>Lab Session Summary \u2014 %s</h1>' % esc(manifest['athlete_id']))
    page.append('<p class="meta">Session id %s \u00b7 created %s \u00b7 schema %s (version %s)</p>'
                % (esc(manifest['id']), esc(manifest['created_at']),
                   esc(manifest['schema']), esc(str(manifest['version']))))
    page.append('<h2>Operator notes</h2>')
    if manifest['operator_notes'].strip():
        page.append('<p class="notes">%s</p>' % esc(manifest['operator_notes']))
    else:
        page.append('<p class="notes"><em>None supplied.</em></p>')
    page.append('</header>')

    page.append('<h2>Blocks</h2>')
    review_labels = []
    if not manifest['blocks']:
        page.append('<p>No blocks recorded in this session.</p>')
    for order, block in enumerate(manifest['blocks'], start=1):
        app = block['app']
        label = APP_LABELS.get(app, app)
        review_labels.append(label)
        page.append('<section class="block">')
        page.append('<h3>%d. %s <span class="app">(%s)</span></h3>'
                    % (order, esc(label), esc(app)))
        page.append('<h4>Artifacts</h4>')
        if block['artifacts']:
            page.append('<ul class="artifacts">%s</ul>'
                        % ''.join('<li><code>%s</code></li>' % esc(artifact)
                                  for artifact in block['artifacts']))
        else:
            page.append('<p>No artifacts listed.</p>')
        page.append('<h4>Extracted facts</h4>')
        page.append(_facts_table_html(_read_facts(readers, block)))
        page.append('<h4>Block notes</h4>')
        if block['notes'].strip():
            page.append('<p class="block-notes">%s</p>' % esc(block['notes']))
        else:
            page.append('<p class="block-notes">None supplied.</p>')
        page.append('</section>')

    page.append('<h2>Session timeline (markers)</h2>')
    if manifest['markers']:
        rows = ''.join('<tr><td>%s</td><td>%s</td><td>%s</td></tr>'
                       % (esc(_fmt_value(marker['t_s'])), esc(marker['name']), esc(marker['source']))
                       for marker in manifest['markers'])
        page.append('<table class="facts timeline"><thead><tr><th>t (s)</th><th>Marker</th>'
                    '<th>Source</th></tr></thead><tbody>%s</tbody></table>' % rows)
    else:
        page.append('<p>No markers recorded.</p>')

    page.append('<section class="limits">')
    page.append('<h2>Method &amp; Limitations</h2>')
    page.append('<ul>%s</ul>' % ''.join('<li>%s</li>' % esc(item) for item in _LIMITATIONS))
    page.append('</section>')

    page.append('<section class="review">')
    page.append('<h2>Requires Physician Review</h2>')
    if review_labels:
        page.append('<p>The following blocks are associated in this session and each '
                    '<strong>requires physician review</strong> before interpretation:</p>')
        page.append('<ul>%s</ul>' % ''.join('<li>%s</li>' % esc(label) for label in review_labels))
    else:
        page.append('<p>No blocks were recorded in this session, so there is nothing to review here.</p>')
    page.append('<p>Nothing in this summary clears an athlete for participation.</p>')
    page.append('</section>')

    page.append('<footer>')
    page.append('<p>Generated by the Hermes multi-modal conductor (research aid). '
                'Manifest schema %s, version %s. Generated at %s.</p>'
                % (esc(manifest['schema']), esc(str(manifest['version'])), esc(str(generated_at))))
    page.append('</footer>')

    page.append('</body>')
    page.append('</html>')
    return '\n'.join(page) + '\n'
