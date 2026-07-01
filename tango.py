#!/usr/bin/env python3
"""
tango -- a two-agent plan -> review -> implement -> review orchestrator
for Claude Code (`claude`) and Codex CLI (`codex`). Takes two to tango:
either agent can lead (write) or follow (review), and roles can swap
between phases or between the plan/implement steps of the same phase.

Usage:
    python tango.py plan      --phase 3 --writer claude --reviewer codex
    python tango.py implement --phase 3 --writer codex  --reviewer claude
    python tango.py phase     --phase 3 --writer claude --reviewer codex

Prereqs:
    - `claude` and `codex` CLIs installed and authenticated
    - Run from (or point --repo-dir at) a git repository
    - A spec file per phase at phases/phase-<N>.md describing what that
      phase needs to accomplish (you said phases are already defined --
      this just expects them as files the agents can read)

NOTE ON CLI FLAGS: both `claude -p` and `codex exec` evolve quickly.
Before relying on this in anger, sanity-check the flags below against
`claude -p --help` and `codex exec --help` on your installed versions --
in particular --allowedTools permission-rule syntax and --sandbox modes.
"""

import argparse
import datetime
import json
import os
import pathlib
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Config -- edit these to taste
# ---------------------------------------------------------------------------

TIMEOUT_SECONDS = 30 * 60  # kill a single agent call after 30 min
PHASES_DIR_NAME = "phases"  # default dir for spec files (overridden by --spec)
PLANS_DIR_NAME = "plans"  # <repo>/plans/phase-<N>.md -- plan output, agents write these
STATE_DIR_NAME = ".agent-workflow"  # <repo>/.agent-workflow -- logs + scratch files

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["APPROVED", "CHANGES_NEEDED"]},
        "summary": {"type": "string"},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "summary", "issues"],
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

PLAN_WRITE_PROMPT = """\
You are implementing a software feature that has been broken into phases.

Read the specification for this phase from: {phase_spec}

Write a detailed implementation plan for ONLY this phase (do not write any
code yet). Cover: approach, files/modules touched, data model or API
changes, edge cases, and an ordered list of implementation steps.

Save the plan to: {plan_path}
Do not modify any other files.
"""

PLAN_REVIEW_PROMPT = """\
You are reviewing an implementation plan before any code is written.

Phase spec (what the plan must satisfy): {phase_spec}
Plan to review: {plan_path}

Read both files. Check the plan fully satisfies the spec, is technically
sound, doesn't miss edge cases, and doesn't introduce unnecessary scope.

Respond with ONLY a JSON object, no other text, no markdown fences,
matching this shape:
{{"verdict": "APPROVED" or "CHANGES_NEEDED", "summary": "<one paragraph>", "issues": ["<specific issue>", ...]}}
If verdict is APPROVED, issues should be an empty list.
"""

PLAN_FIX_PROMPT = """\
Your implementation plan at {plan_path} was reviewed and needs changes.

Reviewer feedback:
{feedback}

Update the plan file at {plan_path} to address every issue above.
Do not write any code yet.
"""

CODE_WRITE_PROMPT = """\
Implement the plan at {plan_path} (phase spec: {phase_spec}).

Write the code changes for this phase only. When finished, stage and
commit your changes with git, using a commit message prefixed
"phase-{phase}: ". Make one or more commits as appropriate, but do not
touch files outside the scope of this phase's plan.
"""

CODE_REVIEW_PROMPT = """\
Review the code changes for this phase.

Phase spec: {phase_spec}
Plan the code should satisfy: {plan_path}
Review everything introduced between commits {base_sha} and {head_sha}.
Inspect it yourself with `git diff {base_sha}..{head_sha}` and
`git log {base_sha}..{head_sha}`.

Check correctness, missed edge cases from the plan, test coverage, and
adherence to existing code conventions.

Respond with ONLY a JSON object, no other text, no markdown fences,
matching this shape:
{{"verdict": "APPROVED" or "CHANGES_NEEDED", "summary": "<one paragraph>", "issues": ["<specific issue>", ...]}}
If verdict is APPROVED, issues should be an empty list.
"""

CODE_FIX_PROMPT = """\
Your code changes for this phase were reviewed and need changes.

Reviewer feedback:
{feedback}

Fix the issues above, then commit the fix with git using the message
"phase-{phase}: address review feedback".
"""

# ---------------------------------------------------------------------------
# Prompt config loader -- overrides built-in templates from tango-prompts.toml
# ---------------------------------------------------------------------------

