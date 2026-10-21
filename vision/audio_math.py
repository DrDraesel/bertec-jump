"""Audio features for the Bertec vision sidecar - PURE STDLIB.

This module is imported by ``vision/worker.py`` under the mediapipe interpreter
AND by the Bertec app's stdlib-only venv (tests, reports).  Nothing here may
pull in numpy, sounddevice, scipy or any other third-party package; the worker
itself does the capture and hands plain Python float lists to these functions.

Sample convention
-----------------
``samples`` is a sequence of mono float samples in [-1, 1] (full scale = 1.0)
at ``fs`` Hz - 16 kHz everywhere inside the worker.  Extra/odd values are
skipped rather than allowed to poison a statistic.

What these numbers are
----------------------
* :func:`dbfs` - RMS level relative to full scale, floored at
  :data:`DBFS_FLOOR` so a silent window still serialises to valid JSON (JSON
  has no -Infinity).
* :func:`pitch_hz` - autocorrelation F0 estimate in :data:`PITCH_MIN_HZ` ..
  :data:`PITCH_MAX_HZ`.  It is a *rough* tracker: octave errors happen on
  speech with a strong second harmonic, and unvoiced sounds correctly return
  ``None``.  Do not report it as a clinical voice measure.
* :func:`speech_activity` - an energy + zero-crossing-rate heuristic, i.e. a
  VAD-like gate, NOT speech recognition.  It will also open on loud non-speech
  noise (a door slam, a dropped plate) and it closes on perfectly steady tones
  because the adaptive floor treats them as background.  The keyword layer
  below is what looks at *content*.
* :func:`keywords` - literal phrase matching over a local STT transcript.
  Local Whisper output is frequently wrong, drops accents and invents words;
  every flag here is a HINT for the operator, never a finding.  Spanish
  ``para``/``pare`` are ordinary prepositions ("for", "to", "stop!") and were
  removed from the stop list (M5): a literal matcher cannot tell an imperative
  from a preposition, and a false "stop phrase" alarm in a clinical session is
  worse than a missed one.  ``stop``, ``basta`` and ``detente`` remain.
"""

from __future__ import annotations

import math
import re
import unicodedata

#: Anything quieter than this serialises as :data:`DBFS_FLOOR`, not -inf.
DBFS_FLOOR = -120.0

#: Autocorrelation search band.  Below 60 Hz a 0.5 s window holds <30 periods
#: and the estimate becomes meaningless; above 400 Hz child/female F0 still
#: fits while keeping the lag search cheap.
PITCH_MIN_HZ = 60.0
PITCH_MAX_HZ = 400.0

#: Decimate to about this rate before autocorrelation.  At 16 kHz the lag
#: search would be ~4x more expensive for no accuracy gain (400 Hz needs only
#: a 5 kHz Nyquist); 4 kHz keeps lags >= 10 samples so parabolic interpolation
#: stays accurate.
PITCH_TARGET_FS = 4000.0

#: Normalised autocorrelation peak below which the window is called aperiodic.
#: White noise sits near 1/sqrt(N) (~0.01 at 8000 samples); a voiced vowel is
#: typically > 0.6.  0.30 is deliberately conservative - "no pitch" is a better
#: answer than a random one.
PITCH_MIN_CLARITY = 0.30

#: Speech heuristic framing: 30 ms analysis window, 15 ms hop.
SPEECH_FRAME_S = 0.030
SPEECH_HOP_S = 0.015

#: Zero-crossing band for "speech-like".  A steady low tone has a ZCR near
#: 2*f/fs (0.027 at 220 Hz / 16 kHz) and voiced speech lands close to that;
#: the band only rejects DC-ish drift (<0.008) and near-Nyquist hiss (>0.55).
SPEECH_ZCR_MIN = 0.008
SPEECH_ZCR_MAX = 0.55

#: Fraction of sub-frames that must be active before the window is "speech".
SPEECH_ACTIVE_MIN = 0.25

#: Absolute activity floor.  Below this the window is room noise whatever the
#: adaptive threshold says.
SPEECH_FLOOR_DBFS = -55.0

#: How far a sub-frame must rise above the window's own noise percentile.
SPEECH_NOISE_MARGIN_DB = 6.0

#: Default event thresholds for :func:`events_for_window`.
AUDIO_THRESHOLDS = {
    "loud_dbfs": -25.0,     # a raised voice / clatter; not a calibration
    "loud_windows": 2,      # consecutive 0.5 s windows => "sustained"
}

