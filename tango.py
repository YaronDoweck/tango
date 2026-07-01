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

AGENT_MODELS = {
    "claude": None,  # None = let the CLI use its own default
    "codex": None,
}

AGENT_SKILLS = {
    "claude": {},  # keys: tag (e.g. "plan_write") or role ("writer"/"reviewer")
    "codex":  {},
}

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["APPROVED", "CHANGES_NEEDED"]},
        "summary": {"type": "string"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                    "description": {"type": "string"},
                },
                "required": ["severity", "description"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "summary", "issues"],
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

PLAN_WRITE_PROMPT = """\
You are implementing a software feature that has been broken into phases.

Spec file (may contain all phases): {phase_spec}
Current phase: {phase}

Read the spec. If it covers multiple phases, focus ONLY on phase '{phase}'.
Write a detailed implementation plan for ONLY this phase (do not write any
code yet). Cover: approach, files/modules touched, data model or API
changes, edge cases, and an ordered list of implementation steps.

Save the plan to: {plan_path}
Do not modify any other files.
"""

PLAN_REVIEW_PROMPT = """\
You are reviewing an implementation plan before any code is written.

Spec file (may contain all phases): {phase_spec}
Current phase: {phase}
Plan to review: {plan_path}

Read both files. If the spec covers multiple phases, evaluate the plan only
against the requirements for phase '{phase}'. Check the plan fully satisfies
those requirements, is technically sound, doesn't miss edge cases, and
doesn't introduce unnecessary scope.

Respond with ONLY a JSON object, no other text, no markdown fences,
matching this shape:
{{"verdict": "APPROVED" or "CHANGES_NEEDED", "summary": "<one paragraph>", "issues": [{{"severity": "HIGH"|"MEDIUM"|"LOW", "description": "<specific issue>"}}]}}
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
{{"verdict": "APPROVED" or "CHANGES_NEEDED", "summary": "<one paragraph>", "issues": [{{"severity": "HIGH"|"MEDIUM"|"LOW", "description": "<specific issue>"}}]}}
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


def load_prompts_config(script_dir, config_override=None):
    config_path = pathlib.Path(config_override).resolve() if config_override else pathlib.Path(script_dir) / "tango-prompts.toml"
    if not config_path.exists():
        if config_override:
            sys.exit(f"Config file not found: {config_path}")
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
    agents = cfg.get("agents", {})
    for agent_name in ("claude", "codex"):
        section = agents.get(agent_name, {})
        if section.get("model"):
            AGENT_MODELS[agent_name] = section["model"]
        for key, val in section.items():
            if key.endswith("_skills") and isinstance(val, list) and val:
                AGENT_SKILLS[agent_name][key[:-len("_skills")]] = val
    loaded = []
    if prompts:
        loaded.append(f"{len(prompts)} prompt(s)")
    if any(AGENT_MODELS.values()):
        loaded.append("agent models: " + ", ".join(f"{k}={v}" for k, v in AGENT_MODELS.items() if v))
    for agent_name, tags in AGENT_SKILLS.items():
        for tag, skills in tags.items():
            loaded.append(f"{agent_name} {tag} skills: {skills}")
    if loaded:
        print(f"[tango] loaded from {config_path.name}: {', '.join(loaded)}")


# ---------------------------------------------------------------------------
# Agent runners
# ---------------------------------------------------------------------------


def run_claude(prompt, cwd, allowed_tools, permission_mode="acceptEdits"):
    cmd = ["claude", "-p", prompt,
           "--output-format", "json",
           "--allowedTools", allowed_tools,
           "--permission-mode", permission_mode]
    if AGENT_MODELS["claude"]:
        cmd += ["--model", AGENT_MODELS["claude"]]
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}\nSTDERR:\n{proc.stderr[-2000:]}")
    data = json.loads(proc.stdout)
    return data.get("result", "")


def run_codex(prompt, cwd, sandbox="workspace-write", schema_path=None, out_path=None):
    cmd = ["codex", "exec", prompt, "--json", "--sandbox", sandbox, "--ask-for-approval", "never"]
    if AGENT_MODELS["codex"]:
        cmd += ["--model", AGENT_MODELS["codex"]]
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


# ---------------------------------------------------------------------------
# Dry-run stubs  (activated by --dry-run; no agents called, no commits made)
# ---------------------------------------------------------------------------

DRY_RUN = False
_dry_counters = {}       # (phase, tag) -> call count
_dry_committed = set()   # phases where fake code-write happened


def _inject_skills(prompt, agent, role, tag):
    m = AGENT_SKILLS.get(agent, {})
    skills = m.get(tag, m.get(role, []))  # tag-specific wins over role fallback
    if not skills:
        return prompt
    return "\n".join(skills) + "\n\n" + prompt

_SEP = "─" * 64


def _dry_inc(phase, tag):
    key = (phase, tag)
    _dry_counters[key] = _dry_counters.get(key, 0) + 1
    return _dry_counters[key]


def _dry_print(label, agent, tag, n, prompt, extra=""):
    print(f"\n{_SEP}")
    print(f"[DRY-RUN] {label}  agent={agent}  tag={tag}  round={n}")
    print(f"\n-- PROMPT --\n{prompt.strip()}")
    if extra:
        print(f"\n{extra}")
    print(f"{_SEP}\n")


# ---------------------------------------------------------------------------
# Agent dispatchers
# ---------------------------------------------------------------------------


def call_writer(agent, prompt, cwd, phase, tag):
    prompt = _inject_skills(prompt, agent, "writer", tag)
    if DRY_RUN:
        n = _dry_inc(phase, tag)
        _dry_print("WRITER", agent, tag, n, prompt)
        response = f"[dry-run {tag} call {n}]"
        log(tag, phase, agent, prompt, response)
        if tag == "plan_write":
            plan_path = cwd / PLANS_DIR_NAME / f"phase-{phase}.md"
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(
                f"# Dry-run plan for phase {phase}\n\n"
                f"Steps:\n1. Do X\n2. Do Y\n3. Do Z\n"
            )
            print(f"[DRY-RUN] wrote fake plan → {plan_path}")
        elif tag == "plan_fix":
            plan_path = cwd / PLANS_DIR_NAME / f"phase-{phase}.md"
            plan_path.write_text(
                f"# Dry-run plan for phase {phase} (fix {n})\n\n"
                f"Updated steps after reviewer feedback.\n"
            )
            print(f"[DRY-RUN] updated fake plan → {plan_path}")
        elif tag in ("code_write", "code_fix"):
            _dry_committed.add(phase)
            print(f"[DRY-RUN] fake commit recorded for phase {phase} (no actual git commit).")
        return response

    if agent == "claude":
        text = run_claude(prompt, cwd, allowed_tools="Read,Edit,Write,Bash,Skill", permission_mode="acceptEdits")
    elif agent == "codex":
        text = run_codex(prompt, cwd, sandbox="workspace-write")
    else:
        sys.exit(f"Unknown writer agent: {agent}")
    log(tag, phase, agent, prompt, text)
    return text


def call_reviewer(agent, prompt, cwd, phase, tag, state_dir):
    prompt = _inject_skills(prompt, agent, "reviewer", tag)
    if DRY_RUN:
        n = _dry_inc(phase, tag)
        approve = n >= 3
        verdict = (
            {"verdict": "APPROVED", "summary": "dry-run auto-approval on round 3", "issues": []}
            if approve else
            {"verdict": "CHANGES_NEEDED", "summary": f"dry-run feedback round {n}",
             "issues": [{"severity": "HIGH", "description": f"issue {n}a: something needs fixing"},
                        {"severity": "LOW",  "description": f"issue {n}b: minor concern"}]}
        )
        _dry_print("REVIEWER", agent, tag, n, prompt,
                   f"-- VERDICT --\n{json.dumps(verdict, indent=2)}")
        log(tag, phase, agent, prompt, json.dumps(verdict))
        return verdict

    if agent == "claude":
        # Read-only + narrow git inspection commands; verify this permission-rule
        # syntax against `claude -p --help` on your installed version.
        allowed = "Read,Bash(git diff *),Bash(git log *),Bash(git show *),Bash(cat *),Skill"
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


def git_advanced(cwd, before_sha, phase=None):
    if DRY_RUN and phase is not None:
        return phase in _dry_committed
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


def resolve_plan(phase, plan_override, plans_dir, cwd):
    if plan_override:
        p = pathlib.Path(plan_override).resolve()
        if not p.exists():
            sys.exit(f"Plan file not found: {p}")
        return p
    conventional = plans_dir / f"phase-{phase}.md"
    if conventional.exists():
        return conventional
    # Auto-detect: scan git status for untracked/modified .md files
    r = subprocess.run(["git", "status", "--short", "--porcelain"],
                       cwd=cwd, capture_output=True, text=True, check=True)
    candidates = [cwd / line[3:].strip() for line in r.stdout.splitlines()
                  if line[3:].strip().endswith(".md")]
    if len(candidates) == 1:
        print(f"[tango] auto-detected plan file: {candidates[0]}")
        return candidates[0]
    if len(candidates) > 1:
        sys.exit(f"Multiple modified .md files found; specify --plan <path>:\n  " +
                 "\n  ".join(str(c) for c in candidates))
    sys.exit(f"No plan file at {conventional} and no modified .md files found in git status.")


def resolve_spec(phase, spec_override, phases_dir):
    if spec_override:
        p = pathlib.Path(spec_override).resolve()
        if not p.exists():
            if DRY_RUN:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(f"[dry-run] Spec for phase {phase}\n")
                print(f"[DRY-RUN] created fake spec at {p}")
            else:
                sys.exit(f"Spec file not found: {p}")
        return p
    spec = phases_dir / f"phase-{phase}.md"
    if not spec.exists():
        if DRY_RUN:
            spec.parent.mkdir(parents=True, exist_ok=True)
            spec.write_text(f"[dry-run] Spec for phase {phase}\n")
            print(f"[DRY-RUN] created fake spec at {spec}")
        else:
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


def run_planning(phase, writer, reviewer, cwd, max_iters, phases_dir, plans_dir, state_dir, spec_override=None, plan_override=None):
    state = load_state(state_dir, phase)

    if state.get("plan_approved"):
        print(f"[phase {phase}] plan already approved, skipping.")
        return True

    phase_spec = resolve_spec(phase, spec_override, phases_dir)
    # Writer always saves to the conventional path; resolve_plan is used after write.
    write_plan_path = plans_dir / f"phase-{phase}.md"
    write_plan_path.parent.mkdir(parents=True, exist_ok=True)

    if write_plan_path.exists():
        print(f"[phase {phase}] plan file exists, skipping write step.")
    else:
        call_writer(
            writer,
            PLAN_WRITE_PROMPT.format(phase_spec=phase_spec, plan_path=write_plan_path, phase=phase),
            cwd, phase, "plan_write",
        )

    # After write, resolve actual plan path (may differ if writer saved elsewhere)
    plan_path = resolve_plan(phase, plan_override, plans_dir, cwd)

    fix_iter = state.get("plan_fix_iter", 0)
    remaining = max_iters - fix_iter

    for i in range(1, remaining + 1):
        abs_iter = fix_iter + i
        verdict = call_reviewer(
            reviewer,
            PLAN_REVIEW_PROMPT.format(phase_spec=phase_spec, plan_path=plan_path, phase=phase),
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
        feedback = "\n".join(f"- [{x['severity']}] {x['description']}" for x in verdict["issues"]) or verdict["summary"]
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


def run_implementing(phase, writer, reviewer, cwd, max_iters, phases_dir, plans_dir, state_dir, spec_override=None, plan_override=None):
    state = load_state(state_dir, phase)

    if state.get("code_approved"):
        print(f"[phase {phase}] code already approved, skipping.")
        return True

    phase_spec = resolve_spec(phase, spec_override, phases_dir)
    plan_path = resolve_plan(phase, plan_override, plans_dir, cwd)

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

    if git_advanced(cwd, base_sha, phase=phase):
        print(f"[phase {phase}] commits exist past base_sha, skipping write step.")
    else:
        call_writer(
            writer,
            CODE_WRITE_PROMPT.format(plan_path=plan_path, phase_spec=phase_spec, phase=phase),
            cwd, phase, "code_write",
        )
        if not git_advanced(cwd, base_sha, phase=phase):
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
        feedback = "\n".join(f"- [{x['severity']}] {x['description']}" for x in verdict["issues"]) or verdict["summary"]
        pre_fix_sha = get_head(cwd)
        call_writer(
            writer,
            CODE_FIX_PROMPT.format(feedback=feedback, phase=phase),
            cwd, phase, "code_fix",
        )
        if not git_advanced(cwd, pre_fix_sha, phase=phase):
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

    global DRY_RUN
    script_dir = pathlib.Path(__file__).parent

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", choices=["plan", "implement", "phase"])
    parser.add_argument("--phase", required=True,
                        help="Phase identifier (used for naming, state, and commit-message grep).")
    parser.add_argument("--spec", default=None,
                        help="Path to spec file. Overrides the default phases/phase-<N>.md lookup.")
    parser.add_argument("--plan", default=None,
                        help="Path to plan file for the reviewer. Auto-detected from git status if omitted.")
    parser.add_argument("--writer", required=True, choices=["claude", "codex"])
    parser.add_argument("--reviewer", required=True, choices=["claude", "codex"])
    parser.add_argument("--repo-dir", default=os.environ.get("TANGO_REPO_DIR", "."))
    parser.add_argument("--max-iters", type=int, default=5)
    parser.add_argument("--config", default=None, help="Path to config TOML file (default: tango-prompts.toml next to script).")
    parser.add_argument("--claude-model", default=None, help="Model for claude agent (e.g. claude-opus-4-8).")
    parser.add_argument("--codex-model", default=None, help="Model for codex agent (e.g. o4-mini).")
    parser.add_argument("--reset", action="store_true", help="Clear saved state for this phase and start fresh.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate all steps without calling agents or committing. Shows prompts and state.")
    args = parser.parse_args()
    load_prompts_config(script_dir, config_override=args.config)

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

    if args.claude_model:
        AGENT_MODELS["claude"] = args.claude_model
    if args.codex_model:
        AGENT_MODELS["codex"] = args.codex_model
    if any(AGENT_MODELS.values()):
        for k, v in AGENT_MODELS.items():
            if v:
                print(f"[tango] {k} model: {v}")

    if args.dry_run:
        DRY_RUN = True
        print("[tango] DRY-RUN mode: no agents called, no commits made.")

    if args.reset:
        reset_state(state_dir, args.phase)

    if args.writer == args.reviewer:
        print("Note: --writer and --reviewer are the same agent -- it'll be reviewing its own work.")

    ok = True
    if args.step in ("plan", "phase"):
        ok = run_planning(args.phase, args.writer, args.reviewer, cwd, args.max_iters,
                           phases_dir, plans_dir, state_dir, spec_override=args.spec, plan_override=args.plan)
        if not ok:
            sys.exit(1)
    if args.step in ("implement", "phase"):
        ok = run_implementing(args.phase, args.writer, args.reviewer, cwd, args.max_iters,
                               phases_dir, plans_dir, state_dir, spec_override=args.spec, plan_override=args.plan)
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