_PROMPT_KEYS = {
    "plan_write": "PLAN_WRITE_PROMPT",
    "plan_review": "PLAN_REVIEW_PROMPT",
    "plan_fix": "PLAN_FIX_PROMPT",
    "code_write": "CODE_WRITE_PROMPT",
    "code_review": "CODE_REVIEW_PROMPT",
    "code_fix": "CODE_FIX_PROMPT",
}


def load_prompts_config(script_dir):
    config_path = pathlib.Path(script_dir) / "tango-prompts.toml"
    if not config_path.exists():
        return
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            print(f"Warning: {config_path} found but tomllib unavailable (needs Python 3.11+ or `pip install tomli`). Using built-in prompts.")
            return
    with open(config_path, "rb") as f:
        cfg = tomllib.load(f)
    prompts = cfg.get("prompts", {})
    g = globals()
    for key, var in _PROMPT_KEYS.items():
        if key in prompts:
            g[var] = prompts[key]
    if prompts:
        print(f"[tango] loaded {len(prompts)} prompt(s) from {config_path.name}")


# ---------------------------------------------------------------------------
# Agent runners
# ---------------------------------------------------------------------------


def run_claude(prompt, cwd, allowed_tools, permission_mode="acceptEdits"):
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--allowedTools", allowed_tools,
        "--permission-mode", permission_mode,
    ]
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}\nSTDERR:\n{proc.stderr[-2000:]}")
    data = json.loads(proc.stdout)
    return data.get("result", "")


def run_codex(prompt, cwd, sandbox="workspace-write", schema_path=None, out_path=None):
    cmd = ["codex", "exec", prompt, "--json", "--sandbox", sandbox, "--ask-for-approval", "never"]
    if schema_path:
        cmd += ["--output-schema", str(schema_path)]
    if out_path:
        cmd += ["-o", str(out_path)]
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    if proc.returncode != 0:
        raise RuntimeError(f"codex exited {proc.returncode}\nSTDERR:\n{proc.stderr[-2000:]}")
    if out_path:
        return out_path.read_text()
    # Fallback: pull the agent's final text out of the JSONL event stream.
    text = ""
    for line in proc.stdout.splitlines():
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = str(evt.get("method", ""))
        if "agentMessage" in method:
            params = evt.get("params", {})
            text += params.get("delta") or params.get("message") or ""
    return text


