"""Loopback-only web app. No telemetry, cloud uploads, fake data or browser USB guessing."""
import asyncio
from contextlib import asynccontextmanager
import html
import json
import math
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit
import urllib.request

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from service import (Acquisition, ProtocolConflict, VISION_DISABLED_MESSAGE,
                     athlete_reference_error, protocol_catalog,
                     render_protocol_report_page, training_catalog,
                     vision_consent)

# Optional v2 vision modules: the guide is pure data and the printable section
# renderer is shared with the plate report. Either may be missing on an older
# checkout - the routes then say so honestly instead of inventing content.
try:
    from vision.guide import guide as vision_guide
except ImportError:                     # pragma: no cover - install dependent
    vision_guide = None
try:
    from vision.vision_report import render_vision_section as render_vision_html
except ImportError:                     # pragma: no cover - install dependent
    render_vision_html = None
try:  # full vision-only printable report (v2); the fallback page is built below
    from vision.vision_only_report import render_vision_only_report as render_vision_only_html
except Exception:                       # pragma: no cover - optional module
    render_vision_only_html = None

ROOT = Path(__file__).resolve().parent

# The printable report page is fully self-contained (inline stylesheet, no
# scripts, no network); its response pins this stricter policy instead of the
# dashboard default set by the middleware below.
REPORT_PAGE_CSP = ("default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
                   "form-action 'none'; frame-ancestors 'none'")


class RecordStart(BaseModel):
    athlete: str = Field(default='', max_length=120)


class ZeroRequest(BaseModel):
    confirm_unloaded: StrictBool = False


class ProtocolStart(BaseModel):
    protocol_id: str = ''
    athlete: str = ''
    # Optional report reference layer. Typed as Any so every bad value reaches
    # athlete_reference_error and keeps the frozen {'ok': false, 'error': ...}
    # JSON contract (400) instead of a pydantic 422 body.
    age: Optional[Any] = None
    sex: Optional[Any] = None


class VisionStart(BaseModel):
    source: Optional[Any] = None
    proc_width: Optional[Any] = None
    fps_limit: Optional[Any] = None
    face: Optional[Any] = None
    audio: Optional[Any] = None
    stt: Optional[Any] = None


class VisionSessionStart(BaseModel):
    # Every field is typed Any so a bad value reaches our own validation and
    # keeps the frozen {'ok': false, 'error': ...} JSON contract (400) instead
    # of a pydantic 422 body.
    label: Optional[Any] = None
    age: Optional[Any] = None
    sex: Optional[Any] = None
    view: Optional[Any] = None
    notes: Optional[Any] = None
    # H4: per-session consent {audio, face, stt}; every lane defaults OFF.
    consent: Optional[Any] = None


class VisionSegmentStart(BaseModel):
    test_id: Optional[Any] = None
    limb: Optional[Any] = None
    trial: Optional[Any] = None


class VisionAudioToggle(BaseModel):
    enabled: Optional[Any] = None


class VisionEventMark(BaseModel):
    type: Optional[Any] = None
    note: Optional[Any] = None


class ProtocolNote(BaseModel):
    text: str = ''


class TrainingStart(BaseModel):
    mode_id: str = ''
    options: dict = Field(default_factory=dict)
    athlete: str = Field(default='', max_length=120)


class TrainingAdvance(BaseModel):
    action: str = 'advance'
    target: Optional[str] = None


VISION_SOURCE_KINDS = ('usb', 'url', 'file')
VISION_PROC_WIDTH_RANGE = (64, 3840)
VISION_FPS_RANGE = (1, 240)


def _vision_source_error(source):
    """Validation message for a POST /api/vision/start source, else None."""
    if not isinstance(source, dict):
        return 'source must be an object with a kind (usb / url / file)'
    kind = source.get('kind')
    if kind not in VISION_SOURCE_KINDS:
        return "source.kind must be 'usb', 'url' or 'file'"
    if kind == 'usb':
        index = source.get('index')
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            return 'source.index must be a non-negative integer for a usb source'
    elif kind == 'url':
        url = source.get('url')
        if not isinstance(url, str) or not url.strip():
            return 'source.url must be a non-empty string for a url source'
        parts = urlsplit(url.strip())
        if not parts.scheme:
            return 'source.url must be a stream URL with a scheme (http/https/rtsp/...)'
    else:
        path = source.get('path')
        if not isinstance(path, str) or not path.strip():
            return 'source.path must be a non-empty string for a file source'
    return None


