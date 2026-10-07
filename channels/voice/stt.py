"""Speech in: cut the caller's audio into utterances and transcribe them (faster-whisper on CPU).

Call audio arrives as PCM16 mono at 48 kHz in ~10 ms frames.
"""
import os
import re
import time
from pathlib import Path

import numpy as np
from faster_whisper import WhisperModel

CALL_RATE = 48000
WHISPER_RATE = 16000
VAD_SR = 16000
VAD_CHUNK = 512  # Silero's recommended chunk size at 16 kHz (32 ms); model only accepts fixed sizes.
VAD_PATH = Path(os.environ.get("JARVIS_VOICE_HOME", Path.home() / ".jarvis-voice")) / "silero_vad.onnx"
VAULT_PATH = Path(os.environ.get("OBSIDIAN_VAULT_PATH", ""))
VOCAB_REFRESH_S = 600  # re-scan the vault for new project/term names this often, not every call

# Terms the vault's own text won't reliably contain (people, generic jargon), always included.
STATIC_VOCAB = ["Jarvis", "Hermes", "Telegram", "vault sync", "repo", "commit"]
if os.environ.get("JARVIS_OWNER_NAME", "").strip():
    STATIC_VOCAB.insert(1, os.environ["JARVIS_OWNER_NAME"].strip())
_GITHUB_RE = re.compile(r"github\.com/[\w.-]+/([\w.-]+)")
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


def build_vocab(vault: Path = VAULT_PATH, limit: int = 15) -> str:
    """Whisper's initial_prompt, built from the vault instead of a hand-maintained list: project
    folder names, repo names pulled from github.com/org/repo mentions in each project's
    overview.md (a repo name often appears only in the overview's `repo:` line, not as a
    folder name), and _index.md's active-projects wikilink aliases.

    Deliberately narrow and deliberately capped low: testing showed a longer prompt stuffed with
    unrelated jargon (OSS library names pulled from research notes) made Whisper WORSE at the
    exact terms it was meant to help with (a project name came out as a similar common word)
    -- lots of initial_prompt text dilutes the bias rather than reinforcing it.
    So: only overview.md (each project's identity file), not every note under projects/, and a
    low cap. New projects/terms still show up automatically on the next refresh
    (VOCAB_REFRESH_S) -- no code change needed, unlike the old hardcoded VOCAB.
    """
    terms, seen = [], {t.lower() for t in STATIC_VOCAB}

    def add(t):
        # Split "proj (RepoName)" into two clean terms instead of leaving a mangled "proj (RepoName"
        # from edge-only stripping -- the vault's own _index.md stores aliases exactly this way.
        for part in re.split(r"[()]", t):
            part = part.strip().strip(".,:;\"'")
            if part and part.lower() not in seen and len(part) <= 40:
                seen.add(part.lower())
                terms.append(part)

    if vault.is_dir():
        projects_dir = vault / "projects"
        if projects_dir.is_dir():
            for d in sorted(projects_dir.iterdir()):
                if not d.is_dir():
                    continue
                add(d.name)
                # Keep a project's repo alias right next to its folder name -- testing showed
                # Whisper's prompt bias is much weaker when the two sit apart in the list (nine
                # terms apart the repo name was misheard; adjacent and title-cased it was right).
                f = d / "overview.md"
                if f.is_file():
                    for m in _GITHUB_RE.finditer(f.read_text(errors="replace")):
                        name = m.group(1)
                        add(name.capitalize() if name.isalpha() and name.islower() else name)
        index = vault / "_index.md"
        if index.is_file():
            for m in _WIKILINK_RE.finditer(index.read_text(errors="replace")):
                add((m.group(2) or m.group(1)).split("/")[-1])
    return ", ".join(STATIC_VOCAB + terms[:limit]) + "."


def rms(pcm: bytes) -> float:
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(x * x))) if x.size else 0.0


VAD_CONTEXT = 64  # samples of trailing context the model prepends to every chunk (silero-vad's OnnxWrapper)


class SileroVAD:
    """Silero VAD (MIT) run directly with onnxruntime -- no torch, just the 2.3 MB ONNX graph
    (`setup.sh` fetches it from the upstream repo). Trained speech/non-speech, so a steady
    background noise floor doesn't read as "loud" the way a fixed RMS threshold did.

    The model is stateful across two things, both reset together: the 128-wide recurrent
    `state`, and a 64-sample `context` carried from the tail of the previous chunk and
    prepended to the next one (matching upstream silero_vad.utils_vad.OnnxWrapper -- without
    this prefix the model never sees a chunk boundary correctly and output stays ~0 regardless
    of input).
    """

    def __init__(self, path=VAD_PATH):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self.sess = ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])
        self.sr = np.array(VAD_SR, dtype=np.int64)
        self.reset()

    def reset(self):
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros(VAD_CONTEXT, dtype=np.float32)

    def prob(self, chunk: np.ndarray) -> float:
        """chunk: exactly VAD_CHUNK float32 samples at 16 kHz in [-1, 1]."""
        x = np.concatenate([self.context, chunk])
        out, self.state = self.sess.run(None, {"input": x[None, :], "state": self.state, "sr": self.sr})
        self.context = x[-VAD_CONTEXT:]
        return float(out[0, 0])


