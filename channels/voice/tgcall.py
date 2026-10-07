"""Jarvis voice calls over Telegram: a Telegram user account on the box that answers the owner's calls and calls them.

  python tgcall.py login              one-time interactive login of the Jarvis account (phone + code)
  python tgcall.py serve              run the call service (systemd: jarvis-voice)
  python tgcall.py call-me [reason]   ask the running service to call the owner

Only the owner (first id in TELEGRAM_ALLOWED_USERS) may call; anyone else is hung up on.
Env (from ~/.hermes/.env): TELEGRAM_CALL_API_ID, TELEGRAM_CALL_API_HASH, TELEGRAM_ALLOWED_USERS,
TELEGRAM_BOT_TOKEN + TELEGRAM_HOME_CHANNEL (to text results that outlive the call).
"""
import asyncio
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

STATE = Path(os.environ.get("JARVIS_VOICE_HOME", Path.home() / ".jarvis-voice"))
SOCK = STATE / "ctl.sock"
VOICE = STATE / "en_US-lessac-medium.onnx"
HERMES = os.environ.get("HERMES_BIN", str(Path.home() / ".local/bin/hermes"))
ASK_SPEAK_WITHIN_S = 15     # a Hermes answer faster than this is spoken; slower ones go to Telegram
ASK_TIMEOUT_S = 900
FRAME_BYTES = 960           # 10 ms of PCM16 mono at 48 kHz
log = logging.getLogger("jarvis-voice")


def owner_id():
    return int(os.environ["TELEGRAM_ALLOWED_USERS"].split(",")[0].strip())


