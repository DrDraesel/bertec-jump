"""TEST ONLY: pure-stdlib tests for vision.audio_math.

Every signal here is generated in-process from the formula written next to it
(no microphone, no recorded audio, no STT model).  Expected values are derived
from those formulas - e.g. a 0.5 amplitude sine has RMS 0.5/sqrt(2), so its
level is 20*log10(0.5/sqrt(2)) dBFS - not from running the implementation and
copying its output.
"""

import math
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vision import audio_math  # noqa: E402

FS = 16000


def sine(freq, amp=0.5, seconds=0.5, fs=FS, phase=0.0):
    """Pure sine at ``freq`` Hz, ``amp`` peak amplitude, full-scale 1.0."""
    count = int(seconds * fs)
    return [amp * math.sin(2.0 * math.pi * freq * i / fs + phase)
            for i in range(count)]


def lcg_noise(count=8000, amp=0.4, seed=12345):
    """Deterministic pseudo-noise (LCG) - no random module, no numpy."""
    state = seed
    out = []
    for _ in range(count):
        state = (1103515245 * state + 12345) % (2 ** 31)
        out.append((state / 2 ** 31 - 0.5) * amp)
    return out


def syllabic_voice(seconds=0.5, amp=0.30, f0=140.0, fs=FS, rate=4.0):
    """Harmonic-rich, amplitude-modulated signal: a crude voiced syllable.

    Fundamental f0 with 2nd/3rd harmonics, multiplied by a raised-cosine
    envelope at ``rate`` Hz so there are quiet troughs between "syllables".
    """
    out = []
    count = int(seconds * fs)
    for i in range(count):
        t = i / fs
        env = 0.5 * (1.0 - math.cos(2.0 * math.pi * rate * t))
        out.append(amp * env * (
            math.sin(2.0 * math.pi * f0 * t)
            + 0.5 * math.sin(2.0 * math.pi * 2.0 * f0 * t)
            + 0.25 * math.sin(2.0 * math.pi * 3.0 * f0 * t)))
    return out


class DbfsTests(unittest.TestCase):
    def test_full_scale_sine(self):
        # RMS of a unit sine is 1/sqrt(2) -> -3.01 dBFS.
        self.assertAlmostEqual(audio_math.dbfs(sine(220, amp=1.0)),
                               -3.0103, places=3)

    def test_half_scale_sine_matches_rms_formula(self):
        expected = 20.0 * math.log10(0.5 / math.sqrt(2.0))
        self.assertAlmostEqual(audio_math.dbfs(sine(220, amp=0.5)),
                               expected, places=6)

    def test_silence_is_floored_not_infinite(self):
        level = audio_math.dbfs([0.0] * 8000)
        self.assertEqual(level, audio_math.DBFS_FLOOR)
        self.assertTrue(math.isfinite(level))

    def test_empty_and_none_are_none(self):
        self.assertIsNone(audio_math.dbfs([]))
        self.assertIsNone(audio_math.dbfs(None))

    def test_bad_entries_are_skipped(self):
        samples = [float("nan"), 0.5, None, "0.5", float("inf")]
        self.assertAlmostEqual(audio_math.dbfs(samples), 20.0 * math.log10(0.5),
                               places=6)


class PitchTests(unittest.TestCase):
    def assert_pitch(self, expected, samples=None):
        value = audio_math.pitch_hz(samples if samples is not None
                                    else sine(expected), FS)
        self.assertIsNotNone(value, f"no pitch for {expected} Hz")
        self.assertLess(abs(value - expected) / expected, 0.02,
                        f"{value} not within 2% of {expected}")

    def test_220_hz_within_two_percent(self):
        self.assert_pitch(220.0)

    def test_low_and_high_end_of_band(self):
        self.assert_pitch(110.0)
        self.assert_pitch(330.0)
        self.assert_pitch(400.0)

    def test_silence_has_no_pitch(self):
        self.assertIsNone(audio_math.pitch_hz([0.0] * 8000, FS))

    def test_aperiodic_noise_has_no_pitch(self):
        # White-ish noise has no autocorrelation peak: clarity ~1/sqrt(N).
        self.assertIsNone(audio_math.pitch_hz(lcg_noise(), FS))

    def test_short_or_invalid_input_is_none(self):
        self.assertIsNone(audio_math.pitch_hz([], FS))
        self.assertIsNone(audio_math.pitch_hz([0.1, -0.1], FS))
        self.assertIsNone(audio_math.pitch_hz(sine(220), 0))
        self.assertIsNone(audio_math.pitch_hz(sine(220), "fast"))

    def test_pitch_is_robust_to_dc_offset(self):
        samples = [value + 0.3 for value in sine(180.0)]
        value = audio_math.pitch_hz(samples, FS)
        self.assertIsNotNone(value)
        self.assertLess(abs(value - 180.0) / 180.0, 0.02)


