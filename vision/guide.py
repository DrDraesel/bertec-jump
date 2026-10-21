"""Guided camera-assessment batteries for the Bertec vision lane - PURE STDLIB.

``guide(mode, views)`` answers one operator question: *what should I actually
record, how many trials, and where does the camera go?*  It is a data module:
no disk access, no clock, no imports beyond the standard library, so the app
can serve it on any request thread and the tests can diff it exactly.

Two batteries are defined:

``vision-only``
    Camera-only screening battery - usable with the plate disconnected.  Double-
    leg stance with eyes open and closed, single-leg stance left/right, tandem
    stance, squat, sit-to-stand and a 10 m walk.  The camera records POSTURE AND
    KINEMATICS during each task; it does NOT score hold time, repetition counts
    or gait events (no foot-lift, rep or gait segmentation exists).  Those
    outcomes must be timed/counted with the plate or a stopwatch and recorded
    as operator marks.  Nothing here produces a diagnosis, a fall-risk score or
    a "balance score"; it produces pose estimate clips an operator and a
    physician can review.

``with-plate``
    The same camera lane layered onto plate protocols: mCTSIB (firm and foam),
    single-leg stance, weight-bearing squat and a sequential weight-shift
    asymmetry task.  The plate measures centre of pressure; the camera only
    helps explain how the body moved.

Camera guidance is deliberately conservative: one camera gives one viewpoint,
so the default is a side or 30-45 degree oblique view at hip height, 2.5-4 m
away.  A front camera is *added* for face/frontal observation - it does not
replace the side view, and no single camera can measure everything (no force,
no depth, no plate-referenced centre of pressure).

Privacy defaults: camera recording, audio, speech-to-text and face analysis are
all OFF until the operator enables them for a session, and each is enabled only
after the athlete consents.  Audio is off unless enabled and consented; when it
is on, transcripts are stored locally as text and face values are
landmark-geometry proxies.
"""

from __future__ import annotations

__all__ = ['guide', 'MODES', 'VIEWS']

MODES = ('vision-only', 'with-plate')
VIEWS = ('side', 'front')

#: Ids are part of the frozen guide contract; the app keys its UI cards on them.
_SOURCES_VISION = [
    # BESS systematic review - the stance battery this guide mirrors.
    'https://pmc.ncbi.nlm.nih.gov/articles/PMC3445164/',
    # Unipedal stance normative values (eyes open / closed).
    'https://pubmed.ncbi.nlm.nih.gov/19839175/',
    # Concurrent validity of markerless mocap for static postural control.
    'https://doi.org/10.1016/j.jbiomech.2026.113371',
    # Single-camera MediaPipe knee-angle validation.
    'https://preprints.jmir.org/preprint/102399',
]

_SOURCES_PLATE = [
    # Instrumented mCTSIB test-retest reliability + MDC.
    'https://journals.lww.com/jgpt/Abstract/2021/10000/Test_Retest_Reliability_and_Minimal_Detectable.3.aspx',
    # Bertec CDP norms / best-practice update.
    'https://www.bertec.com/blog/cdp-best-practices-update-clinical-considerations-for-using-new-bertec-norms',
    # Unipedal stance normative values.
    'https://pubmed.ncbi.nlm.nih.gov/19839175/',
    # Markerless static postural control validity (single camera).
    'https://doi.org/10.1016/j.jbiomech.2026.113371',
]

_CAMERA_VIEW_TEXT = {
    'side': (
        'Side view (or a 30-45 degree oblique): place the camera level with the '
        'hips, 2.5-4 m from the standing area, aimed at the middle of the '
        'movement lane. The whole body - head to floor, both feet - must stay '
        'in frame for the entire trial. This is the default single-camera view: '
        'it shows sagittal knee/hip/ankle motion and trunk lean.'
    ),
    'front': (
        'Front view: place the camera facing the person at hip-to-chest height, '
        '2.5-3.5 m away, so both shoulders, both hips and both feet are visible '
        'and the person is centred. A front camera is needed for face '
        'expression proxies and for frontal-plane observation - it does NOT '
        'measure a valgus angle and it cannot see sagittal knee flexion.'
    ),
}

