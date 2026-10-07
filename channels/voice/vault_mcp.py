"""Read-only MCP server over stdio, giving the call model (brain.py) direct access to the
note vault for quick factual lookups -- no hand-off to Hermes, no new runtime deps
(hand-rolled JSON-RPC over stdio per the MCP spec, stdlib only; the official `mcp` SDK pulls in
starlette/uvicorn/cryptography for transports we don't use).

Two tools, both read-only and confined to the vault root:
  search_vault(query, limit=5)  -- substring search over every .md file, path + matching lines
  read_note(path)                -- full text of one note (path relative to the vault root)

Run standalone for a protocol smoke test: `python vault_mcp.py` then paste a JSON-RPC line.
"""
import json
import os
import re
import sys
from pathlib import Path

VAULT = Path(os.environ.get("OBSIDIAN_VAULT_PATH", ""))

TOOLS = [
    {
        "name": "search_vault",
        "description": "Search the owner's note vault for a keyword or phrase. "
                        "Returns matching lines with their file path, for quick factual lookups "
                        "(project status, decisions, who the owner is) without a full agent hand-off.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword or phrase to search for (case-insensitive)."},
                "limit": {"type": "integer", "description": "Max matching lines to return (default 5)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_note",
        "description": "Read one note's full text from the vault, e.g. after search_vault "
                        "points at 'projects/atlas/overview.md'. Path is relative to the vault root.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Note path relative to the vault root."}},
            "required": ["path"],
        },
    },
]


STOPWORDS = {"the", "a", "an", "is", "are", "was", "were", "of", "on", "in", "at", "to", "for",
             "and", "or", "what", "my", "me", "do", "i", "project", "state", "about", "with"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 2 and w not in STOPWORDS}


def _safe_path(rel: str) -> Path:
    """Resolve rel against the vault root; raise if it escapes the vault.

    rel must be relative: pathlib silently drops the left side of a join when the right side
    is absolute (Path("/vault") / "/etc/passwd" == Path("/etc/passwd")), so an absolute-looking
    input has to be rejected before it ever reaches the join, not caught after.
    """
    if os.path.isabs(rel) or rel.startswith(("~", "/")):
        raise ValueError(f"path must be relative to the vault root: {rel}")
    p = (VAULT / rel).resolve()
    root = VAULT.resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"path escapes the vault: {rel}")
    return p


def search_vault(query: str, limit: int = 6) -> str:
    """Score every line by how many of the query's significant words it contains (plus a bonus
    if a word is in the file's own path, e.g. "atlas" matching projects/atlas/overview.md),
    not a literal substring match -- a multi-word question rarely appears verbatim in a note.
    Falls back to a plain substring scan only if word-scoring finds nothing, so a query that's
    actually one exact phrase (a name, a path) still works.
    """
    if not VAULT.is_dir():
        return "vault not found (OBSIDIAN_VAULT_PATH unset or wrong on this box)"
    qwords = _words(query)
    scored = []  # (score, path, line_no, line_text)
    if qwords:
        for f in sorted(VAULT.rglob("*.md")):
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            rel = str(f.relative_to(VAULT))
            path_bonus = len(qwords & _words(rel))
            for i, line in enumerate(text.splitlines(), 1):
                score = len(qwords & _words(line)) + path_bonus
                if score:
                    scored.append((score, rel, i, line.strip()))
        scored.sort(key=lambda x: -x[0])
    if not scored:
        q = query.lower()
        for f in sorted(VAULT.rglob("*.md")):
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            rel = str(f.relative_to(VAULT))
            for i, line in enumerate(text.splitlines(), 1):
                if q in line.lower():
                    scored.append((1, rel, i, line.strip()))
    if not scored:
        return f"no matches for {query!r}"
    # A couple of lines of context around each hit, not just the bare matching line: the model
    # was spending an extra read_note round trip per turn just to get the sentence finished.
    lines_cache = {}
    out, seen_files = [], set()
    for score, rel, i, line in scored:
        if rel not in lines_cache:
            lines_cache[rel] = (VAULT / rel).read_text(errors="replace").splitlines()
        ctx = lines_cache[rel]
        snippet = " / ".join(s.strip() for s in ctx[max(0, i - 1):i + 1] if s.strip())
        out.append(f"{rel}:{i}: {snippet}")
        seen_files.add(rel)
        if len(out) >= limit:
            break
    hint = "" if len(out) < limit else f"\n(more in: {', '.join(sorted(seen_files))} -- read_note the relevant one for full context)"
    return "\n".join(out) + hint


def _resolve_note(path: str) -> Path | None:
    """Direct relative path, then the same with .md appended, then a vault-wide search for any
    note whose path (without extension) ends with it -- covers wikilink-style targets from
    search_vault's own hits (e.g. "jarvis/overview", no extension, maps to
    projects/jarvis/overview.md) without a failed round trip first.
    """
    p = _safe_path(path)
    if p.is_file() and p.suffix == ".md":
        return p
    p2 = _safe_path(path + ".md") if not path.endswith(".md") else None
    if p2 and p2.is_file():
        return p2
    target = path[:-3] if path.endswith(".md") else path
    target = target.strip("/").lower()
    for f in sorted(VAULT.rglob("*.md")):
        stem = str(f.relative_to(VAULT))[:-3].lower()
        if stem == target or stem.endswith("/" + target):
            return f
    return None


def read_note(path: str) -> str:
    try:
        p = _resolve_note(path)
    except ValueError as e:
        return f"error: {e}"
    if p is None:
        return f"error: no such note: {path}"
    rel = str(p.relative_to(VAULT))
    note = f"(resolved to {rel})\n" if rel != path else ""
    return note + p.read_text(errors="replace")


def call_tool(name: str, args: dict) -> str:
    if name == "search_vault":
        return search_vault(args.get("query", ""), int(args.get("limit", 5)))
    if name == "read_note":
        return read_note(args.get("path", ""))
    return f"error: unknown tool {name}"


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, mid = req.get("method"), req.get("id")
        if method == "initialize":
            resp = {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": req.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "vault", "version": "0.1.0"},
            }}
        elif method == "notifications/initialized":
            continue  # no response to notifications
        elif method == "tools/list":
            resp = {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
        elif method == "tools/call":
            params = req.get("params", {})
            try:
                text = call_tool(params.get("name"), params.get("arguments", {}) or {})
                resp = {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}], "isError": False}}
            except Exception as e:
                resp = {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}}
        elif mid is None:
            continue  # unknown notification, ignore
        else:
            resp = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