class Endpointer:
    """VAD endpointing: an utterance ends after `silence_s` of quiet that follows speech.

    Keeps `preroll_s` of audio from before the speech onset so the first syllable isn't clipped.
    Loudness is decided by Silero VAD on 32 ms chunks (resampled from the 48 kHz call audio),
    not a fixed energy threshold, so it holds up under a steady noise floor.
    """

    BYTES_PER_VAD_CHUNK = VAD_CHUNK * 3 * 2  # 512 samples @16k == 1536 samples @48k, PCM16

    def __init__(self, vad=None, rate=CALL_RATE, threshold=0.5, silence_s=0.6, preroll_s=0.3, max_s=30.0):
        self.vad = vad or SileroVAD()
        self.bps = rate * 2  # bytes per second, PCM16 mono
        self.threshold, self.silence_s, self.preroll, self.max_bytes = threshold, silence_s, int(preroll_s * self.bps), int(max_s * self.bps)
        self.reset()

    def reset(self):
        self.buf, self.pre, self.in_speech, self.quiet = bytearray(), bytearray(), False, 0.0
        self.vad_buf = bytearray()
        self.loud = False
        self.vad.reset()

    def _update_loud(self, pcm: bytes):
        self.vad_buf += pcm
        while len(self.vad_buf) >= self.BYTES_PER_VAD_CHUNK:
            chunk48 = self.vad_buf[: self.BYTES_PER_VAD_CHUNK]
            del self.vad_buf[: self.BYTES_PER_VAD_CHUNK]
            x = np.frombuffer(bytes(chunk48), dtype=np.int16).astype(np.float32) / 32768.0
            x16 = x[: len(x) // 3 * 3].reshape(-1, 3).mean(axis=1)  # 48 kHz -> 16 kHz
            self.loud = self.vad.prob(x16) >= self.threshold

    def feed(self, pcm: bytes) -> bytes | None:
        self._update_loud(pcm)
        loud = self.loud
        if not self.in_speech:
            if not loud:
                self.pre += pcm
                del self.pre[:-self.preroll or None]
                return None
            self.in_speech, self.buf = True, bytearray(self.pre)
        self.buf += pcm
        self.quiet = 0.0 if loud else self.quiet + len(pcm) / self.bps
        if self.quiet >= self.silence_s or len(self.buf) >= self.max_bytes:
            out = bytes(self.buf)
            self.reset()
            return out
        return None


class STT:
    """Whisper small.en on the GPU (180 ms per utterance, measured 2026-09-28), falling back to base.en on
    the CPU (~390 ms) when CUDA can't be used, so a GPU problem never breaks calls."""

    def __init__(self, model="small.en", device="cuda", threads=2):
        try:
            if device != "cuda":
                raise RuntimeError("GPU not requested")
            self.model = WhisperModel(model, device="cuda", compute_type="float16")
            self._warm_up()  # a missing CUDA library only fails on the first transcribe, so fail here instead
            self.where = f"{model} on GPU"
        except Exception as e:
            self.model = WhisperModel("base.en", device="cpu", compute_type="int8", cpu_threads=threads)
            self._warm_up()
            self.where = f"base.en on CPU (GPU unavailable: {str(e)[:80]})"

    def _warm_up(self):
        list(self.model.transcribe(np.zeros(16000, dtype=np.float32), language="en", beam_size=1)[0])
        self.vocab = build_vocab()
        self._vocab_built_at = time.monotonic()

    def _current_vocab(self) -> str:
        if time.monotonic() - self._vocab_built_at >= VOCAB_REFRESH_S:
            self.vocab = build_vocab()
            self._vocab_built_at = time.monotonic()
        return self.vocab

    def transcribe(self, pcm48: bytes) -> str:
        x = np.frombuffer(pcm48, dtype=np.int16).astype(np.float32) / 32768.0
        x = x[: len(x) // 3 * 3].reshape(-1, 3).mean(axis=1)  # 48 kHz -> 16 kHz
        segs, info = self.model.transcribe(x, language="en", beam_size=1, initial_prompt=self._current_vocab(), vad_filter=True)
        text = " ".join(s.text.strip() for s in segs).strip()
        return text if info.duration_after_vad >= 0.3 else ""