_SINGLE_CAMERA_LIMIT = (
    'One camera cannot measure everything. A single view has no depth, no '
    'force measurement and no plate-referenced calibration: it cannot produce '
    'a centre-of-pressure path, a limb-length-normalised displacement or a '
    'frontal-plane joint angle. Every pose number is a descriptive estimate '
    'from that viewpoint, comparable only within the same session and camera '
    'position - never across sessions, camera distances or views.'
)

_CAMERA_SETUP = {
    'default_view': 'side',
    'placement': (
        'Default single camera: side-on, or 30-45 degrees oblique to the '
        'direction the person faces, at hip height, 2.5-4 m away. Frame the '
        'whole body head-to-floor with a little margin; keep the camera still '
        '(tripod) and do not zoom or move it between trials.'
    ),
    'height': 'hip height (~0.9-1.1 m for most adults)',
    'height_m': [0.9, 1.1],
    'distance_m': [2.5, 4.0],
    'oblique_deg': [30, 45],
    'lighting': (
        'Even, diffuse light; avoid a bright window or lamp behind the person '
        '(backlight makes the silhouette and the pose landmarks unstable) and '
        'avoid hard shadows across the legs.'
    ),
    'framing': (
        'Head, both hands and both feet inside the frame for the whole trial, '
        'camera level (not looking down), floor line visible so foot contact '
        'can be judged.'
    ),
    'surface': (
        'Firm, level floor for the firm-surface conditions; a proper balance '
        'foam pad for the foam conditions. Never substitute a folded towel or '
        'cushion - the foam height and compliance are part of the condition.'
    ),
    'views': dict(_CAMERA_VIEW_TEXT),
    'single_camera_limit': _SINGLE_CAMERA_LIMIT,
    'note': (
        'Add a front camera only when face/frontal observation is wanted; keep '
        'the side camera as the primary view. Two cameras are two views, not a '
        'stereo calibration - do not combine them into one 3D measurement.'
    ),
}

_NOTES_COMMON = [
    'Stop the trial on dizziness, pain, loss of balance or a request to stop. '
    'A spotter stands within arm\'s reach for every eyes-closed, single-leg and '
    'tandem condition.',
    'Barefoot or the same shoes for every trial; the same floor spot, the same '
    'camera position and the same instructions within a session.',
    'Rest between trials (about the trial duration) so fatigue does not become '
    'the measured variable.',
    'The camera output is a markerless pose ESTIMATE. It is not a diagnosis, '
    'not a fall-risk score, not a balance score and not a medical device; '
    'interpretation and any return-to-sport decision stay physician-led.',
]

_NOTES_VISION_ONLY = _NOTES_COMMON + [
    'This battery works with the force plate disconnected - no plate data is '
    'used, and nothing here is a substitute for a plate measurement.',
    'Hold time, repetition counts and gait events are NOT scored by the '
    'camera. Time or count them with a plate or a stopwatch and record the '
    'result as an operator mark; a camera clip shows posture, not the outcome '
    'measure.',
    'Record the clip and the operator marks (pain, did not understand, note) '
    'in the same session so the observation has context.',
    'Audio, speech-to-text and face analysis are OFF by default. Enable them '
    'only after the athlete consents; audio is off unless enabled and '
    'consented, and transcripts are then stored locally as text.',
]

_NOTES_PLATE = _NOTES_COMMON + [
    'The plate must be connected, zeroed on the completely unloaded plate and '
    'streaming before the first trial; a camera-only clip is not a plate trial.',
    'Foam conditions (mCTSIB 3 and 4) REQUIRE the foam pad. Without foam, '
    'record only conditions 1 and 2 and label the session accordingly.',
    'Keep both feet on the plate for the double-leg conditions; for single-leg '
    'stance the stance foot stays on the plate and the other foot is off it.',
    'The SLS card is per side: three 30 s trials on one leg, then repeat the '
    'card for the other leg - six trials in total. The plate records the COP '
    'window; an early touch-down is an operator mark, not an automatically '
    'scored hold time.',
    'Audio, speech-to-text and face analysis are OFF by default. Enable them '
    'only after the athlete consents; audio is off unless enabled and '
    'consented, and transcripts are then stored locally as text.',
]


