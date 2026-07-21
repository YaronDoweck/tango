# Tango Agent

**Two-agent code review loops for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`claude`) and the [Codex CLI](https://github.com/openai/codex) (`codex`).**

Tango runs an adversarial coding workflow: one agent writes the plan and
implementation, while the other reviews in a read-only sandbox and must return a
machine-parseable verdict. Roles can be swapped per phase, so Claude can write
while Codex reviews, or the other way around.

It takes two to tango.

```text
plan:      writer drafts plan  →  reviewer approves or requests changes  →  repeat
implement: writer codes+commits →  reviewer approves or requests changes  →  repeat
```

Start with `--dry-run`: it shows the full workflow without calling agents,
editing files, or creating commits.

## Why

Letting a single agent write code and declare it done is how you ship confident
nonsense. A second agent that reviews *blind* — with no memory of being asked
nicely — is a much stronger signal. Tango wires that adversarial loop into a
repeatable CLI:

- **Cross-model review.** Have Claude plan and Codex review, or vice versa. Two
  different models catch different classes of mistakes.
- **Stateless and reproducible.** Every agent call rebuilds its context from
  files on disk and `git diff`/`git log`, never from a live conversation. The
  whole run reconstructs from the logs.
- **Structured verdicts.** Reviewers return JSON (`APPROVED` / `CHANGES_NEEDED`
  plus severity-tagged issues), not prose you have to eyeball.
- **Git is the source of truth.** The base SHA is pinned before the first
  commit, so the reviewer always sees the full diff for the phase.
- **Fail loud.** A phase stuck after `--max-iters` rounds exits non-zero and
  escalates to you — it doesn't retry harder or paper over the disagreement.

## Quickstart

Clone Tango:

```bash
git clone https://github.com/YaronDoweck/tango.git
cd tango
chmod +x tango.py
```

In the repo you want Tango to work on, create a phase spec:

```bash
mkdir -p phases
cat > phases/phase-1.md <<'EOF'
Add a small health-check command to the CLI.

Requirements:
- Add a command named `health`
- It should print `ok`
- Add or update tests if the project has tests
EOF
```

Then run Tango against that repo:

```bash
python /path/to/tango/tango.py phase \
  --phase 1 \
  --writer claude \
  --reviewer codex \
  --repo-dir /path/to/your/repo \
  --dry-run
```

Remove `--dry-run` when the flow looks right.

## Safety note

Tango gives the writer agent edit access to the target repository. For serious
or unattended runs, use a git worktree, disposable clone, or container instead
of your main checkout.

Recommended first real run:

```bash
python tango.py phase --phase 1 --writer claude --reviewer codex --dry-run
```

Review the generated prompts, logs, and planned commands before running without
`--dry-run`.

## How it works

You break your work into **phases** and write a short spec for each
(`phases/phase-1.md`, `phases/phase-2.md`, …). For each phase Tango runs two
loops:

1. **Plan** — the writer drafts an implementation plan into `plans/phase-N.md`;
   the reviewer approves it or returns issues; the writer revises. Repeat until
   approved or `--max-iters` is hit.
2. **Implement** — the writer codes the plan and commits; the reviewer inspects
   the diff and approves or returns issues; the writer fixes and commits again.
   Same loop.

Progress is checkpointed to `.agent-workflow/` so an interrupted run resumes
where it left off, and every prompt/response pair is logged.

### Reviewer verdicts

Reviewers must return structured JSON:

```json
{
  "verdict": "CHANGES_NEEDED",
  "issues": [
    {
      "severity": "high",
      "summary": "The implementation updates the CLI but does not add a test.",
      "recommendation": "Add or update a test that covers the new command."
    }
  ]
}
```

When the reviewer returns `APPROVED`, Tango moves to the next step. When it
returns `CHANGES_NEEDED`, the writer gets the issues and tries again until
approval or `--max-iters` is reached.

## Requirements

- **Python 3.11+** (uses the stdlib `tomllib` for optional config). On 3.8–3.10,
  install the `tomli` backport only if you use a `--config` file:
  `pip install tomli`. With no config file, Tango has **zero** third-party
  dependencies — it's pure standard library, so there is no `requirements.txt`.
- The **`claude`** CLI ([install](https://docs.anthropic.com/en/docs/claude-code/setup)),
  installed and authenticated.
- The **`codex`** CLI ([install](https://github.com/openai/codex)),
  installed and authenticated.
- **git** — Tango runs against a git repository and uses commit ranges to track
  what changed.

Tango does not handle agent login; authenticate both CLIs first (`claude` once
interactively, `codex login`).

## Install

```bash
git clone https://github.com/YaronDoweck/tango.git
cd tango
```

`tango.py` is a single self-contained script — nothing to build or `pip install`.
Optionally put it on your `PATH`:

```bash
chmod +x tango.py
ln -s "$(pwd)/tango.py" /usr/local/bin/tango   # or anywhere on your PATH
```

## Setup

`tango.py` doesn't need to live inside your project. Point it at the target repo
with `--repo-dir` or the `TANGO_REPO_DIR` env var (`--repo-dir` wins if both are
set):

```bash
# per-call
python tango.py plan --phase 3 --writer claude --reviewer codex --repo-dir ~/code/myproject

# or set once per shell (e.g. in ~/.bashrc)
export TANGO_REPO_DIR=~/code/myproject
python tango.py plan --phase 3 --writer claude --reviewer codex
```

`phases/`, `plans/`, and `.agent-workflow/` are created **inside that repo dir**,
and every agent command (`git`, file edits) runs there too. Before your first
run:

1. Write a spec file per phase: `phases/phase-1.md`, `phases/phase-2.md`, … —
   whatever describes what that phase needs to do.
2. Add `.agent-workflow/` (logs + scratch state) to that repo's `.gitignore`.

`plans/` and `.agent-workflow/` are created automatically.

## Usage

```bash
# Plan phase 3 — Claude writes, Codex reviews
python tango.py plan --phase 3 --writer claude --reviewer codex

# Implement phase 3 — roles swapped
python tango.py implement --phase 3 --writer codex --reviewer claude

# Both steps in one call (same roles for both), cap at 5 review rounds
python tango.py phase --phase 3 --writer claude --reviewer codex --max-iters 5
```

### Commands

| Command     | What it does                                             |
|-------------|----------------------------------------------------------|
| `plan`      | Run the plan → review loop only.                         |
| `implement` | Run the code → review loop only (needs an approved plan).|
| `phase`     | Run both loops back to back.                             |

### Common flags

| Flag                       | Purpose                                                        |
|----------------------------|---------------------------------------------------------------|
| `--phase N`                | Phase identifier (used for file names, state, commit grep). **Required.** |
| `--writer claude\|codex`   | Agent that plans and codes. Required unless `workflow.writer` is set in config. |
| `--reviewer claude\|codex` | Agent that reviews. Required unless `workflow.reviewer` is set in config. |
| `--repo-dir PATH`          | Target repository (or set `TANGO_REPO_DIR`). Default `.`.     |
| `--max-iters N`            | Max review rounds before escalating. Default `5`.             |
| `--spec PATH`              | Override the default `phases/phase-N.md` spec lookup.         |
| `--plan PATH`              | Plan file for the reviewer. Auto-detected if omitted.         |
| `--config PATH`            | Prompt/model overrides (TOML). See [Configuration](#configuration). |
| `--claude-model` / `--codex-model` | Per-agent model override.                             |
| `--claude-effort` / `--codex-effort` | Per-agent reasoning effort.                        |
| `--no-stream`              | Disable real-time agent output (streaming is on by default).  |
| `--dry-run`                | Simulate every step — no agents called, no commits made.     |
| `--reset`                  | Clear saved state for this phase and start fresh.            |
| `--resume-claude` / `--resume-codex` | Resume a prior agent session by ID.               |

Run `python tango.py --help` for the full list.

The exit code is non-zero if a phase gets stuck after `--max-iters` rounds
without approval — treat that as "stop and look," not "retry harder." You can
wire it into a wrapper loop over your phase list, but watch the first few phases
run manually before letting it walk through all of them unattended.

Use **`--dry-run`** when introducing Tango to a new repo: it walks the full loop
with stub prompts and fake verdicts so you can see the flow without spending
tokens or changing files.

## Configuration

All prompts and model settings are optional — built-in defaults work out of the
box. To customize, copy an example config and pass it with `--config`:

```bash
cp examples/tango-prompts.toml my-prompts.toml
# edit my-prompts.toml as needed
python tango.py plan --phase 1 --writer claude --reviewer codex --config my-prompts.toml
```

The [`examples/`](examples/) directory contains starting points:

- **`tango-prompts.toml`** — full reference for every prompt, model, effort, and
  skill key.
- **`plan-prompts.toml`** — plan-step overrides only.
- **`impl-prompts.toml`** — implementation-step overrides only.

Every key is optional — omit any section or key to keep the built-in default.

## Design choices worth knowing about

- **Every call is stateless.** No session resumption by default — each agent
  call gets its context from files on disk and `git diff`/`git log`, not from a
  running conversation. This keeps every step reproducible from the logs and
  stops the reviewer from being anchored by the implementer's framing. The
  tradeoff is higher token cost per call since context gets rebuilt each time.
  If that matters, `--resume-claude` / `--resume-codex` let you thread sessions
  for cheaper fix-up rounds.
- **Reviewers are sandboxed read-only.** Codex gets `--sandbox read-only`;
  Claude gets a restricted `--allowedTools` (`Read` plus a few `git` inspection
  commands only). The reviewer literally cannot edit the plan or code it's
  judging.
- **Verdicts are forced into JSON**, not parsed from prose. Codex does this
  natively via `--output-schema`; Claude gets a strict prompt instruction plus a
  defensive parser in case it adds stray text.
- **Git is the source of truth for "what changed this phase."** The base SHA is
  captured before the writer's first commit and stays fixed through all fix-up
  rounds, so the reviewer always sees the full diff for the phase.
- **Everything is logged** to `.agent-workflow/logs/` — one timestamped file per
  prompt/response — so when a phase escalates to you, you can see exactly what
  was said and why the reviewer objected.

## Things you'll likely want to tune

- **Flag drift.** Both CLIs ship fast; verify `--allowedTools` syntax and
  `--sandbox` mode names against `claude -p --help` / `codex exec --help` on the
  versions you're running before trusting this unattended.
- **Cost/time caps.** Neither CLI has a built-in per-run spend cap; set a usage
  budget in your provider dashboard before scheduling this, and consider
  `--max-turns` (Claude) if a single call can spiral.
- **Isolation.** For real unattended runs, use a git worktree or container
  rather than your main checkout — a writer running with edit permissions can
  touch any file in the working directory.
- **"Same agent reviews itself"** is allowed but the script warns about it — it
  defeats the point of the loop, so it's really only useful for quick local
  testing.

## License

[MIT](LICENSE) © Yaron Doweck