def extract_json(text):
    """Best-effort JSON extraction in case the model adds stray text."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Could not parse a JSON verdict from response:\n{text[:800]}")


def call_writer(agent, prompt, cwd, phase, tag):
    if agent == "claude":
        text = run_claude(prompt, cwd, allowed_tools="Read,Edit,Write,Bash", permission_mode="acceptEdits")
    elif agent == "codex":
        text = run_codex(prompt, cwd, sandbox="workspace-write")
    else:
        sys.exit(f"Unknown writer agent: {agent}")
    log(tag, phase, agent, prompt, text)
    return text


def call_reviewer(agent, prompt, cwd, phase, tag, state_dir):
    if agent == "claude":
        # Read-only + narrow git inspection commands; verify this permission-rule
        # syntax against `claude -p --help` on your installed version.
        allowed = "Read,Bash(git diff *),Bash(git log *),Bash(git show *),Bash(cat *)"
        text = run_claude(prompt, cwd, allowed_tools=allowed, permission_mode="acceptEdits")
        log(tag, phase, agent, prompt, text)
        return extract_json(text)
    elif agent == "codex":
        out_path = state_dir / f"verdict-{tag}-{phase}-{int(time.time() * 1000)}.json"
        schema_path = state_dir / "verdict-schema.json"
        text = run_codex(prompt, cwd, sandbox="read-only", schema_path=schema_path, out_path=out_path)
        log(tag, phase, agent, prompt, text)
        return json.loads(text)
    else:
        sys.exit(f"Unknown reviewer agent: {agent}")


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def _state_path(state_dir, phase):
    return state_dir / f"state-phase-{phase}.json"


def load_state(state_dir, phase):
    p = _state_path(state_dir, phase)
    return json.loads(p.read_text()) if p.exists() else {}


def save_state(state_dir, phase, state):
    _state_path(state_dir, phase).write_text(json.dumps(state, indent=2))


def reset_state(state_dir, phase):
    p = _state_path(state_dir, phase)
    if p.exists():
        p.unlink()
        print(f"[tango] state reset for phase {phase}.")
    else:
        print(f"[tango] no state found for phase {phase}.")


# ---------------------------------------------------------------------------
# Git + logging helpers
# ---------------------------------------------------------------------------


def get_head(cwd):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def git_advanced(cwd, before_sha):
    return get_head(cwd) != before_sha


def find_phase_base_sha(cwd, phase):
    """Parent of the earliest commit whose message starts with 'phase-{phase}:'."""
    r = subprocess.run(
        ["git", "log", "--format=%H", f"--grep=^phase-{phase}:"],
        cwd=cwd, capture_output=True, text=True, check=True,
    )
    shas = [s for s in r.stdout.strip().splitlines() if s]
    if not shas:
        return None
    earliest = shas[-1]
    r2 = subprocess.run(["git", "rev-parse", f"{earliest}^"],
                        cwd=cwd, capture_output=True, text=True)
    return r2.stdout.strip() if r2.returncode == 0 else earliest


def resolve_spec(phase, spec_override, phases_dir):
    if spec_override:
        p = pathlib.Path(spec_override).resolve()
        if not p.exists():
            sys.exit(f"Spec file not found: {p}")
        return p
    spec = phases_dir / f"phase-{phase}.md"
    if not spec.exists():
        sys.exit(f"Missing phase spec: {spec} (write your phase description there, or pass --spec <path>)")
    return spec


LOG_DIR = None  # set in main()


def log(tag, phase, agent, prompt, response):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    fp = LOG_DIR / f"{ts}_phase{phase}_{tag}_{agent}.md"
    fp.write_text(f"# {tag} ({agent}) -- phase {phase}\n\n## Prompt\n\n{prompt}\n\n## Response\n\n{response}\n")


# ---------------------------------------------------------------------------
# The two loops, matching your pseudocode
# ---------------------------------------------------------------------------


def run_planning(phase, writer, reviewer, cwd, max_iters, phases_dir, plans_dir, state_dir, spec_override=None):
    state = load_state(state_dir, phase)

    if state.get("plan_approved"):
        print(f"[phase {phase}] plan already approved, skipping.")
        return True

    phase_spec = resolve_spec(phase, spec_override, phases_dir)
    plan_path = plans_dir / f"phase-{phase}.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)

    if plan_path.exists():
        print(f"[phase {phase}] plan file exists, skipping write step.")
    else:
        call_writer(
            writer,
            PLAN_WRITE_PROMPT.format(phase_spec=phase_spec, plan_path=plan_path),
            cwd, phase, "plan_write",
        )

    fix_iter = state.get("plan_fix_iter", 0)
    remaining = max_iters - fix_iter

    for i in range(1, remaining + 1):
        abs_iter = fix_iter + i
        verdict = call_reviewer(
            reviewer,
            PLAN_REVIEW_PROMPT.format(phase_spec=phase_spec, plan_path=plan_path),
            cwd, phase, "plan_review", state_dir,
        )
        print(f"[phase {phase}] plan review {abs_iter}/{max_iters} ({reviewer}): {verdict['verdict']}")
        if verdict["verdict"] == "APPROVED":
            state["plan_approved"] = True
            save_state(state_dir, phase, state)
            print(f"[phase {phase}] plan approved.")
            return True
        if i == remaining:
            break
        feedback = "\n".join(f"- {x}" for x in verdict["issues"]) or verdict["summary"]
        call_writer(
            writer,
            PLAN_FIX_PROMPT.format(plan_path=plan_path, feedback=feedback),
            cwd, phase, "plan_fix",
        )
        state["plan_fix_iter"] = abs_iter
        save_state(state_dir, phase, state)

    print(f"[phase {phase}] plan NOT approved after {max_iters} rounds -- escalating to you. "
          f"See {LOG_DIR} and {plan_path}.")
    return False


def run_implementing(phase, writer, reviewer, cwd, max_iters, phases_dir, plans_dir, state_dir, spec_override=None):
    state = load_state(state_dir, phase)

    if state.get("code_approved"):
        print(f"[phase {phase}] code already approved, skipping.")
        return True

    phase_spec = resolve_spec(phase, spec_override, phases_dir)
    plan_path = plans_dir / f"phase-{phase}.md"
    if not plan_path.exists():
        sys.exit(f"No plan found at {plan_path}. Run the `plan` step first.")

    # Prefer git-history detection (survives restarts); fall back to stored state; else capture now.
    git_base = find_phase_base_sha(cwd, phase)
    base_sha = git_base or state.get("base_sha")
    if base_sha:
        if "base_sha" not in state or state["base_sha"] != base_sha:
            state["base_sha"] = base_sha
            save_state(state_dir, phase, state)
        src = "git history" if git_base else "saved state"
        print(f"[phase {phase}] base_sha {base_sha[:7]} (from {src}).")
    else:
        base_sha = get_head(cwd)
        state["base_sha"] = base_sha
        save_state(state_dir, phase, state)
        print(f"[phase {phase}] WARNING: no commits with 'phase-{phase}:' prefix found. "
              f"base_sha set to current HEAD ({base_sha[:7]}). "
              f"Ensure writer uses 'phase-{phase}: ' commit prefix so reviews cover all phase work.")

    if git_advanced(cwd, base_sha):
        print(f"[phase {phase}] commits exist past base_sha, skipping write step.")
    else:
        call_writer(
            writer,
            CODE_WRITE_PROMPT.format(plan_path=plan_path, phase_spec=phase_spec, phase=phase),
            cwd, phase, "code_write",
        )
        if not git_advanced(cwd, base_sha):
            sys.exit(f"{writer} did not commit anything for phase {phase}. Check {LOG_DIR} and fix manually.")

    fix_iter = state.get("code_fix_iter", 0)
    remaining = max_iters - fix_iter

    for i in range(1, remaining + 1):
        abs_iter = fix_iter + i
        head_sha = get_head(cwd)
        verdict = call_reviewer(
            reviewer,
            CODE_REVIEW_PROMPT.format(phase_spec=phase_spec, plan_path=plan_path,
                                       base_sha=base_sha, head_sha=head_sha),
            cwd, phase, "code_review", state_dir,
        )
        print(f"[phase {phase}] code review {abs_iter}/{max_iters} ({reviewer}): {verdict['verdict']}")
        if verdict["verdict"] == "APPROVED":
            state["code_approved"] = True
            save_state(state_dir, phase, state)
            print(f"[phase {phase}] code approved. Range: {base_sha[:7]}..{head_sha[:7]}")
            return True
        if i == remaining:
            break
        feedback = "\n".join(f"- {x}" for x in verdict["issues"]) or verdict["summary"]
        pre_fix_sha = get_head(cwd)
        call_writer(
            writer,
            CODE_FIX_PROMPT.format(feedback=feedback, phase=phase),
            cwd, phase, "code_fix",
        )
        if not git_advanced(cwd, pre_fix_sha):
            print(f"[phase {phase}] WARNING: {writer} made no fix commit -- next review will likely repeat.")
        state["code_fix_iter"] = abs_iter
        save_state(state_dir, phase, state)

    print(f"[phase {phase}] code NOT approved after {max_iters} rounds -- escalating to you. "
          f"See {LOG_DIR} and `git log {base_sha}..HEAD`.")
    return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    global LOG_DIR

    script_dir = pathlib.Path(__file__).parent
    load_prompts_config(script_dir)

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", choices=["plan", "implement", "phase"])
    parser.add_argument("--phase", required=True,
                        help="Phase identifier (used for naming, state, and commit-message grep).")
    parser.add_argument("--spec", default=None,
                        help="Path to spec file. Overrides the default phases/phase-<N>.md lookup.")
    parser.add_argument("--writer", required=True, choices=["claude", "codex"])
    parser.add_argument("--reviewer", required=True, choices=["claude", "codex"])
    parser.add_argument("--repo-dir", default=os.environ.get("TANGO_REPO_DIR", "."))
    parser.add_argument("--max-iters", type=int, default=5)
    parser.add_argument("--reset", action="store_true", help="Clear saved state for this phase and start fresh.")
    args = parser.parse_args()

    cwd = pathlib.Path(args.repo_dir).resolve()
    if not (cwd / ".git").exists():
        sys.exit(f"{cwd} doesn't look like a git repo root (no .git dir). "
                  f"Pass --repo-dir or set TANGO_REPO_DIR.")
    print(f"[tango] repo dir: {cwd}")

    phases_dir = cwd / PHASES_DIR_NAME
    plans_dir = cwd / PLANS_DIR_NAME
    state_dir = cwd / STATE_DIR_NAME
    LOG_DIR = state_dir / "logs"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "verdict-schema.json").write_text(json.dumps(VERDICT_SCHEMA, indent=2))

    if args.reset:
        reset_state(state_dir, args.phase)

    if args.writer == args.reviewer:
        print("Note: --writer and --reviewer are the same agent -- it'll be reviewing its own work.")

    ok = True
    if args.step in ("plan", "phase"):
        ok = run_planning(args.phase, args.writer, args.reviewer, cwd, args.max_iters,
                           phases_dir, plans_dir, state_dir, spec_override=args.spec)
        if not ok:
            sys.exit(1)
    if args.step in ("implement", "phase"):
        ok = run_implementing(args.phase, args.writer, args.reviewer, cwd, args.max_iters,
                               phases_dir, plans_dir, state_dir, spec_override=args.spec)
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