def _tests_vision_only():
    """Camera-only battery: eight tests, ordered easiest to hardest."""
    return [
        {
            'id': 'double_leg_eo',
            'name': 'Double-leg stance - eyes open',
            'duration_s': 30,
            'trials': 2,
            'instructions': (
                'Stand with feet about hip-width apart, arms relaxed at the '
                'sides, gaze on a fixed point at eye level. Stand as still as '
                'comfortably possible for 30 s. Two trials, rest in between.'
            ),
            'camera': 'side (default) or front',
            'needs': {'spotter': False, 'foam': False, 'chair': False,
                      'space_m': None},
        },
        {
            'id': 'double_leg_ec',
            'name': 'Double-leg stance - eyes closed',
            'duration_s': 30,
            'trials': 2,
            'instructions': (
                'Same stance as eyes open. Close the eyes, keep breathing '
                'normally, stand still for 30 s. A spotter stands within arm\'s '
                'reach and says "open" at 30 s.'
            ),
            'camera': 'side (default) or front',
            'needs': {'spotter': True, 'foam': False, 'chair': False,
                      'space_m': None},
        },
        {
            'id': 'single_leg_left',
            'name': 'Single-leg stance - left leg (camera records posture)',
            'duration_s': 30,
            'trials': 3,
            'instructions': (
                'Stand on the left leg, hip height, arms relaxed; lift the '
                'right foot clear of the floor without touching the standing '
                'leg. Record three 30 s trials. The camera records posture and '
                'kinematics ONLY: hold time is NOT scored from video (foot '
                'landmarks are the least accurate), so time each trial with a '
                'stopwatch and record the seconds as an operator mark.'
            ),
            'camera': 'side (default) or front',
            'needs': {'spotter': True, 'foam': False, 'chair': True,
                      'space_m': None},
        },
        {
            'id': 'single_leg_right',
            'name': 'Single-leg stance - right leg (camera records posture)',
            'duration_s': 30,
            'trials': 3,
            'instructions': (
                'Mirror of the left-leg test: stand on the right leg, lift the '
                'left foot clear, three 30 s trials. Hold time is NOT scored '
                'from video - time each trial with a stopwatch and record the '
                'seconds as an operator mark.'
            ),
            'camera': 'side (default) or front',
            'needs': {'spotter': True, 'foam': False, 'chair': True,
                      'space_m': None},
        },
        {
            'id': 'tandem_stance',
            'name': 'Tandem stance',
            'duration_s': 30,
            'trials': 2,
            'instructions': (
                'Place one foot directly in front of the other, heel touching '
                'the toes, arms relaxed. Hold 30 s; a spotter stands beside the '
                'person. Two trials.'
            ),
            'camera': 'side (default) or front',
            'needs': {'spotter': True, 'foam': False, 'chair': True,
                      'space_m': None},
        },
        {
            'id': 'squat_5_reps',
            'name': 'Squat - 5 repetitions (operator-counted)',
            'duration_s': None,
            'trials': 1,
            'instructions': (
                'Feet shoulder-width, arms forward for balance, sit back and '
                'down to a comfortable depth (no deeper than pain-free), then '
                'stand tall. Five slow repetitions, about 3 s down and 3 s up. '
                'The camera observes knee, hip and trunk kinematics during the '
                'movement; it does NOT count repetitions or measure tempo, so '
                'count the reps and note any deviation as an operator mark.'
            ),
            'camera': 'side (default) - full body, knees and feet in frame',
            'needs': {'spotter': False, 'foam': False, 'chair': False,
                      'space_m': 2},
        },
        {
            'id': 'sit_to_stand_5_reps',
            'name': 'Sit-to-stand - 5 repetitions (operator-counted)',
            'duration_s': None,
            'trials': 1,
            'instructions': (
                'Sit on a standard chair, feet flat and slightly behind the '
                'knees, arms crossed over the chest. Stand fully, then sit '
                'under control. Five repetitions; use the armrests only if '
                'needed and note it. The camera observes the movement; it does '
                'NOT count repetitions, so count them and record the result as '
                'an operator mark.'
            ),
            'camera': 'side (default) - chair, hips and feet in frame',
            'needs': {'spotter': False, 'foam': False, 'chair': True,
                      'space_m': 2},
        },
        {
            'id': 'walk_10m',
            'name': 'Walk 10 m (operator-timed; no gait metrics)',
            'duration_s': None,
            'trials': 2,
            'instructions': (
                'Walk 10 m at a comfortable pace, turn around and walk back. '
                'Skip this test when there is not about 12 m of clear floor; '
                'record the reason in the session notes. The camera observes '
                'posture inside the framed section only: it does NOT measure '
                'gait speed, step length, cadence or gait events (there is no '
                'gait segmentation), so time the walk with a stopwatch and '
                'record the result as an operator mark.'
            ),
            'camera': 'side (default) - pan is not allowed; frame the middle '
                      '6 m and accept entry/exit',
            'needs': {'spotter': False, 'foam': False, 'chair': False,
                      'space_m': 12},
        },
    ]


