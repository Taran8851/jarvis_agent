"""Tests for jarvis-voice. Run on the box from this directory: ~/.jarvis-voice/venv/bin/python test_voice.py
The STT/TTS round trip needs the Piper voice in ~/.jarvis-voice; the live Haiku test needs a logged-in `claude`.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

import brain
import stt
import tgcall

RATE = 48000


def tone(seconds, amp=0.3):
    t = np.arange(int(seconds * RATE)) / RATE
    return (amp * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16).tobytes()


def silence(seconds):
    return b"\0" * (int(seconds * RATE) * 2)


def noise(seconds, amp=0.02, seed=0):
    """A steady low-level hiss: what a fixed RMS threshold used to mistake for speech."""
    rng = np.random.default_rng(seed)
    return (rng.normal(0, amp, int(seconds * RATE)).clip(-1, 1) * 32767).astype(np.int16).tobytes()


def frames(pcm, size=960):
    return [pcm[i:i + size] for i in range(0, len(pcm), size)]


def speech():
    """Real synthesized speech (Piper), cached, so the VAD has something to actually classify."""
    if not hasattr(speech, "_cache"):
        import tts
        speech._cache = tts.TTS(tgcall.VOICE).synth("The vault sync finished and three tests failed.")
    return speech._cache


@unittest.skipUnless(tgcall.VOICE.exists(), "Piper voice not installed (needed to synthesize test speech)")
class TestEndpointer(unittest.TestCase):
    def test_one_utterance_after_trailing_silence(self):
        ep = stt.Endpointer()
        utt = speech()
        outs = [u for f in frames(silence(1) + utt + silence(1)) if (u := ep.feed(f))]
        self.assertEqual(len(outs), 1)
        dur = len(outs[0]) / (RATE * 2)
        self.assertAlmostEqual(dur, 0.3 + len(utt) / (RATE * 2) + 0.6, delta=0.25)  # preroll + speech + silence window (+/- one 32ms VAD chunk's slop)

    def test_silence_only_yields_nothing(self):
        ep = stt.Endpointer()
        self.assertEqual([u for f in frames(silence(3)) if ep.feed(f)], [])

    def test_background_noise_does_not_block_endpoint(self):
        """The old RMS endpointer never saw silence under a steady noise floor (ran to max_s).
        Silero should still detect speech start/end against it."""
        ep = stt.Endpointer()
        utt = speech()
        outs = [u for f in frames(noise(0.5) + utt + noise(1.5)) if (u := ep.feed(f))]
        self.assertEqual(len(outs), 1)

    def test_short_pause_does_not_split(self):
        ep = stt.Endpointer()
        utt = speech()
        outs = [u for f in frames(utt + silence(0.3) + utt + silence(1)) if (u := ep.feed(f))]
        self.assertEqual(len(outs), 1)


@unittest.skipUnless(tgcall.VOICE.exists(), "Piper voice not installed")
class TestSpeechRoundTrip(unittest.TestCase):
    def test_tts_then_stt(self):
        import tts
        speech = tts.TTS(tgcall.VOICE)
        t = time.monotonic()
        pcm = speech.synth("The vault sync finished and three tests failed.")
        synth_s = time.monotonic() - t
        audio_s = len(pcm) / (RATE * 2)
        self.assertEqual(len(pcm) % 2, 0)
        self.assertTrue(1.5 < audio_s < 6, audio_s)
        t = time.monotonic()
        text = stt.STT().transcribe(pcm).lower()
        print(f"\n  tts {synth_s:.2f}s for {audio_s:.2f}s audio; stt {time.monotonic() - t:.2f}s -> {text!r}")
        for word in ("vault", "sync", "finished", "tests", "failed"):
            self.assertIn(word, text)


FAKE_CLAUDE = r'''
import json, sys
replies = {"hi": "Hey. What do you need?",
           "state of atlas": "Checking. [[ASK: read the Atlas overview and summarize its current state]]",
           "bye": "Bye, talk soon. [[HANGUP]]"}
for line in sys.stdin:
    text = json.loads(line)["message"]["content"]
    out = replies.get(text, "Okay.")
    for i in range(0, len(out), 4):  # stream in small deltas, like the real CLI
        ev = {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": out[i:i+4]}}}
        print(json.dumps(ev), flush=True)
    print(json.dumps({"type": "result"}), flush=True)
'''


class TestBrain(unittest.TestCase):
    def run_turns(self, cmd, texts):
        async def go():
            b = brain.Brain(cmd=cmd)
            await b.start()
            out = [[x async for x in b.turn(t)] for t in texts]
            await b.close()
            return out
        return asyncio.run(go())

    def test_sentences_and_markers(self):
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(FAKE_CLAUDE)
        try:
            hi, ask, bye = self.run_turns([sys.executable, f.name], ["hi", "state of atlas", "bye"])
        finally:
            os.unlink(f.name)
        self.assertEqual(hi, [("say", "Hey."), ("say", "What do you need?")])
        self.assertEqual(ask, [("say", "Checking."), ("ask", "read the Atlas overview and summarize its current state")])
        self.assertEqual(bye, [("say", "Bye, talk soon."), ("hangup", None)])

    def test_split_markers(self):
        self.assertEqual(brain.split_markers("Ok. [[ASK: x y]]"), ("Ok.", "x y", False))
        self.assertEqual(brain.split_markers("Bye. [[HANGUP]]"), ("Bye.", None, True))
        self.assertEqual(brain.split_markers("Half a marker [[AS"), ("Half a marker", None, False))

    @unittest.skipUnless(shutil.which("claude") and os.environ.get("LIVE"), "set LIVE=1 to call the real Haiku")
    def test_live_haiku(self):
        (turn,) = self.run_turns(None, ["What is the state of my Atlas project?"])
        print(f"\n  live: {turn}")
        self.assertIn("ask", [k for k, _ in turn])  # it can't know project state, so it must hand off


class TestPacing(unittest.TestCase):
    def test_play_pcm_is_real_time(self):
        sent = []

        class Calls:
            async def send_frame(self, chat_id, device, data):
                sent.append((time.monotonic(), len(data)))

        svc = type("Svc", (), {"calls": Calls()})()
        call = tgcall.Call.__new__(tgcall.Call)
        call.svc, call.chat_id, call.ended, call.speaking, call.ep = svc, 1, False, False, stt.Endpointer()
        asyncio.run(call.play_pcm(tone(1.0) + b"\1\0"))  # 1 s + a partial frame
        self.assertEqual([n for _, n in sent], [960] * 101)  # last frame padded
        span = sent[-1][0] - sent[0][0]  # frame 0 goes out at t=0, frame 100 at t=1.00 s
        self.assertAlmostEqual(span, 1.00, delta=0.03)
        self.assertFalse(call.speaking)


class TestConfig(unittest.TestCase):
    def test_owner_is_first_allowed_user(self):
        os.environ["TELEGRAM_ALLOWED_USERS"] = "123, 456"
        self.assertEqual(tgcall.owner_id(), 123)


if __name__ == "__main__":
    unittest.main(verbosity=2)