def notify(text):
    """Text the owner through the Hermes bot (Bot API), used when a result arrives after the call ended or went long."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_HOME_CHANNEL")
    if not (token and chat):
        log.warning("notify: no TELEGRAM_BOT_TOKEN/TELEGRAM_HOME_CHANNEL")
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000]}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=15).read()
        log.info("texted result to Telegram (%d chars)", len(text))
    except Exception as e:
        log.warning("notify failed: %s", e)


async def ask_hermes(task):
    """Run one Hermes turn (same agent as the Telegram bot) and return its answer text."""
    prompt = f"(From a voice call with the owner. Answer concisely; the gist will be read aloud.) {task}"
    p = await asyncio.create_subprocess_exec(HERMES, "chat", "-Q", "--oneshot", "-q", prompt, cwd=str(Path.home()),
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(p.communicate(), ASK_TIMEOUT_S)
    lines = [ln for ln in out.decode(errors="replace").splitlines() if not ln.startswith("session_id:")]
    return "\n".join(lines).strip() or "(no answer)"


class Call:
    """One live call: endpoint the caller's audio -> STT -> brain -> TTS -> frames back, half-duplex."""

    def __init__(self, svc, chat_id, opening):
        from brain import Brain
        from stt import Endpointer
        self.svc, self.chat_id, self.opening = svc, chat_id, opening
        self.ep, self.brain = Endpointer(), Brain()
        self.utterances = asyncio.Queue()
        self.speaking, self.ended = False, False
        self.task = None

    def on_audio(self, pcm):
        if self.speaking or self.ended:
            return  # half-duplex: ignore the line while Jarvis talks (no barge-in yet)
        utt = self.ep.feed(pcm)
        if utt:
            self.utterances.put_nowait((time.monotonic(), utt))

    async def say(self, text):
        t = time.monotonic()
        pcm = await asyncio.to_thread(self.svc.tts.synth, text)
        log.info("tts %.0f ms for %r", (time.monotonic() - t) * 1000, text[:60])
        await self.play_pcm(pcm)

    async def play_pcm(self, pcm):
        from pytgcalls.types import Device
        self.speaking = True
        try:
            start = time.monotonic()
            for i in range(0, len(pcm), FRAME_BYTES):
                if self.ended:
                    return
                chunk = pcm[i:i + FRAME_BYTES].ljust(FRAME_BYTES, b"\0")
                await self.svc.calls.send_frame(self.chat_id, Device.MICROPHONE, chunk)
                delay = start + (i // FRAME_BYTES + 1) * 0.01 - time.monotonic()  # real-time pacing, no drift
                if delay > 0:
                    await asyncio.sleep(delay)
        finally:
            await asyncio.sleep(0.3)  # let the tail and its echo die before listening again
            self.ep.reset()
            self.speaking = False

    async def respond(self, text, t_heard=None):
        """One brain turn; speaks sentences as they arrive and runs any hand-off."""
        first = True
        async for kind, arg in self.brain.turn(text):
            if kind == "say":
                if first and t_heard:
                    log.info("voice-to-voice (end of speech -> first sentence ready) %.2f s", time.monotonic() - t_heard)
                first = False
                await self.say(arg)
            elif kind == "ask":
                asyncio.create_task(self.hand_off(arg))
            elif kind == "hangup":
                await self.svc.hang_up(self)

    async def hand_off(self, task):
        log.info("ask hermes: %s", task[:200])
        job = asyncio.create_task(ask_hermes(task))
        try:
            answer = await asyncio.wait_for(asyncio.shield(job), ASK_SPEAK_WITHIN_S)
        except asyncio.TimeoutError:
            if not self.ended:
                await self.say("That will take a bit. I'll text you the result.")
            try:
                answer = await job
            except Exception as e:
                answer = f"failed: {e}"
            await asyncio.to_thread(notify, f"Jarvis (from call): {answer}")
            return
        except Exception as e:
            answer = f"The agent failed: {e}"
        if self.ended:
            await asyncio.to_thread(notify, f"Jarvis (from call): {answer}")
        else:
            await self.respond(f"[RESULT] {answer}")

    async def run(self):
        try:
            await self.brain.start()
            await self.say(self.opening)
            while not self.ended:
                t_heard, utt = await self.utterances.get()
                t = time.monotonic()
                text = await asyncio.to_thread(self.svc.stt.transcribe, utt)
                log.info("stt %.0f ms: %r", (time.monotonic() - t) * 1000, text)
                if text:
                    await self.respond(text, t_heard)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("call failed")
            await self.svc.hang_up(self)
        finally:
            await self.brain.close()


class Service:
    def __init__(self):
        from stt import STT
        from tts import TTS
        self.owner = owner_id()
        self.stt = STT(os.environ.get("JARVIS_STT_MODEL", "base.en"), os.environ.get("JARVIS_STT_DEVICE", "cuda"))
        log.info("speech-to-text: %s", self.stt.where)
        self.tts = TTS(VOICE)
        self.call = None

    async def run(self):
        from pytgcalls import PyTgCalls, filters
        from pytgcalls.types import ChatUpdate, Device, Direction, StreamFrames
        from telethon import TelegramClient
        self.client = TelegramClient(str(STATE / "jarvis"), int(os.environ["TELEGRAM_CALL_API_ID"]), os.environ["TELEGRAM_CALL_API_HASH"])
        await self.client.start()  # uses the saved session; never prompts under systemd
        self.calls = PyTgCalls(self.client)
        await self.calls.start()
        try:
            await self.client.get_input_entity(self.owner)
        except ValueError:
            log.warning("owner %s not in this account's cache: message the Jarvis account once from your Telegram, "
                        "or outgoing calls fail", self.owner)

        @self.calls.on_update(filters.chat_update(ChatUpdate.Status.INCOMING_CALL))
        async def incoming(_, update):
            if update.chat_id != self.owner or self.call:
                log.info("rejecting call from %s (busy=%s)", update.chat_id, bool(self.call))
                try:
                    await self.calls.leave_call(update.chat_id)
                except Exception as e:
                    log.info("reject: %s", e)
                return
            await self.start_call(update.chat_id, "Jarvis here.")

        @self.calls.on_update(filters.chat_update(ChatUpdate.Status.LEFT_CALL))
        async def left(_, update):
            if self.call and update.chat_id == self.call.chat_id:
                log.info("call ended by the other side")
                await self.end(self.call)

        @self.calls.on_update(filters.stream_frame(Direction.INCOMING, Device.MICROPHONE))
        async def audio(_, update: StreamFrames):
            if self.call and update.chat_id == self.call.chat_id:
                for f in update.frames:
                    self.call.on_audio(f.frame)

        server = await asyncio.start_unix_server(self.ctl, path=str(SOCK))
        os.chmod(SOCK, 0o600)
        log.info("jarvis-voice ready (owner %s)", self.owner)
        async with server:
            await self.client.run_until_disconnected()

    async def start_call(self, chat_id, opening):
        from pytgcalls.types import ExternalMedia, MediaStream, RecordStream
        from pytgcalls.types.raw import AudioParameters
        audio = AudioParameters(48000, 1)
        self.call = Call(self, chat_id, opening)
        await self.calls.play(chat_id, MediaStream(ExternalMedia.AUDIO, audio))  # answers an incoming call / dials out
        await self.calls.record(chat_id, RecordStream(True, audio))
        self.call.task = asyncio.create_task(self.call.run())
        log.info("call up with %s", chat_id)

    async def end(self, call):
        if call.ended:
            return
        call.ended = True
        if self.call is call:
            self.call = None
        if call.task and call.task is not asyncio.current_task():
            call.task.cancel()

    async def hang_up(self, call):
        await self.end(call)
        try:
            await self.calls.leave_call(call.chat_id)
        except Exception as e:
            log.info("leave_call: %s", e)

    async def ctl(self, reader, writer):
        """Local control socket: one JSON line {"cmd": "call", "reason": "..."}."""
        try:
            req = json.loads(await reader.readline())
            if req.get("cmd") != "call":
                reply = {"ok": False, "error": "unknown cmd"}
            elif self.call:
                reply = {"ok": False, "error": "already in a call"}
            else:
                reason = req.get("reason") or ""
                opening = f"Jarvis here. {reason}".strip()
                # Outgoing P2P media sometimes never connects: ~10 s then TelegramServerError (ntgcalls#70, open).
                # Redialling works, but only after discarding the stuck call, or the callee reads as "busy".
                for attempt in (1, 2, 3):
                    try:
                        await self.start_call(self.owner, opening)
                        reply = {"ok": True}
                        break
                    except Exception as e:
                        log.warning("dial attempt %d failed: %r", attempt, e)
                        self.call = None
                        reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        try:
                            await self.calls.leave_call(self.owner)
                        except Exception as e2:
                            log.info("discard after failed dial: %r", e2)
                        await asyncio.sleep(2)
        except Exception as e:
            reply = {"ok": False, "error": str(e)}
        writer.write((json.dumps(reply) + "\n").encode())
        await writer.drain()
        writer.close()


async def call_me(reason):
    reader, writer = await asyncio.open_unix_connection(str(SOCK))
    writer.write((json.dumps({"cmd": "call", "reason": reason}) + "\n").encode())
    await writer.drain()
    print((await asyncio.wait_for(reader.readline(), 90)).decode().strip())


async def login():
    from telethon import TelegramClient
    client = TelegramClient(str(STATE / "jarvis"), int(os.environ["TELEGRAM_CALL_API_ID"]), os.environ["TELEGRAM_CALL_API_HASH"])
    await client.start()  # prompts for phone, code and 2FA password on this terminal
    me = await client.get_me()
    print(f"logged in as {me.first_name} (id {me.id}); session at {STATE / 'jarvis.session'}")
    await client.disconnect()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.umask(0o077)  # the Telethon session file is a credential
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "login":
        asyncio.run(login())
    elif cmd == "call-me":
        asyncio.run(call_me(" ".join(sys.argv[2:])))
    elif cmd == "serve":
        if not (STATE / "jarvis.session").exists():
            sys.exit(f"no Telegram session at {STATE}/jarvis.session: run `tgcall.py login` once in a terminal")
        asyncio.run(Service().run())
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