def _tests_with_plate():
    """Plate + camera battery: mCTSIB split by surface, SLS, WBS, asymmetry."""
    return [
        {
            'id': 'mctsib_firm',
            'name': 'mCTSIB - conditions 1-2 (firm surface)',
            'duration_s': 30,
            'trials': 2,
            'instructions': (
                'Stand quietly on the plate, feet together, arms across the '
                'chest. Condition 1: eyes open, 30 s. Condition 2: eyes '
                'closed, 30 s. One trial per condition.'
            ),
            'camera': 'side (default) or front',
            'needs': {'spotter': False, 'foam': False, 'chair': False,
                      'space_m': None},
        },
        {
            'id': 'mctsib_foam',
            'name': 'mCTSIB - conditions 3-4 (foam surface)',
            'duration_s': 30,
            'trials': 2,
            'instructions': (
                'Place the balance foam pad on the plate, stand on the foam, '
                'feet together, arms across the chest. Condition 3: eyes open, '
                '30 s. Condition 4: eyes closed, 30 s with a spotter within '
                'arm\'s reach. FOAM IS REQUIRED for both conditions - do not '
                'substitute a towel, cushion or folded mat.'
            ),
            'camera': 'side (default) or front',
            'needs': {'spotter': True, 'foam': True, 'chair': False,
                      'space_m': None},
        },
        {
            'id': 'single_leg_sls',
            'name': 'Single-leg stance (SLS) on the plate - one side, 3 trials',
            'duration_s': 30,
            'trials': 3,
            'instructions': (
                'Stand on one foot on the plate, arms relaxed, gaze fixed, for '
                'three 30 s trials ON THIS SIDE. The stance foot stays on the '
                'plate, the lifted foot off it. Then repeat this card for the '
                'other side (three trials per side, six trials in total). The '
                'plate records the COP window and the camera records posture; '
                'an early touch-down is NOT scored automatically, so record it '
                'as an operator mark with the time you observed.'
            ),
            'camera': 'side (default) or front',
            'needs': {'spotter': True, 'foam': False, 'chair': True,
                      'space_m': None},
        },
        {
            'id': 'wbs',
            'name': 'Weight-bearing squat (WBS)',
            'duration_s': None,
            'trials': 1,
            'instructions': (
                'Stand on the plate with feet shoulder-width and even weight '
                'through both feet (watch the left/right load). Perform five '
                'slow squats to a comfortable depth and return to standing.'
            ),
            'camera': 'side (default) - knees, hips and feet in frame',
            'needs': {'spotter': False, 'foam': False, 'chair': False,
                      'space_m': 2},
        },
        {
            'id': 'asymmetry_sequential',
            'name': 'Sequential weight-shift asymmetry',
            'duration_s': 60,
            'trials': 2,
            'instructions': (
                'Stand on the plate, feet hip-width. Shift the weight onto the '
                'left foot for 10 s, then onto the right foot for 10 s, then '
                'return to even stance for 10 s. Repeat the cycle twice; do '
                'not lift either foot.'
            ),
            'camera': 'front (recommended) - both feet and hips in frame',
            'needs': {'spotter': False, 'foam': False, 'chair': False,
                      'space_m': None},
        },
    ]