#: Phrase tables.  Accents are stripped before matching, so the accented
#: spellings below are documentation, not patterns.  ``para``/``pare`` are NOT
#: stop phrases: they are ordinary Spanish prepositions far more often than
#: imperatives, and a literal matcher cannot disambiguate (M5).
PAIN_PHRASES = ("pain", "hurt", "hurts", "hurting", "sore",
                "duele", "dolor", "molestia")
COMPREHENSION_PHRASES = ("no entiendo", "no entendi", "no comprendo",
                         "no comprendo nada", "confused", "again", "repite",
                         "what?")
STOP_PHRASES = ("stop", "basta", "detente")

# Word-boundary matching on the accent-stripped, punctuation-normalised text.
# Alternatives are ordered longest-first so "no comprendo nada" is recorded as
# the whole phrase rather than its prefix.
_PAIN_RE = re.compile(r"\b(?:pain|hurts?|hurting|sore|duele|dolor|molestia)\b")
_COMP_RE = re.compile(
    r"\b(?:no\s+entiendo|no\s+entendi|no\s+comprendo(?:\s+nada)?|"
    r"confused|again|repite)\b|\bwhat\s*\?")
_STOP_RE = re.compile(r"\b(?:stop|basta|detente)\b")

_CATEGORIES = (("pain", _PAIN_RE), ("comprehension", _COMP_RE),
               ("stop", _STOP_RE))


# ------------------------------------------------------------------- helpers

def _floats(samples):
    """Finite floats from an arbitrary sample sequence (bad entries skipped)."""
    if samples is None:
        return []
    try:
        iterator = iter(samples)
    except TypeError:
        return []
    out = []
    for raw in iterator:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            out.append(value)
    return out