class SpeechActivityTests(unittest.TestCase):
    def test_voiced_signal_is_speech(self):
        result = audio_math.speech_activity(syllabic_voice(), FS)
        self.assertTrue(result["speech"])
        self.assertGreater(result["score"], 0.5)
        self.assertGreater(result["dbfs"], -30.0)
        self.assertGreater(result["frames"], 10)

    def test_silence_is_not_speech(self):
        result = audio_math.speech_activity([0.0] * 8000, FS)
        self.assertFalse(result["speech"])
        self.assertEqual(result["score"], 0.0)

    def test_quiet_room_noise_is_not_speech(self):
        # ~-59 dBFS uniform noise: below the absolute activity floor.
        result = audio_math.speech_activity(lcg_noise(amp=0.004), FS)
        self.assertFalse(result["speech"])
        self.assertLess(result["dbfs"], audio_math.SPEECH_FLOOR_DBFS)

    def test_empty_window(self):
        result = audio_math.speech_activity([], FS)
        self.assertFalse(result["speech"])
        self.assertEqual(result["score"], 0.0)
        self.assertIsNone(result["dbfs"])

    def test_score_is_a_fraction(self):
        result = audio_math.speech_activity(syllabic_voice(), FS)
        self.assertGreaterEqual(result["score"], 0.0)
        self.assertLessEqual(result["score"], 1.0)