def _vision_source(source):
    """Whitespace-normalized source payload after _vision_source_error passes."""
    kind = source['kind']
    if kind == 'usb':
        return {'kind': 'usb', 'index': int(source['index'])}
    if kind == 'url':
        return {'kind': 'url', 'url': source['url'].strip()}
    return {'kind': 'file', 'path': source['path'].strip()}


def _vision_option_error(value, name, minimum, maximum):
    """Validation message for an optional integer vision option, else None."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return '%s must be an integer' % name
    if not minimum <= value <= maximum:
        return '%s must be between %d and %d' % (name, minimum, maximum)
    return None


# -- vision-only sessions / guide (v2, additive) -----------------------------
# Same JSON error contract as the rest of the app: 400 bad request, 404 unknown
# session, 409 session conflict, 503 sidecar/store unavailable.  Nothing here
# touches the plate routes.

VISION_SESSION_LABEL_MAX = 120
VISION_SESSION_NOTES_MAX = 2000
VISION_MARK_TYPES = ('pain', 'did_not_understand', 'note')
VISION_ONLY_STORE_MESSAGE = ('Vision-only session storage is not installed; '
                             'the plate service is unaffected.')
VISION_ONLY_NOT_RUNNING = ('Vision sidecar is not running; start the camera '
                           'before starting a vision-only session.')


def vision_views(value):
    """Query-string ``views`` -> tuple for vision.guide.guide().

    ``'side'``, ``'side,front'``, ``'side+front'`` and ``'front'`` are accepted;
    an empty value means the default single side view.  Validation of the view
    names themselves happens in the guide module (single source of truth).
    """
    if value is None:
        return ('side',)
    text = str(value).strip().lower()
    if not text:
        return ('side',)
    for separator in ('+', ';', '|', ' '):
        text = text.replace(separator, ',')
    return tuple(part.strip() for part in text.split(',') if part.strip())


def _vision_view_value(view):
    """Normalised camera-view label for a session, or None (raises ValueError)."""
    if view is None:
        return None
    if not isinstance(view, str):
        raise ValueError("view must be 'side', 'front' or 'side+front'")
    text = view.strip().lower().replace(' ', '+')
    if not text:
        return None
    if text in ('side', 'front'):
        return text
    if text in ('side+front', 'side,front', 'side/front', 'front+side', 'both'):
        return 'side+front'
    raise ValueError("view must be 'side', 'front' or 'side+front'")


def _vision_text(value, name, limit):
    """Bounded string session field (raises ValueError for a non-string)."""
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ValueError('%s must be a string' % name)
    return value.strip()[:limit]


def vision_disabled_response():
    """503 JSON error when ``vision.enabled`` is false in config.json.

    M8: the master switch was documented but never read.  It is checked on
    every start route (before any sidecar/store work) so a deployment that
    disabled the lane cannot open a camera or a microphone by accident.
    """
    return JSONResponse({'ok': False, 'error': VISION_DISABLED_MESSAGE},
                        status_code=503)


def vision_disabled(acquisition):
    """Whether ``acquisition``'s config disables the camera lane (never raises)."""
    try:
        return not acquisition.vision_enabled()
    except Exception:
        return False


def _esc(value):
    """HTML-escaped text for the printable vision report."""
    return html.escape('' if value is None else str(value), quote=True)


def _fmt(value, digits=1):
    """Fixed-decimal number for the report, '-' when absent/non-finite."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _esc(value)
    if not math.isfinite(number):
        return '-'
    return '%.*f' % (digits, number)


def vision_report_session(session):
    """Map a stored vision-only session to render_vision_section's input shape.

    The shared plate-report renderer expects ``{'trials': [{'condition',
    'vision_summary'}]}``; a vision-only clip is one trial whose summary is the
    stored session summary.  Nothing is invented - an absent summary yields no
    trials and the renderer returns an empty section.
    """
    summary = session.get('summary') if isinstance(session, dict) else None
    trials = []
    if isinstance(summary, dict) and summary:
        trials.append({'condition': str(session.get('view') or 'vision-only'),
                       'vision_summary': summary})
    return {'id': (session or {}).get('id'), 'trials': trials}


_VISION_REPORT_CSS = """
:root { color-scheme: light; }
body { margin: 0; padding: 32px 24px 56px; background: #ffffff; color: #17202a;
       font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       font-size: 14px; line-height: 1.5; }