def _finite(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _percentile(values, q):
    """Linear-interpolated percentile of a non-empty sequence."""
    vals = sorted(values)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = max(0.0, min(1.0, float(q))) * (len(vals) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(vals) - 1)
    frac = pos - lo
    return vals[lo] * (1.0 - frac) + vals[hi] * frac


# --------------------------------------------------------------------- level

def dbfs(samples):
    """RMS level of ``samples`` in dBFS (full scale 1.0), floored.

    Returns ``None`` for an empty/missing window and :data:`DBFS_FLOOR` for
    digital silence - JSON cannot carry ``-Infinity``, so silence is clamped
    instead of rounded to a plausible-looking level.
    """
    vals = _floats(samples)
    if not vals:
        return None
    total = 0.0
    for value in vals:
        total += value * value
    rms = math.sqrt(total / len(vals))
    if rms <= 1e-9:
        return DBFS_FLOOR
    return max(DBFS_FLOOR, 20.0 * math.log10(rms))


def decimate(samples, factor):
    """Box-average groups of ``factor`` samples (crude anti-alias downsampler).

    Used when a microphone only opens at 48 kHz: averaging before taking every
    ``factor``-th sample keeps high-frequency hiss from folding down into the
    pitch band.  It is not a designed low-pass filter and the worker says so.
    """
    vals = _floats(samples)
    try:
        factor = int(factor)
    except (TypeError, ValueError):
        return vals
    if factor <= 1:
        return vals
    count = len(vals) // factor
    out = []
    for index in range(count):
        start = index * factor
        chunk = vals[start:start + factor]
        out.append(sum(chunk) / float(factor))
    return out


# --------------------------------------------------------------------- pitch

def _autocorr(x, lag):
    total = 0.0
    n = len(x) - lag
    for i in range(n):
        total += x[i] * x[i + lag]
    return total


def pitch_hz(samples, fs=16000.0):
    """Autocorrelation F0 estimate in Hz, or ``None`` when aperiodic.

    The window is mean-removed, decimated to about :data:`PITCH_TARGET_FS`,
    normalised by zero-lag energy, and searched for lags in
    :data:`PITCH_MIN_HZ` .. :data:`PITCH_MAX_HZ`.  The winning lag is refined
    with a 3-point parabola, which is what keeps a 220 Hz sine inside 1 % even
    at 4 kHz (where the lag is only ~18 samples).

    ``None`` is returned for: too few samples, non-finite/zero ``fs``,
    silence, or a peak clarity below :data:`PITCH_MIN_CLARITY`.  A
    *rough* estimate - octave errors are possible on harmonic-rich speech.
    """
    vals = _floats(samples)
    try:
        fs = float(fs)
    except (TypeError, ValueError):
        return None
    if fs <= 0 or len(vals) < 8:
        return None

    factor = max(1, int(fs // PITCH_TARGET_FS))
    if factor > 1:
        vals = decimate(vals, factor)
        fs_eff = fs / float(factor)
    else:
        fs_eff = fs
    if len(vals) < 8:
        return None

    mean = sum(vals) / len(vals)
    x = [value - mean for value in vals]
    r0 = sum(value * value for value in x)
    if r0 <= 1e-12:
        return None

    min_lag = max(2, int(fs_eff / PITCH_MAX_HZ))
    max_lag = int(math.ceil(fs_eff / PITCH_MIN_HZ))
    max_lag = min(max_lag, len(x) // 2)
    if max_lag <= min_lag:
        return None

    best_lag = None
    best = -2.0
    for lag in range(min_lag, max_lag + 1):
        score = _autocorr(x, lag) / r0
        if score > best:
            best = score
            best_lag = lag
    if best_lag is None or best < PITCH_MIN_CLARITY:
        return None

    offset = 0.0
    if min_lag < best_lag < max_lag:
        prev = _autocorr(x, best_lag - 1) / r0
        nxt = _autocorr(x, best_lag + 1) / r0
        denom = prev - 2.0 * best + nxt
        if abs(denom) > 1e-12:
            offset = 0.5 * (prev - nxt) / denom
            offset = max(-0.5, min(0.5, offset))

    period = best_lag + offset
    if period <= 0:
        return None
    freq = fs_eff / period
    if not math.isfinite(freq):
        return None
    if freq < PITCH_MIN_HZ:
        freq = PITCH_MIN_HZ
    elif freq > PITCH_MAX_HZ:
        freq = PITCH_MAX_HZ
    return float(freq)


# -------------------------------------------------------------------- speech

def _zcr(frame):
    """Zero-crossing rate of one frame in [0, 1] (sign changes / gaps)."""
    if len(frame) < 2:
        return 0.0
    changes = 0
    prev = frame[0]
    for value in frame[1:]:
        if (prev < 0.0 and value >= 0.0) or (prev >= 0.0 and value < 0.0):
            changes += 1
        prev = value
    return changes / float(len(frame) - 1)


def speech_activity(samples, fs=16000.0):
    """Energy + zero-crossing heuristic: is somebody talking in this window?

    Splits the window into 30 ms frames (15 ms hop), scores each frame's RMS
    level against an adaptive threshold (the window's own 20th-percentile
    level + :data:`SPEECH_NOISE_MARGIN_DB`, never below
    :data:`SPEECH_FLOOR_DBFS`) and requires a speech-plausible ZCR.  The window
    is "speech" when at least :data:`SPEECH_ACTIVE_MIN` of its frames are
    active.

    Returns ``{'speech': bool, 'score': 0..1, 'dbfs': float|None,
    'zcr': float, 'frames': int}``.  ``score`` is the active-frame fraction,
    not a probability.  This is a gate for the STT layer and a coarse "is the
    subject vocalising" hint - it is not speech recognition and it cannot tell
    a voice from a loud clatter.
    """
    vals = _floats(samples)
    try:
        fs = float(fs)
    except (TypeError, ValueError):
        fs = 0.0
    result = {"speech": False, "score": 0.0, "dbfs": dbfs(vals), "zcr": 0.0,
              "frames": 0}
    if not vals or fs <= 0:
        return result

    frame_len = max(8, int(round(SPEECH_FRAME_S * fs)))
    hop = max(1, int(round(SPEECH_HOP_S * fs)))
    if len(vals) <= frame_len:
        frames = [vals]
    else:
        frames = [vals[start:start + frame_len]
                  for start in range(0, len(vals) - frame_len + 1, hop)]
    if not frames:
        return result

    levels = []
    zcrs = []
    for frame in frames:
        level = dbfs(frame)
        levels.append(DBFS_FLOOR if level is None else level)
        zcrs.append(_zcr(frame))

    if len(frames) >= 4:
        noise = _percentile(levels, 0.20)
        threshold = max(SPEECH_FLOOR_DBFS,
                        (noise if noise is not None else SPEECH_FLOOR_DBFS)
                        + SPEECH_NOISE_MARGIN_DB)
    else:
        # Too few frames for a meaningful noise floor - fall back to the
        # absolute gate rather than declaring the whole window background.
        threshold = SPEECH_FLOOR_DBFS

    active = 0
    for level, zcr in zip(levels, zcrs):
        if level > threshold and SPEECH_ZCR_MIN <= zcr <= SPEECH_ZCR_MAX:
            active += 1
    score = active / float(len(frames))
    overall = result["dbfs"]
    result["frames"] = len(frames)
    result["score"] = float(score)
    result["zcr"] = float(_percentile(zcrs, 0.5) or 0.0)
    result["speech"] = bool(score >= SPEECH_ACTIVE_MIN
                            and overall is not None
                            and overall > SPEECH_FLOOR_DBFS)
    return result


# ------------------------------------------------------------------ keywords

def normalize_text(text):
    """Lowercase, accent-strip and punctuation-normalise an STT transcript.

    ``"¿NO ENTENDÍ?"`` -> ``"?no entendi?"``.  Keeps ``?`` and ``!`` because
    the English "what?" pattern needs the question mark; collapses everything
    else to spaces.
    """
    raw = "" if text is None else str(text)
    decomposed = unicodedata.normalize("NFD", raw)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    stripped = stripped.replace("\u00bf", "?").replace("\u00a1", "!")
    lowered = stripped.lower()
    cleaned = re.sub(r"[^a-z0-9?!'\s]", " ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


def keywords(text):
    """Match pain / comprehension / stop phrases in a transcript.

    Returns ``{'pain': bool, 'comprehension': bool, 'stop': bool,
    'matches': [{'category': ..., 'phrase': ...}, ...]}``.  Matching is on
    whole words over :func:`normalize_text` output, so accents (``entendí``)
    and case do not matter.

    KNOWN AMBIGUITIES - these are literal phrase hits, not understanding:
    English ``again`` is common in normal speech, ``stop`` can appear in
    quoted or casual speech, and local STT mangles short words.  Every hit
    needs the operator to confirm it.  Spanish ``para``/``pare`` were REMOVED
    from the stop list (M5): as prepositions they fired on ordinary sentences
    like "esto es para la rodilla", and this matcher has no imperative context
    to distinguish the two uses.
    """
    normalized = normalize_text(text)
    out = {"pain": False, "comprehension": False, "stop": False, "matches": []}
    if not normalized:
        return out
    for category, pattern in _CATEGORIES:
        phrases = []
        for match in pattern.finditer(normalized):
            phrase = match.group(0).strip()
            if phrase and phrase not in phrases:
                phrases.append(phrase)
        if phrases:
            out[category] = True
            for phrase in phrases:
                out["matches"].append({"category": category, "phrase": phrase})
    return out


def empty_flags():
    """A flags dict with every category false (for "no transcript yet")."""
    return {"pain": False, "comprehension": False, "stop": False, "matches": []}


# -------------------------------------------------------------------- events

def _short(text, limit=80):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "\u2026"


def events_for_window(features, thresholds=None):
    """Operator-facing events for one audio window.

    ``features`` is the worker's audio dict (all keys optional)::

        {"dbfs": -18.4, "speech": true, "pitch_hz": 132.0, "loud_streak": 3,
         "stt": {"state": "ready", "text": "...", "flags": {...}}}

    ``thresholds`` overrides :data:`AUDIO_THRESHOLDS` (``loud_dbfs``,
    ``loud_windows``).

    Returns a list of ``{'event', 'severity', 'detail'}`` dicts, in a stable
    order: ``audio_loud``, ``audio_pain_phrase``, ``audio_unknown_phrase``,
    ``audio_stop_phrase``.  Pure and debounce-free - the worker applies its
    own per-event cooldown, so the same window evaluated twice gives the same
    list.
    """
    out = []
    if not isinstance(features, dict):
        return out
    limits = dict(AUDIO_THRESHOLDS)
    if isinstance(thresholds, dict):
        limits.update(thresholds)

    level = _finite(features.get("dbfs"))
    speech = features.get("speech")
    loud = _finite(limits.get("loud_dbfs"))
    streak = features.get("loud_streak")
    sustained = _finite(limits.get("loud_windows")) or 1
    if (level is not None and loud is not None and level > loud
            and speech is not False):
        if streak is None or _finite(streak) is None or float(streak) >= sustained:
            out.append({"event": "audio_loud", "severity": "notice",
                        "detail": "loud audio {:.1f} dBFS (threshold {:.0f})"
                                  .format(level, loud)})

    stt = features.get("stt") if isinstance(features.get("stt"), dict) else {}
    flags = stt.get("flags") if isinstance(stt.get("flags"), dict) else {}
    if not flags and isinstance(features.get("flags"), dict):
        flags = features["flags"]
    text = stt.get("text") or features.get("text") or ""
    note = " - local STT, may be wrong"

    if flags.get("pain"):
        out.append({"event": "audio_pain_phrase", "severity": "warning",
                    "detail": "possible pain phrase: \"{}\"{}"
                              .format(_short(text), note)})
    if flags.get("comprehension"):
        out.append({"event": "audio_unknown_phrase", "severity": "notice",
                    "detail": "possible confusion phrase: \"{}\"{}"
                              .format(_short(text), note)})
    if flags.get("stop"):
        out.append({"event": "audio_stop_phrase", "severity": "warning",
                    "detail": "stop phrase: \"{}\"{}"
                              .format(_short(text), note)})
    return out
