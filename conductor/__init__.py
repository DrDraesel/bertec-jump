"""Local multi-modal Session Conductor - roadmap Phase 6A (skeleton).

Associates three independent, separately-graded evaluations - Bertec balance
plate, NeuroSync EEG, RunTrack running form - into one doctor-facing session
manifest and summary. The association is side-by-side context for a physician;
it is never diagnostic fusion, and each app keeps its own grading and native
exports (see research/NEURO-BERTEC-INTEGRATION.md §5).

Modules:
    manifest - ``lab-session/1`` schema, validation, atomic save/load, session dirs
    summary  - self-contained printable HTML summary for physician review

Pure standard library. Default session root: ``manifest.SESSIONS_ROOT``
(``<repo>/sessions/``). Research aid - requires physician review for any
clinical interpretation.
"""
from .manifest import (
    APPS,
    ID_PATTERN,
    MANIFEST_FILENAME,
    MARKER_SOURCES,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    SESSIONS_ROOT,
    add_block,
    add_marker,
    is_valid_id,
    load,
    new_id,
    new_manifest,
    save,
    session_dir,
    validate,
)
from .summary import APP_LABELS, build_summary_html, default_readers

__version__ = '6.0.0a1'

__all__ = [
    'SCHEMA_NAME',
    'SCHEMA_VERSION',
    'APPS',
    'MARKER_SOURCES',
    'ID_PATTERN',
    'MANIFEST_FILENAME',
    'SESSIONS_ROOT',
    'APP_LABELS',
    'new_id',
    'is_valid_id',
    'new_manifest',
    'validate',
    'save',
    'load',
    'add_block',
    'add_marker',
    'session_dir',
    'build_summary_html',
    'default_readers',
]