class KeywordTests(unittest.TestCase):
    def test_english_pain(self):
        for phrase in ("that hurts", "I have pain here", "it is sore"):
            flags = audio_math.keywords(phrase)
            self.assertTrue(flags["pain"], phrase)
            self.assertFalse(flags["stop"], phrase)

    def test_spanish_pain(self):
        for phrase in ("me duele la rodilla", "tengo dolor", "siento molestia"):
            self.assertTrue(audio_math.keywords(phrase)["pain"], phrase)

    def test_spanish_comprehension(self):
        for phrase in ("no entiendo", "no comprendo nada", "repite por favor"):
            self.assertTrue(audio_math.keywords(phrase)["comprehension"], phrase)

    def test_english_comprehension(self):
        for phrase in ("I am confused", "say that again", "What?"):
            self.assertTrue(audio_math.keywords(phrase)["comprehension"], phrase)

    def test_stop_phrases_es_en(self):
        for phrase in ("stop", "stop now", "basta", "¡Basta!", "detente"):
            self.assertTrue(audio_math.keywords(phrase)["stop"], phrase)

    def test_spanish_para_is_not_a_stop_phrase(self):
        # M5 (QC 2026-10-21): "para"/"pare" are ordinary prepositions far more
        # often than imperatives.  A literal matcher cannot tell "esto es para
        # la rodilla" from an imperative, so they were removed rather than
        # left as a guaranteed false positive in every Spanish session.
        for phrase in ("esto es para la rodilla", "pare la grabacion",
                       "para", "pare"):
            flags = audio_math.keywords(phrase)
            self.assertFalse(flags["stop"], phrase)
            self.assertNotIn("para", audio_math.STOP_PHRASES)
            self.assertNotIn("pare", audio_math.STOP_PHRASES)

    def test_accent_and_case_insensitive(self):
        accented = audio_math.keywords("NO ENTENDÍ")
        plain = audio_math.keywords("no entendi")
        self.assertTrue(accented["comprehension"])
        self.assertEqual(accented, plain)
        self.assertTrue(audio_math.keywords("ME DUELE")["pain"])
        self.assertTrue(audio_math.keywords("DÓLOR")["pain"])

    def test_matches_list_records_category_and_phrase(self):
        flags = audio_math.keywords("no comprendo nada, me duele")
        categories = {match["category"] for match in flags["matches"]}
        self.assertEqual(categories, {"comprehension", "pain"})
        phrases = {match["phrase"] for match in flags["matches"]}
        self.assertIn("no comprendo nada", phrases)
        self.assertIn("duele", phrases)

    def test_unrelated_text_matches_nothing(self):
        flags = audio_math.keywords("the weather is nice today")
        self.assertFalse(flags["pain"])
        self.assertFalse(flags["comprehension"])
        self.assertFalse(flags["stop"])
        self.assertEqual(flags["matches"], [])

    def test_empty_text(self):
        for value in (None, "", "   ", "..."):
            flags = audio_math.keywords(value)
            self.assertEqual(flags["matches"], [])
            self.assertFalse(any(flags[key] for key in
                                 ("pain", "comprehension", "stop")))

    def test_spanish_para_does_not_raise_a_stop_event(self):
        # M5: the same false positive must not reach the event stream either.
        flags = audio_math.keywords("esto es para la rodilla")
        events = audio_math.events_for_window(
            {"dbfs": -30.0, "speech": True,
             "stt": {"state": "ready", "text": "esto es para la rodilla",
                     "flags": flags}})
        self.assertEqual([event["event"] for event in events], [])

    def test_word_boundaries_do_not_fire_inside_words(self):
        flags = audio_math.keywords("parable spartan repiten")
        self.assertFalse(flags["stop"])
        self.assertFalse(flags["comprehension"])

    def test_what_without_question_mark_is_not_comprehension(self):
        self.assertFalse(audio_math.keywords("what a nice day")["comprehension"])


class EventTests(unittest.TestCase):
    def test_loud_speech_emits_audio_loud(self):
        events = audio_math.events_for_window(
            {"dbfs": -12.0, "speech": True, "loud_streak": 3})
        self.assertEqual([event["event"] for event in events], ["audio_loud"])
        self.assertEqual(events[0]["severity"], "notice")
        self.assertIn("-12.0", events[0]["detail"])

    def test_quiet_window_emits_nothing(self):
        self.assertEqual(audio_math.events_for_window(
            {"dbfs": -48.0, "speech": True, "loud_streak": 0}), [])

    def test_loud_but_not_sustained_emits_nothing(self):
        self.assertEqual(audio_math.events_for_window(
            {"dbfs": -12.0, "speech": True, "loud_streak": 1}), [])

    def test_loud_non_speech_emits_nothing(self):
        self.assertEqual(audio_math.events_for_window(
            {"dbfs": -12.0, "speech": False, "loud_streak": 9}), [])

    def test_custom_loud_threshold(self):
        events = audio_math.events_for_window(
            {"dbfs": -35.0, "speech": True, "loud_streak": 2},
            {"loud_dbfs": -40.0, "loud_windows": 2})
        self.assertEqual([event["event"] for event in events], ["audio_loud"])

    def test_pain_phrase_event_is_a_warning_with_text(self):
        events = audio_math.events_for_window(
            {"dbfs": -30.0, "speech": True,
             "stt": {"state": "ready", "text": "me duele mucho",
                     "flags": audio_math.keywords("me duele mucho")}})
        names = [event["event"] for event in events]
        self.assertEqual(names, ["audio_pain_phrase"])
        self.assertEqual(events[0]["severity"], "warning")
        self.assertIn("duele", events[0]["detail"])
        self.assertIn("may be wrong", events[0]["detail"])

    def test_comprehension_and_stop_events(self):
        events = audio_math.events_for_window(
            {"dbfs": -30.0, "speech": True,
             "stt": {"state": "ready", "text": "no entiendo, basta",
                     "flags": audio_math.keywords("no entiendo, basta")}})
        self.assertEqual([event["event"] for event in events],
                         ["audio_unknown_phrase", "audio_stop_phrase"])
        self.assertEqual(events[0]["severity"], "notice")
        self.assertEqual(events[1]["severity"], "warning")

    def test_flags_may_arrive_top_level(self):
        events = audio_math.events_for_window(
            {"dbfs": -30.0, "flags": {"pain": True}, "text": "ay"})
        self.assertEqual([event["event"] for event in events],
                         ["audio_pain_phrase"])

    def test_missing_or_bad_features(self):
        self.assertEqual(audio_math.events_for_window(None), [])
        self.assertEqual(audio_math.events_for_window({}), [])
        self.assertEqual(audio_math.events_for_window("loud"), [])

    def test_function_is_pure_and_repeatable(self):
        features = {"dbfs": -10.0, "speech": True, "loud_streak": 4}
        first = audio_math.events_for_window(features)
        second = audio_math.events_for_window(features)
        self.assertEqual(first, second)
        self.assertEqual(features, {"dbfs": -10.0, "speech": True,
                                    "loud_streak": 4})


