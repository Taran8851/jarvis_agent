"""Speech out: text -> PCM16 mono 48 kHz for the call (Piper on CPU)."""
import numpy as np
from piper import PiperVoice

CALL_RATE = 48000


class TTS:
    def __init__(self, voice_path):
        self.voice = PiperVoice.load(str(voice_path))

    def synth(self, text: str) -> bytes:
        chunks = list(self.voice.synthesize(text))
        if not chunks:
            return b""
        rate = chunks[0].sample_rate
        x = np.concatenate([c.audio_float_array for c in chunks]).astype(np.float32)
        n = int(round(len(x) * CALL_RATE / rate))
        y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)  # linear resample to 48 kHz
        return (np.clip(y, -1, 1) * 32767).astype(np.int16).tobytes()
