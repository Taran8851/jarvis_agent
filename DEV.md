# DEV.md — how to work in this repo

Drop this file into a project root and point your agent at it. It says how to work, not what
to build; the product rules belong in a separate file.

**To make an agent load it automatically:** name it `AGENTS.md` or `CLAUDE.md`, or add one line
to that file: `Read DEV.md before doing anything.` A file no tool reads changes nothing.

Lines marked **[SET]** are the ones to edit per project. Everything else travels as-is.

---

## 0. The project in three lines

**[SET]** Fill these in; an agent that has to guess will guess wrong.

- **What this is:** <one sentence>
- **Authority files:** <the files that win an argument, e.g. `SPEC.md`, `reference/`>
- **How to run it:** <the exact command>

---

## 1. Declare before you build

Before writing code for any feature, section or fix — **including small ones** — state:

1. **Understanding** — what you think you're building, in your own words. Cite the file and
   line where you read anything you're relying on.
2. **Plan** — the specific things you'll produce, in order.
3. **Dependencies** — every library, font, asset or service you intend to add: name, exact
   version, licence, size, and **what it replaces that would otherwise be hand-written**. A
   dependency that only saves typing doesn't qualify.
4. **Not doing** — what you're deliberately leaving out, and anything in the brief you think is
   wrong or unbuildable.

Then **stop and wait**. Don't build until it's approved.

This isn't ceremony. It surfaces the misunderstanding while it costs one message instead of an
afternoon, and it's where "actually, that's not what I meant" belongs.

**Exception:** a question is not a build. Answering, reading, measuring and explaining need no
gate. Use judgement for a one-line typo fix; use the gate the moment behaviour changes.

## 2. Don't invent what isn't specified

If the brief asks for something the code can't support, **say so and stop**. Don't build the
gap and don't guess the missing behaviour. Raising a conflict is always the right move and is
never unwelcome.

If a decision is genuinely the user's — a product call, a trade-off with no technical winner —
ask, having first done everything that doesn't depend on the answer.

## 3. Dependencies: default zero

**[SET]** Approved list: <e.g. nothing; or React + Vite; or stdlib only>

Anything else needs the declaration above and an explicit yes. The test: could it be ripped out
and hand-written in an afternoon? If not, it's too much leverage over a small codebase.

Lockfiles stay committed — they pin exact versions so builds are reproducible.

## 4. Verify, don't assert

**Never report something works because it should.** Run it, and say what you ran.

- Changed a build? Build it.
- Changed a path? Grep for every reference, then run the thing.
- Changed UI? Open it and look — screenshot it if you can't.
- Wrote a benchmark? Sanity-check it on cases where you already know the answer, before
  believing any number it prints.
- Claiming a bug is fixed? Show the output that proves it.

If you couldn't verify something, **say which part is unverified**. That sentence is worth more
than confident prose.

## 5. Report faithfully

- Tests fail → say so, with the output. Step skipped → say which. Done and verified → say it
  plainly, without hedging.
- State the limits with the result: sample size, what the numbers can't tell you, what you
  assumed. A result with a caveat is usable; a result without one is a trap.
- No sycophancy and no drama. "It works" or "it doesn't, here's why".
- Correct a mistake in one sentence and move on. No ruminating.

## 6. Simplest thing that could work, measured first

Before reaching for a model, a framework or a service, write the dumb baseline — the regex, the
lookup table, the ten-line rule — and **measure both**. Often the baseline wins, and then the
complicated thing is a downgrade you can now refuse with a number rather than a hunch.

This applies to architecture too: the boring solution that fits the existing shape beats the
elegant one that needs three new concepts.

## 7. Changes are surgical

- Change what was asked. Don't widen scope, don't quietly narrow it, don't refactor next to the
  fix because you were passing.
- Match the surrounding code: its naming, its comment density, its idiom. New code should be
  hard to spot in a diff.
- Found a real problem outside scope? Say it in a sentence and keep going. Don't fix it silently.
- Finish the whole task. If part is blocked, do the rest and say exactly what you left and why.

## 8. Before anything irreversible

Destructive or outward-facing work — deleting, overwriting, force-pushing, rewriting history,
deploying, posting, sending:

1. **Look at the target first.** Read the file before overwriting; list before deleting.
2. **Back it up** when it isn't cheaply recoverable. A tarball or a `git bundle` costs seconds.
3. **Confirm** unless the user already told you to go ahead. Approval for one action isn't
   approval for the next one.
4. **Scan before publishing:** no keys, tokens, `.env`, `.pem`, dumps or private data — and
   check the **history**, not just the working tree.

Never read a credential you don't need. If a file holds one, work around it and say so.

## 9. Git

- Commit only when asked. One concern per commit; a mixed commit can't be reverted.
- Message: what changed and **why**, in plain words. The diff already says how.
- Separate the move from the fix, the rename from the behaviour change. Reviewers and
  `git bisect` both need this.
- Never rewrite shared history without explicit agreement. On an unpushed or empty remote,
  rewriting is cheap and fine — say so and back it up first.
- **[SET]** Attribution: <e.g. commit as the user, no co-author trailers; or default>

## 10. Files and structure

- **The file that ships is the file you edit.** No generated output shadowing a source file.
- One place for shared values (design tokens, config, constants). Changing a colour is one line
  in one file.
- Semantic names over clever ones. A reader should find the thing by guessing its name.
- **Comment the non-obvious only** — why this cutoff, why this order, why not the obvious way.
  Never narrate what the code already says.
- Move a file → update every reference, including scripts, configs, CI and docs. Then run it.

## 11. Notes and docs

- Findings that took real work — a benchmark, an investigation, a rejected approach and its
  numbers — belong in a file, not in chat scrollback. Include what you'd need to pick it up
  cold: the numbers, the caveats, where the artifacts are, how to re-run it.
- Date them. Say which parts are current and which are historical.
- **[SET]** Where notes live, and whether they're committed: <e.g. `docs/`, local only>

## 12. Tools and workspace

- Temporary work goes in a scratch directory, never in the repo.
- Keep an eye on what you install: a package can drop gigabytes outside its folder. Check, and
  clean up when done.
- Don't touch the user's other projects, credentials or machine config. If the task can't be
  finished with what you were given, say what's missing instead of reaching further.

## 13. Memory: the vault

Long-term memory lives in the Obsidian vault at `OBSIDIAN_VAULT_PATH` (plain markdown files).
It is shared by every project and every session.

- **Start of session:** read `_index.md`, then `dev/DEV.md`. Pull anything else by search: read a
  note's TL;DR first, and open the full note only if you need it.
- **Layout:** `dev/` rules · `memory/` user profile and feedback · `knowledge/concepts/` ideas
  shared across projects · `projects/<name>/` overview, papers, checkpoints, references ·
  `logs/<date>.md` one per session · `templates/` starting points for new notes.
- **Every note:** frontmatter, a TL;DR of at most 3 lines at the top, and `[[wikilinks]]` to
  concepts. The links are what make the graph useful; an unlinked note is lost.
- **End of session:** append to `logs/<date>.md` (done, decided, next, artifacts) and update the
  project's `overview.md` if its status or decisions changed.
- **Date anything that goes stale** (provider policies, versions, prices) and give its source.

---

## The short version

Say what you're about to do. Build only what was asked. Prove it works. Say what you couldn't
prove. Back up before you break things. Prefer the boring solution, with a measurement.
