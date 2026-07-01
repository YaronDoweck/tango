# Tango

Two-agent plan/review/implement/review orchestrator for `claude` (Claude Code)
and `codex` (Codex CLI). Takes two to tango: either agent leads (writes)
or follows (reviews), and roles swap freely per phase or per step. The
loop:

```
plan:      writer drafts plan -> reviewer approves or sends back -> repeat
implement: writer codes + commits -> reviewer approves or sends back -> repeat
```

Either CLI can be `--writer` or `--reviewer`, and you set that per invocation,
so roles can swap freely between phases *or* between the plan and implement
steps of the same phase.

## Setup

1. Keep `tango.py` wherever you like -- it doesn't need to live inside
   your project. Point it at your project with `--repo-dir` or by setting
   the `TANGO_REPO_DIR` env var:

   ```bash
   # per-call
   python /path/to/tango.py plan --phase 3 --writer claude --reviewer codex --repo-dir ~/code/myproject

   # or set once per shell / in your .bashrc
   export TANGO_REPO_DIR=~/code/myproject
   python /path/to/tango.py plan --phase 3 --writer claude --reviewer codex
   ```

   `--repo-dir` always wins if both are set. `phases/`, `plans/`, and
   `.agent-workflow/` are created inside that repo dir, not next to
   `tango.py` -- and agent commands (`git`, file edits) run there too.

2. Optional: put it on your `PATH` so you can just run `tango`:

   ```bash
   chmod +x tango.py
   ln -s "$(pwd)/tango.py" /usr/local/bin/tango   # or anywhere on PATH
   tango plan --phase 3 --writer claude --reviewer codex
   ```

3. Make sure `claude` and `codex` are installed and already authenticated
   (the orchestrator doesn't handle login).
4. For each phase, write a spec file the agents will read:
   `phases/phase-1.md`, `phases/phase-2.md`, etc. -- whatever you already
   have describing what that phase needs to do.
5. `plans/` and `.agent-workflow/` (logs + scratch) are created
   automatically. Add `.agent-workflow/` to `.gitignore`.

## Usage

```bash
# Plan phase 3, Claude writes, Codex reviews
python tango.py plan --phase 3 --writer claude --reviewer codex

# Implement phase 3 with roles swapped
python tango.py implement --phase 3 --writer codex --reviewer claude

# Or both steps in one call (same roles for both)
python tango.py phase --phase 3 --writer claude --reviewer codex --max-iters 5
```

Exit code is non-zero if a phase gets stuck after `--max-iters` rounds
without approval -- treat that as "stop and look," not "retry harder."
Wire it into a wrapper loop over your phase list if you want, but I'd
watch the first few phases run manually before letting it walk through
all of them unattended.

## Design choices worth knowing about

- **Every call is stateless.** No session resumption -- each agent call
  gets its context from files on disk and `git diff`/`git log`, not from
  a running conversation. This keeps every step reproducible from the
  logs and stops the reviewer from being anchored by the implementer's
  framing (a fresh, "blind" review is a stronger signal than one that
  remembers being asked nicely). The tradeoff is slightly higher token
  cost per call since context gets rebuilt each time. If that matters,
  Claude Code supports `--resume <session_id>`/`--continue` and Codex
  supports `codex exec resume --last` -- easy to add back per-writer if
  you want cheaper fix-up rounds.
- **Reviewers are sandboxed read-only.** Codex gets `--sandbox read-only`;
  Claude gets a restricted `--allowedTools` (`Read` plus a few `git`
  inspection commands only). The reviewer literally cannot edit the plan
  or code it's judging.
- **Verdicts are forced into JSON**, not parsed from prose. Codex does
  this natively via `--output-schema`; Claude gets a strict prompt
  instruction plus a defensive parser in case it adds stray text.
- **Git is the source of truth for "what changed this phase."** The base
  SHA is captured before the writer's first commit and stays fixed
  through all fix-up rounds, so the reviewer always sees the full diff
  for the phase, not just the latest patch.
- **Everything is logged** to `.agent-workflow/logs/` -- one file per
  prompt/response, timestamped -- so when a phase gets escalated to you,
  you can see exactly what was said and why the reviewer objected.

## Things you'll likely want to tune

- **Flag drift.** Both CLIs ship fast; verify `--allowedTools` syntax and
  `--sandbox` mode names against `claude -p --help` / `codex exec --help`
  on whatever versions you're running before trusting this unattended.
- **Cost/time caps.** Neither CLI has a built-in per-run spend cap; set a
  usage budget in your provider dashboard before scheduling this, and
  consider `--max-turns` (Claude) if a single call can spiral.
- **Isolation.** If you let this run unattended for real, do it in a git
  worktree or container rather than your main checkout -- a writer that
  runs `--permission-mode acceptEdits` / `--sandbox workspace-write` can
  touch any file in the working directory.
- **"Same agent reviews itself"** is allowed but the script warns about
  it -- it defeats the point of the loop, so it's really only useful for
  quick local testing.
