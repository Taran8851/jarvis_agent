"""Conversation layer for a call: one persistent Claude Code process (Haiku, no tools), fed turn by turn.

A persistent process answers in ~1.2 s median to first word on the box; spawning `claude -p` per turn
costs ~5.3 s (measured 2026-09-28). Real work is not done here: the model ends a reply with
[[ASK: task]] to hand the task to Hermes, or [[HANGUP]] to end the call.
"""
import asyncio
import json
import os
import re
from pathlib import Path

SYSTEM = """You are Jarvis, the owner's engineering agent, on a live Telegram voice call with them.
Everything you write is read aloud: short plain sentences, no markdown, lists, emojis or URLs. Terse and direct, never "sir".
You have two read-only tools over their note vault: search_vault(query) and read_note(path). Use them directly,
with no filler line, for quick factual lookups -- project status, decisions, who the owner is, what's in their notes. They are fast;
answer from what they return in at most three short spoken sentences, and say if the vault has nothing on it.
You cannot see code, repos, running commands or the web yourself. For anything that needs those (not just notes), say one
short line such as "Checking." and end your reply with [[ASK: <self-contained task for the agent>]].
When a message starts with [RESULT], it is the agent's answer: tell them the gist in at most three short spoken sentences.
When the owner says bye or wants to stop, say a short goodbye and end with [[HANGUP]]. Never invent project facts."""

OWNER = os.environ.get("JARVIS_OWNER_NAME", "").strip()
if OWNER:
    SYSTEM += f"\nThe owner's name is {OWNER}."

# passed inline (--mcp-config takes a JSON string) so no file holds an absolute checkout path
MCP_CONFIG = json.dumps({"mcpServers": {"vault": {"command": "python3", "args": [str(Path(__file__).parent / "vault_mcp.py")]}}})
CMD = ["claude", "-p", "--model", "haiku", "--input-format", "stream-json", "--output-format", "stream-json",
       "--verbose", "--include-partial-messages", "--tools", "", "--strict-mcp-config", "--setting-sources", "",
       "--mcp-config", MCP_CONFIG, "--allowedTools", "mcp__vault__search_vault,mcp__vault__read_note"]

ASK = re.compile(r"\[\[ASK:\s*(.*?)\]\]", re.S)
HANGUP = "[[HANGUP]]"
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def split_markers(text):
    """-> (spoken text, ask task or None, hangup?)"""
    m = ASK.search(text)
    task = m.group(1).strip() if m else None
    spoken = ASK.sub("", text).replace(HANGUP, "")
    return re.sub(r"\[\[.*$", "", spoken, flags=re.S).strip(), task, HANGUP in text


class Brain:
    def __init__(self, cmd=None, system=SYSTEM):
        self.cmd = (cmd or CMD) + ["--system-prompt", system]
        self.proc = None

    async def start(self):
        # Haiku 4.5 thinks by default (~1-1.5 s of silent "thinking" tokens before any reply);
        # dead weight on a live call where replies are short and terse anyway. Measured
        # 2026-10-04: median first-word latency 2.48/1.81/1.09s -> 1.49/0.72/0.75s with this off.
        env = dict(os.environ, MAX_THINKING_TOKENS="0")
        self.proc = await asyncio.create_subprocess_exec(
            *self.cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=1 << 22, env=env)

    async def close(self):
        if self.proc and self.proc.returncode is None:
            self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()

    async def turn(self, text):
        """Yield ("say", sentence) as sentences complete, then ("ask", task) and/or ("hangup", None)."""
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()
        pending = ""
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                raise RuntimeError("claude process exited")
            ev = json.loads(line)
            if ev.get("type") == "stream_event":
                delta = ev["event"].get("delta", {})
                if delta.get("type") != "text_delta":
                    continue
                pending += delta["text"]
                if "[[" in pending:  # a marker is coming: hold everything from it until the end
                    continue
                *done, pending = SENTENCE_END.split(pending)
                for s in done:
                    if s.strip():
                        yield "say", s.strip()
            elif ev.get("type") == "result":
                break
        spoken, task, hangup = split_markers(pending)
        if spoken:
            yield "say", spoken
        if task:
            yield "ask", task
        if hangup:
            yield "hangup", None