class StdlibOnlyTests(unittest.TestCase):
    def test_no_third_party_imports(self):
        source = Path(audio_math.__file__).read_text(encoding="utf-8")
        banned = re.compile(
            r"^\s*(?:import|from)\s+(numpy|cv2|mediapipe|sounddevice|"
            r"faster_whisper|scipy|soundfile)\b", re.MULTILINE)
        self.assertIsNone(banned.search(source),
                          "audio_math must stay pure stdlib")


class WorkerAudioPathTests(unittest.TestCase):
    """The worker's audio thread glue, exercised without numpy or a device.

    ``AudioMonitor._process`` only needs an object with ``tolist()``, so a
    duck-typed block stands in for the numpy array sounddevice would deliver.
    This pins the payload shape and the speech -> loud-streak -> event chain
    that :mod:`vision.audio_math` alone cannot cover.
    """

    class Block:
        def __init__(self, values):
            self.values = values

        def tolist(self):
            return self.values

    def setUp(self):
        from vision import worker
        self.worker = worker
        self.app = worker.App("test", 0)
        self.monitor = worker.AudioMonitor(self.app, stt=False)

    def test_silence_payload_shape(self):
        self.monitor._process(self.Block([0.0] * 8000))
        payload = self.monitor.snapshot()
        self.assertEqual(set(payload), {"dbfs", "pitch_hz", "speech", "stt"})
        self.assertFalse(payload["speech"])
        self.assertIsNone(payload["pitch_hz"])
        self.assertEqual(payload["dbfs"], -120.0)
        self.assertEqual(payload["stt"]["state"], "off")
        self.assertEqual(self.app.drain_events(), [])

    def test_tone_payload_has_pitch_and_no_events(self):
        import math
        tone = [0.2 * math.sin(2 * math.pi * 180 * i / 16000)
                for i in range(8000)]
        self.monitor._process(self.Block(tone))
        payload = self.monitor.snapshot()
        self.assertAlmostEqual(payload["pitch_hz"], 180.0, delta=4.0)
        self.assertAlmostEqual(payload["dbfs"], -17.0, delta=1.0)
        # A perfectly steady tone is background to the adaptive floor - the
        # documented limit of the energy+ZCR heuristic, not speech.
        self.assertFalse(payload["speech"])
        self.assertEqual(self.app.drain_events(), [])

    def test_sustained_voiced_window_raises_audio_loud_event(self):
        signal = [value * 1.6 for value in syllabic_voice()]
        self.monitor._process(self.Block(signal))
        first = self.app.drain_events()
        self.assertEqual(first, [])  # one window is not "sustained"
        self.monitor._process(self.Block(signal))
        events = self.app.drain_events()
        self.assertEqual([event["event"] for event in events], ["audio_loud"])
        self.assertEqual(events[0]["severity"], "notice")
        self.assertIn("loud audio", events[0]["detail"])


if __name__ == "__main__":
    unittest.main()