main { max-width: 860px; margin: 0 auto; }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 28px 0 8px; border-bottom: 1px solid #d5dbdb; padding-bottom: 4px; }
p { margin: 8px 0; }
.sub { color: #566573; margin: 0 0 18px; }
.banner { border: 2px solid #9a6700; background: #fff8e6; padding: 12px 14px; margin: 18px 0; }
.banner strong { display: block; margin-bottom: 4px; }
.note { background: #f4f6f7; border-left: 4px solid #7f8c8d; padding: 10px 12px; margin: 14px 0; }
.unavailable { border-left-color: #9a6700; }
table { width: 100%; border-collapse: collapse; margin: 10px 0 18px; }
th, td { border: 1px solid #d5dbdb; padding: 6px 8px; text-align: left; vertical-align: top; }
th { background: #f4f6f7; font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
ul { margin: 8px 0 8px 20px; padding: 0; }
li { margin: 4px 0; }
footer { margin-top: 32px; border-top: 1px solid #d5dbdb; padding-top: 10px; color: #566573; font-size: 12px; }
code { font-family: Consolas, "Courier New", monospace; font-size: 12px; }
@media print { body { padding: 0; } .banner { break-inside: avoid; } h2 { break-after: avoid; } }
"""


def render_vision_only_report_page(session, section_html=''):
    """Printable HTML page for one stored vision-only session.

    Pure presentation over stored data: every number comes from
    ``session['summary']``, every operator mark from ``session['events_log']``.
    When the shared vision section renderer is unavailable (or produced
    nothing) the page says so in words instead of inventing content.  Inline
    CSS only - no scripts, no network - so REPORT_PAGE_CSP applies.
    """
    session = session if isinstance(session, dict) else {}
    summary = session.get('summary') if isinstance(session.get('summary'), dict) else {}
    label = str(session.get('label') or 'vision-only session')
    parts = [
        '<!DOCTYPE html>',
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<title>Vision-only session - %s</title>' % _esc(label),
        '<style>%s</style></head><body><main>' % _VISION_REPORT_CSS,
        '<h1>Vision-only session</h1>',
        '<p class="sub">%s &middot; <code>%s</code></p>'
        % (_esc(label), _esc(session.get('id'))),
        '<div class="banner"><strong>Camera-only session - no force-plate data.</strong>',
        'The force plate was not part of this recording: there is no centre-of-'
        'pressure path, no force value and no plate reference in this report. '
        'Pose values are markerless ESTIMATES from one camera, not measurements. '
        'This is not a diagnosis, not a balance score and not a fall-risk score; '
        'interpretation stays physician-led.</div>',
    ]

    meta = [
        ('Started', session.get('started_at')),
        ('Ended', session.get('ended_at')),
        ('Camera view', session.get('view')),
        ('Age', session.get('age')),
        ('Sex', session.get('sex')),
        ('Frames stored', session.get('frames')),
        ('Operator marks', summary.get('operator_marks')),
        ('Pose coverage (%)', summary.get('pose_coverage_pct')),
        ('Mean processing fps', summary.get('fps_mean')),
        ('Pose summary available', 'yes' if summary.get('available') else 'no'),
    ]
    rows = ''.join('<tr><th>%s</th><td>%s</td></tr>'
                   % (_esc(name), _fmt(value) if isinstance(value, (int, float))
                      and not isinstance(value, bool) else _esc(value))
                   for name, value in meta if value is not None)
    parts.append('<h2>Session</h2><table>%s</table>' % rows)

    if session.get('notes'):
        parts.append('<p class="note"><strong>Session notes:</strong> %s</p>'
                     % _esc(session.get('notes')))

    if not summary.get('available'):
        parts.append('<p class="note">No usable pose summary was stored for this '
                     'session (fewer than 10 frames, or no pose was detected). '
                     'Nothing is inferred from an empty clip.</p>')

    angles = summary.get('angles')
    if isinstance(angles, dict) and angles:
        angle_rows = []
        for name in sorted(angles):
            stats = angles.get(name) if isinstance(angles.get(name), dict) else {}
            angle_rows.append(
                '<tr><td>%s</td><td class="num">%s</td><td class="num">%s</td>'
                '<td class="num">%s</td><td class="num">%s</td></tr>'
                % (_esc(name.replace('_', ' ')), _fmt(stats.get('mean')),
                   _fmt(stats.get('sd')), _fmt(stats.get('min')),
                   _fmt(stats.get('max'))))
        parts.append('<h2>Pose estimates (degrees)</h2>'
                     '<table><thead><tr><th>Metric</th><th class="num">Mean</th>'
                     '<th class="num">SD</th><th class="num">Min</th>'
                     '<th class="num">Max</th></tr></thead><tbody>%s</tbody></table>'
                     % ''.join(angle_rows))

    counts = summary.get('event_counts')
    if isinstance(counts, dict) and counts:
        count_rows = ''.join('<tr><td>%s</td><td class="num">%s</td></tr>'
                             % (_esc(key), _esc(value))
                             for key, value in sorted(counts.items()))
        parts.append('<h2>Recorded events</h2><table><thead><tr><th>Event</th>'
                     '<th class="num">Count</th></tr></thead><tbody>%s</tbody>'
                     '</table>' % count_rows)

    marks = [event for event in (session.get('events_log') or [])
             if isinstance(event, dict) and event.get('source') == 'operator']
    parts.append('<h2>Operator marks</h2>')
    if marks:
        mark_rows = ''.join(
            '<tr><td>%s</td><td>%s</td><td>%s</td></tr>'
            % (_esc(event.get('t')), _esc(event.get('type')),
               _esc(event.get('note')))
            for event in marks)
        parts.append('<table><thead><tr><th>Time (UTC)</th><th>Mark</th>'
                     '<th>Note</th></tr></thead><tbody>%s</tbody></table>'
                     % mark_rows)
    else:
        parts.append('<p class="note">No operator marks were recorded for this '
                     'session.</p>')

    if section_html:
        parts.append(section_html)
    else:
        parts.append('<h2>Camera-derived kinematics (pose estimate)</h2>')
        if summary.get('available'):
            parts.append('<p class="note unavailable">Vision report module '
                         'unavailable - the shared printable camera section could '
                         'not be rendered in this build. The stored summary above '
                         'is shown unmodified; nothing has been estimated or '
                         'filled in.</p>')
        else:
            parts.append('<p class="note unavailable">No printable camera section '
                         'exists because no usable frames were stored.</p>')

    parts.append(
        '<h2>Limitations</h2><ul>'
        '<li>Single-camera markerless pose: segment angles are estimates and '
        'trunk/sway values are relative to the person\'s on-screen position, '
        'not to the plate.</li>'
        '<li>No centre-of-pressure, no force, no 3D joint centre and no '
        'frontal-plane valgus angle can be derived from this recording.</li>'
        '<li>Audio/face values, when present, are observational proxies - not '
        'pain detection and not emotion detection. Transcripts are local '
        'machine output that may be wrong.</li>'
        '<li>No diagnosis, no fall-risk score, no return-to-sport clearance. '
        'Research software, not a validated clinical device.</li>'
        '</ul>')
    parts.append('<footer>Bertec Jump Lab - vision-only session %s. '
                 'Generated from the stored session data only.</footer>'
                 % _esc(session.get('id')))
    parts.append('</main></body></html>')
    return '\n'.join(parts)


def create_app(data_dir=None, auto_connect=True):
    config_path = ROOT/'config.json'
    config = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
    acquisition = Acquisition(data_dir or ROOT/'data', config=config)

    @asynccontextmanager
    async def lifespan(app):
        acquisition.launch(auto_connect=auto_connect)
        yield
        await asyncio.to_thread(acquisition.shutdown)

    app = FastAPI(title='Bertec Jump Lab', lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.acquisition = acquisition
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', '[::1]'])

    @app.middleware('http')
    async def local_origin(request: Request, call_next):
        origin = request.headers.get('origin')
        if origin and urlsplit(origin).netloc != request.headers.get('host'):
            return JSONResponse({'detail':'Cross-origin access is not allowed'}, status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        # A route may pin its own stricter policy (the self-contained printable
        # report page allows its inline stylesheet and loads nothing else); every
        # other response keeps the dashboard default below.
        if 'content-security-policy' not in response.headers:
            response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        return response

    async def command(name, *args):
        try:
            return await asyncio.to_thread(acquisition.call, name, *args)
        except ProtocolConflict as exc:
            # Explicit 409 for a protocol-session conflict: zero()/connect()/
            # disconnect() refuse while a trial is running (the error text tells
            # the operator to end or discard the trial first).
            raise HTTPException(409, str(exc)) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(504, 'SDK command still running; inspect status before retrying') from exc

    async def protocol_command(name, *args):
        """Run a protocol command; returns (payload, None) or (None, error JSON).

        Errors follow the frozen protocol contract: {"ok": false, "error": ...}
        with 409 for a conflicting session (already active), 400 for a bad
        request / bad state and 504 when the SDK worker does not answer.
        """
        try:
            return await asyncio.to_thread(acquisition.call, name, *args), None
        except ProtocolConflict as exc:
            return None, JSONResponse({'ok': False, 'error': str(exc)}, status_code=409)
        except TimeoutError:
            return None, JSONResponse({'ok': False, 'error': 'SDK command still running; '
                                      'inspect protocol state before retrying'}, status_code=504)
        except (RuntimeError, ValueError) as exc:
            return None, JSONResponse({'ok': False, 'error': str(exc)}, status_code=400)

    async def training_command(name, *args):
        """Run a training command; returns (payload, None) or (None, error JSON).

        Same frozen contract as protocol_command ({"ok": false, "error": ...}):
        409 for a session conflict (a protocol session or training session is
        already active), 400 for a bad request / bad state - including the
        TypeError a wrong-typed training option raises, which is a caller error
        like any other - and 504 when the SDK worker does not answer.
        """
        try:
            return await asyncio.to_thread(acquisition.call, name, *args), None
        except ProtocolConflict as exc:
            return None, JSONResponse({'ok': False, 'error': str(exc)}, status_code=409)
        except TimeoutError:
            return None, JSONResponse({'ok': False, 'error': 'SDK command still running; '
                                      'inspect training state before retrying'}, status_code=504)
        except (RuntimeError, ValueError, TypeError) as exc:
            return None, JSONResponse({'ok': False, 'error': str(exc)}, status_code=400)

    async def vision_command(name, *args):
        """Run a vision sidecar command; (payload, None) or (None, error JSON).

        Frozen protocol contract: 409 when a protocol trial is running (the
        sidecar is never restarted underneath a capturing trial), 400 for a bad
        request or a sidecar refusal, 504 when the worker does not answer and
        502 for an unexpected sidecar-side failure.
        """
        try:
            return await asyncio.to_thread(acquisition.call, name, *args), None
        except ProtocolConflict as exc:
            return None, JSONResponse({'ok': False, 'error': str(exc)}, status_code=409)
        except TimeoutError:
            return None, JSONResponse({'ok': False, 'error': 'Vision command still running; '
                                      'inspect vision status before retrying'}, status_code=504)
        except (RuntimeError, ValueError) as exc:
            return None, JSONResponse({'ok': False, 'error': str(exc)}, status_code=400)
        except Exception as exc:
            return None, JSONResponse({'ok': False,
                                       'error': 'vision sidecar command failed: %s' % exc},
                                      status_code=502)

    def vision_state_payload():
        """Shared /api/vision/state + /api/state body (status enriched by latest)."""
        status = acquisition.vision_status()
        latest = acquisition.vision_latest()
        if isinstance(latest, dict):
            if status.get('fps') is None:
                status['fps'] = latest.get('fps')
            if status.get('pose_found') is None:
                status['pose_found'] = latest.get('pose_found')
        return {'ok': True, 'vision': status, 'latest': latest}

    @app.get('/api/status')
    def status():
        return acquisition.snapshot()

    @app.post('/api/connect')
    async def connect():
        return await command('connect')

    @app.post('/api/disconnect')
    async def disconnect():
        return await command('disconnect')

    @app.post('/api/zero')
    async def zero(body: ZeroRequest):
        return await command('zero', body.confirm_unloaded)

    @app.post('/api/record/start')
    async def start_recording(body: RecordStart):
        return await command('start_recording', body.athlete)

    @app.post('/api/record/stop')
    async def stop_recording():
        report = await command('stop_recording')
        return {**acquisition.snapshot(), 'session':report}

    @app.get('/api/sessions')
    def sessions():
        return acquisition.sessions()

    @app.get('/api/sessions/{record_id}/{filename}')
    def session_file(record_id: str, filename: str):
        try:
            path = acquisition.session_file(record_id, filename)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from exc
        return FileResponse(path, filename=f'{record_id}-{filename}',
                            media_type='text/csv' if filename.endswith('.csv') else 'application/json')

    # -- guided protocol sessions (research aid; physician-led interpretation) --

    @app.get('/api/protocol/catalog')
    def protocol_catalog_route():
        return {'ok': True, 'protocols': protocol_catalog()}

    @app.get('/api/protocol/state')
    def protocol_state_route():
        return {'ok': True, **acquisition.protocol_state()}

    @app.post('/api/protocol/start')
    async def protocol_start_route(body: ProtocolStart):
        sex = body.sex.strip().lower() if isinstance(body.sex, str) else body.sex
        error = athlete_reference_error(body.age, sex)
        if error is not None:
            return JSONResponse({'ok': False, 'error': error}, status_code=400)
        state, error = await protocol_command('protocol_start', body.protocol_id,
                                              body.athlete, body.age, sex)
        return error if error is not None else {'ok': True, 'state': state}

    @app.post('/api/protocol/begin_trial')
    async def protocol_begin_trial_route():
        state, error = await protocol_command('protocol_begin_trial')
        return error if error is not None else {'ok': True, 'state': state}

    @app.post('/api/protocol/end_trial')
    async def protocol_end_trial_route():
        result, error = await protocol_command('protocol_end_trial')
        return error if error is not None else {'ok': True, 'trial': result['trial'],
                                                'state': result['state']}

    @app.post('/api/protocol/keep_trial')
    async def protocol_keep_trial_route():
        state, error = await protocol_command('protocol_keep_trial')
        return error if error is not None else {'ok': True, 'state': state}

    @app.post('/api/protocol/discard_trial')
    async def protocol_discard_trial_route():
        state, error = await protocol_command('protocol_discard_trial')
        return error if error is not None else {'ok': True, 'state': state}

    @app.post('/api/protocol/repeat_trial')
    async def protocol_repeat_trial_route():
        state, error = await protocol_command('protocol_repeat_trial')
        return error if error is not None else {'ok': True, 'state': state}

    @app.post('/api/protocol/skip_condition')
    async def protocol_skip_condition_route():
        state, error = await protocol_command('protocol_skip_condition')
        return error if error is not None else {'ok': True, 'state': state}

    @app.post('/api/protocol/note')
    async def protocol_note_route(body: ProtocolNote):
        _, error = await protocol_command('protocol_note', body.text)
        return error if error is not None else {'ok': True}

    @app.post('/api/protocol/close')
    async def protocol_close_route():
        result, error = await protocol_command('protocol_close')
        return error if error is not None else {'ok': True, 'session_id': result['session_id'],
                                                'summary': result['summary']}

    # -- stored reports (Phase 3): JSON for the dashboard, printable HTML ----
    # Additive, read-only routes over data/protocols/<session_id>/; no new
    # auth surface and no cloud path (loopback-only like the rest).

    @app.get('/api/protocol/sessions')
    def protocol_sessions_route():
        return {'ok': True, 'sessions': acquisition.protocol_sessions()}

    @app.get('/api/protocol/report/{session_id}')
    def protocol_report_route(session_id: str):
        try:
            report = acquisition.protocol_report(session_id)
        except (ValueError, FileNotFoundError) as exc:
            return JSONResponse({'ok': False, 'error': str(exc)}, status_code=404)
        return {'ok': True, 'report': report}

    @app.get('/api/protocol/progress')
    def protocol_progress_route(athlete: str = ''):
        name = str(athlete).strip()
        if not name:
            return JSONResponse({'ok': False, 'error': 'Query parameter athlete is required'},
                                status_code=400)
        return {'ok': True, 'series': acquisition.protocol_progress(name)}

    @app.get('/report/{session_id}')
    def protocol_report_page_route(session_id: str):
        """Self-contained printable report page: inline CSS, no scripts."""
        try:
            report = acquisition.protocol_report(session_id)
        except (ValueError, FileNotFoundError):
            report = None
        return HTMLResponse(render_protocol_report_page(report, session_id),
                            status_code=200 if report is not None else 404,
                            headers={'Content-Security-Policy': REPORT_PAGE_CSP})

    # -- training modes (Phase 4, research engagement aid; not therapy) -----
    # Additive routes over the training.py engine: the same frozen contract
    # shape as /api/protocol/* ({"ok": true, ...} / {"ok": false, "error": ...}
    # with 409 conflicts and 400 bad requests) and the same mutual exclusion -
    # a training session and a protocol session can never be open together.

    @app.get('/api/training/catalog')
    def training_catalog_route():
        return {'ok': True, 'modes': training_catalog()}

    @app.get('/api/training/state')
    def training_state_route():
        return {'ok': True, **acquisition.training_state()}

    @app.post('/api/training/start')
    async def training_start_route(body: TrainingStart):
        state, error = await training_command('training_start', body.mode_id,
                                              body.options, body.athlete)
        return error if error is not None else {'ok': True, 'state': state}

    @app.post('/api/training/stop')
    async def training_stop_route():
        result, error = await training_command('training_stop')
        return error if error is not None else {'ok': True, 'session_id': result['session_id'],
                                                'summary': result['summary']}

    @app.post('/api/training/advance')
    async def training_advance_route(body: TrainingAdvance):
        state, error = await training_command('training_advance', body.action, body.target)
        return error if error is not None else {'ok': True, 'state': state}

    # -- optional vision sidecar ------------------------------------------
    # Additive local-only routes over vision/manager.py. The sidecar runs on
    # its own worker process and streams MJPEG over loopback; /feed proxies
    # that stream so the browser only ever talks to this origin (no extra
    # network surface, no CORS opening). Status is an O(1) read.

    @app.get('/api/vision/status')
    def vision_status_route():
        return {'ok': True, 'vision': acquisition.vision_status()}

    @app.post('/api/vision/start')
    async def vision_start_route(body: VisionStart):
        if vision_disabled(acquisition):
            return vision_disabled_response()
        source_error = _vision_source_error(body.source)
        if source_error is not None:
            return JSONResponse({'ok': False, 'error': source_error}, status_code=400)
        option_error = (_vision_option_error(body.proc_width, 'proc_width',
                                             *VISION_PROC_WIDTH_RANGE)
                        or _vision_option_error(body.fps_limit, 'fps_limit',
                                                *VISION_FPS_RANGE))
        if option_error is not None:
            return JSONResponse({'ok': False, 'error': option_error}, status_code=400)
        for name in ('face', 'audio', 'stt'):
            flag_value = getattr(body, name, None)
            if flag_value is not None and not isinstance(flag_value, bool):
                return JSONResponse({'ok': False,
                                     'error': '%s must be true or false' % name},
                                    status_code=400)
        source = _vision_source(body.source)
        status, error = await vision_command('vision_start', source,
                                             body.proc_width, body.fps_limit,
                                             body.face, body.audio, body.stt)
        return error if error is not None else {'ok': True, 'vision': status}

    @app.post('/api/vision/stop')
    async def vision_stop_route():
        status, error = await vision_command('vision_stop')
        return error if error is not None else {'ok': True, 'vision': status}

    # -- guided vision batteries + camera-only sessions --------------------
    # Additive v2 surface. A vision-only session records the same sidecar
    # stream but never touches the plate, so it works with the plate
    # disconnected. Errors keep the frozen {'ok': false, 'error': ...} JSON
    # contract: 400 bad request, 404 unknown session, 409 a plate
    # session/trial (or another vision-only session) owns the camera,
    # 503 the sidecar or the session store is not available.

    @app.get('/api/vision/guide')
    def vision_guide_route(mode: str = 'vision-only', views: str = 'side'):
        if vision_guide is None:
            return JSONResponse({'ok': False,
                                 'error': 'vision guide module is not installed'},
                                status_code=503)
        try:
            payload = vision_guide(mode=mode, views=vision_views(views))
        except ValueError as exc:
            return JSONResponse({'ok': False, 'error': str(exc)}, status_code=400)
        return {'ok': True, 'guide': payload}

    @app.post('/api/vision/session/start')
    async def vision_session_start_route(body: VisionSessionStart):
        if vision_disabled(acquisition):
            return vision_disabled_response()
        try:
            label = _vision_text(body.label, 'label', VISION_SESSION_LABEL_MAX)
            notes = _vision_text(body.notes, 'notes', VISION_SESSION_NOTES_MAX)
            view = _vision_view_value(body.view)
            consent = vision_consent(body.consent)
        except ValueError as exc:
            return JSONResponse({'ok': False, 'error': str(exc)}, status_code=400)
        sex = body.sex.strip().lower() if isinstance(body.sex, str) else body.sex
        error = athlete_reference_error(body.age, sex)
        if error is not None:
            return JSONResponse({'ok': False, 'error': error}, status_code=400)
        if getattr(acquisition, 'vision_store', None) is None:
            return JSONResponse({'ok': False, 'error': VISION_ONLY_STORE_MESSAGE},
                                status_code=503)
        if acquisition.vision_status().get('state') not in ('running', 'starting'):
            return JSONResponse({'ok': False, 'error': VISION_ONLY_NOT_RUNNING},
                                status_code=503)
        session, error = await vision_command('vision_only_start', label,
                                              body.age, sex, view, notes,
                                              consent)
        return error if error is not None else {'ok': True, 'session': session}

    @app.post('/api/vision/session/segment')
    async def vision_session_segment_route(body: VisionSegmentStart):
        """Start a test segment (H7); frames after it carry its id/test/limb."""
        if getattr(acquisition, 'vision_only_id', None) is None:
            return JSONResponse({'ok': False,
                                 'error': 'No vision-only session is active.'},
                                status_code=409)
        payload, error = await vision_command('vision_only_segment',
                                              body.test_id, body.limb,
                                              body.trial)
        return error if error is not None else {'ok': True, **payload}

    @app.post('/api/vision/audio')
    async def vision_audio_route(body: VisionAudioToggle):
        """Switch the microphone between sessions (M8); refuses mid-session."""
        if vision_disabled(acquisition):
            return vision_disabled_response()
        if not isinstance(body.enabled, bool):
            return JSONResponse({'ok': False,
                                 'error': 'enabled must be true or false'},
                                status_code=400)
        payload, error = await vision_command('vision_audio', body.enabled)
        return error if error is not None else {'ok': True, **payload}

    @app.post('/api/vision/session/stop')
    async def vision_session_stop_route():
        if getattr(acquisition, 'vision_only_id', None) is None:
            return JSONResponse({'ok': False,
                                 'error': 'No vision-only session is active.'},
                                status_code=409)
        session, error = await vision_command('vision_only_stop')
        return error if error is not None else {'ok': True, 'session': session}

    @app.get('/api/vision/sessions')
    def vision_sessions_route():
        return {'ok': True, 'sessions': acquisition.vision_only_list()}

    @app.get('/api/vision/session/{session_id}/report')
    def vision_session_report_route(session_id: str):
        """Self-contained printable page: inline CSS, no scripts, no network."""
        session = acquisition.vision_only_get(session_id)
        if session is None:
            return JSONResponse({'ok': False,
                                 'error': 'unknown vision-only session'},
                                status_code=404)
        if render_vision_only_html is not None:
            try:
                return HTMLResponse(
                    render_vision_only_html(session), status_code=200,
                    headers={'Content-Security-Policy': REPORT_PAGE_CSP})
            except Exception:
                # Fall through to the built-in page; never turn a stored
                # session into a 500 because a renderer failed.
                pass
        section = ''
        if render_vision_html is not None:
            try:
                section = render_vision_html(vision_report_session(session)) or ''
            except Exception:
                section = ''
        return HTMLResponse(render_vision_only_report_page(session, section),
                            status_code=200,
                            headers={'Content-Security-Policy': REPORT_PAGE_CSP})

    @app.get('/api/vision/session/{session_id}')
    def vision_session_route(session_id: str):
        session = acquisition.vision_only_get(session_id)
        if session is None:
            return JSONResponse({'ok': False,
                                 'error': 'unknown vision-only session'},
                                status_code=404)
        return {'ok': True, 'session': session}

    @app.post('/api/vision/event')
    async def vision_event_route(body: VisionEventMark):
        kind = str(body.type or '').strip().lower()
        if kind not in VISION_MARK_TYPES:
            return JSONResponse({'ok': False,
                                 'error': "event type must be 'pain', "
                                          "'did_not_understand' or 'note'"},
                                status_code=400)
        try:
            note = _vision_text(body.note, 'note', 500)
        except ValueError as exc:
            return JSONResponse({'ok': False, 'error': str(exc)}, status_code=400)
        payload, error = await vision_command('vision_mark', kind, note)
        return error if error is not None else {'ok': True, **payload}

    @app.get('/api/vision/state')
    def vision_state_route():
        return vision_state_payload()

    @app.get('/api/vision/feed')
    def vision_feed_route():
        """Proxy the worker's loopback MJPEG stream (one origin for the UI)."""
        status = acquisition.vision_status()
        if status.get('state') != 'running':
            return JSONResponse({'ok': False, 'error': 'vision is not running'},
                                status_code=503)
        port = status.get('port')
        if isinstance(port, bool) or not isinstance(port, int):
            port = (acquisition.config.get('vision') or {}).get('port', 8791)
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = 8791

        def upstream():
            url = 'http://127.0.0.1:%d/feed' % port
            try:
                response = urllib.request.urlopen(url, timeout=10)
            except Exception:
                return
            try:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    yield chunk
            except Exception:
                return
            finally:
                try:
                    response.close()
                except Exception:
                    pass

        return StreamingResponse(upstream(),
                                 media_type='multipart/x-mixed-replace; boundary=frame')

    # The vision panel polls one compact endpoint; /api/status (used by the
    # rest of the dashboard) carries the same status block, so a build
    # without this route still degrades to status-only rendering.
    @app.get('/api/state')
    def state_route():
        return vision_state_payload()

    @app.websocket('/ws')
    async def live(ws: WebSocket):
        origin = ws.headers.get('origin')
        if origin and urlsplit(origin).netloc != ws.headers.get('host'):
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            while True:
                await ws.send_json(acquisition.snapshot())
                await asyncio.sleep(0.1)
        except WebSocketDisconnect:
            return

    @app.get('/')
    def index():
        return FileResponse(ROOT/'static'/'index.html')

    app.mount('/static', StaticFiles(directory=ROOT/'static', check_dir=False), name='static')
    return app


app = create_app()