def _normalise_views(views):
    """Return a validated, de-duplicated tuple of view names.

    Accepts a string (``'side'``, ``'side,front'``, ``'side+front'``,
    ``'side front'``), a list/tuple of strings, or ``None``/empty for the
    default single side view.  Raises ValueError for anything else - the API
    maps that to a 400 rather than silently recording a view nobody can serve.
    """
    if views is None:
        return ('side',)
    if isinstance(views, str):
        text = views.strip().lower()
        for separator in ('+', ';', '|', ' '):
            text = text.replace(separator, ',')
        items = [part for part in (chunk.strip() for chunk in text.split(','))
                 if part]
    elif isinstance(views, (list, tuple)):
        items = [str(item).strip().lower() for item in views
                 if str(item).strip()]
    else:
        raise ValueError("views must be a string or a list of view names")
    if not items:
        return ('side',)
    ordered = []
    for item in items:
        if item not in VIEWS:
            raise ValueError("unknown camera view %r; views must be %s"
                             % (item, ' and/or '.join(VIEWS)))
        if item not in ordered:
            ordered.append(item)
    return tuple(ordered)


def _camera_setup(views):
    """Camera block for the selected views (copy - callers may mutate it)."""
    setup = {key: (dict(value) if isinstance(value, dict) else value)
             for key, value in _CAMERA_SETUP.items()}
    setup['selected'] = list(views)
    setup['advice'] = [_CAMERA_VIEW_TEXT[view] for view in views]
    setup['frontal_added'] = 'front' in views
    if 'front' in views and 'side' not in views:
        setup['note'] = (
            'Front view only: face and frontal observation are available, but '
            'sagittal knee/hip/ankle motion is not. Add a side camera when '
            'sagittal kinematics matter.'
        )
    return setup


def guide(mode='vision-only', views=('side',)):
    """Recommended assessment battery + camera placement for one mode.

    ``mode`` is ``'vision-only'`` (plate disconnected, camera-only screening)
    or ``'with-plate'`` (camera alongside plate protocols).  ``views`` is the
    selected camera view(s): ``('side',)`` (default), ``('front',)`` or
    ``('side', 'front')``.

    Returns a JSON-ready dict:

    ``mode`` / ``views``
        The normalised request echo.
    ``tests``
        Ordered list of ``{id, name, duration_s, trials, instructions,
        camera, needs}``.  ``duration_s`` is ``None`` for repetition or
        distance tasks, whose dose is stated in ``name``/``instructions``;
        ``needs`` is
        ``{spotter, foam, chair, space_m}``.
    ``camera_setup``
        Placement text, hip-height and 2.5-4 m distance guidance, the 30-45
        degree oblique default, lighting/framing/surface rules, the selected
        view advice and the single-camera limitation statement.
    ``notes``
        Safety and interpretation notes for the mode - including the foam
        requirement and the no-diagnosis statement.
    ``sources``
        Research-document URLs behind the battery (4).

    Raises ValueError for an unknown mode or view; nothing is silently
    substituted.
    """
    text = str(mode or '').strip().lower().replace('_', '-')
    if text not in MODES:
        raise ValueError("unknown mode %r; mode must be 'vision-only' or "
                         "'with-plate'" % (mode,))
    selected = _normalise_views(views)
    if text == 'vision-only':
        tests = _tests_vision_only()
        notes = list(_NOTES_VISION_ONLY)
        sources = list(_SOURCES_VISION)
    else:
        tests = _tests_with_plate()
        notes = list(_NOTES_PLATE)
        sources = list(_SOURCES_PLATE)
    return {
        'mode': text,
        'views': list(selected),
        'tests': tests,
        'camera_setup': _camera_setup(selected),
        'notes': notes,
        'sources': sources,
    }
