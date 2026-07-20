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
    "claude": {},  # "" key = default; tag key (e.g. "code_write") = override
    "codex": {},
}

AGENT_EFFORTS = {
    "claude": {},  # "" key = default; tag key = override
    "codex": {},
}


def _resolve(mapping, agent, tag):
    """Return tag-specific value if set, else default, else None."""
    m = mapping.get(agent, {})
    return m.get(tag) or m.get("") or None

AGENT_SKILLS = {
    "claude": {},  # keys: tag (e.g. "plan_write") or role ("writer"/"reviewer")
    "codex":  {},
}

RESUME_CLAUDE = None   # --resume-claude <session_id>
RESUME_CODEX = None    # --resume-codex <thread_id>
SESSION_FILE = None    # set in main to {state_dir}/sessions.json

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
commit your changes with git. Make one or more commits as appropriate,
but do not touch files outside the scope of this phase's plan.
"""

CODE_REVIEW_PROMPT = """\
Review the code changes for phase {phase}.

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


def load_prompts_config(script_dir, config_override=None, base_dir=None):
    if config_override:
        p = pathlib.Path(config_override)
        config_path = (pathlib.Path(base_dir) / p).resolve() if (base_dir and not p.is_absolute()) else p.resolve()
    else:
        config_path = pathlib.Path(script_dir) / "tango-prompts.toml"
    if not config_path.exists():
        if config_override:
            sys.exit(f"Config file not found: {config_path}")
        return None, None, [], []
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            print(f"Warning: {config_path} found but tomllib unavailable (needs Python 3.11+ or `pip install tomli`). Using built-in prompts.")
            return None, None, [], []
    with open(config_path, "rb") as f:
        cfg = tomllib.load(f)
    workflow = cfg.get("workflow", {})
    prompts = cfg.get("prompts", {})
    g = globals()
    for key, var in _PROMPT_KEYS.items():
        if key in prompts:
            g[var] = prompts[key]
    agents = cfg.get("agents", {})
    for agent_name in ("claude", "codex"):
        section = agents.get(agent_name, {})
        if section.get("model"):
            AGENT_MODELS[agent_name][""] = section["model"]
        if section.get("effort"):
            AGENT_EFFORTS[agent_name][""] = section["effort"]
        for key, val in section.items():
            if key.endswith("_model") and val:
                AGENT_MODELS[agent_name][key[:-len("_model")]] = val
            elif key.endswith("_effort") and val:
                AGENT_EFFORTS[agent_name][key[:-len("_effort")]] = val
            elif key.endswith("_skills") and isinstance(val, list) and val:
                AGENT_SKILLS[agent_name][key[:-len("_skills")]] = val
    loaded = []
    if prompts:
        loaded.append(f"{len(prompts)} prompt(s)")
    if any(AGENT_MODELS.values()):
        loaded.append("agent models: " + ", ".join(f"{k}={v}" for k, v in AGENT_MODELS.items() if v))
    for agent_name, tags in AGENT_SKILLS.items():
        for tag, skills in tags.items():
            loaded.append(f"{agent_name} {tag} skills: {skills}")
    spec_dirs = workflow.get("spec_dirs", [])
    plan_dirs = workflow.get("plan_dirs", [])
    if spec_dirs:
        loaded.append(f"spec_dirs={spec_dirs}")
    if plan_dirs:
        loaded.append(f"plan_dirs={plan_dirs}")
    if loaded:
        print(f"[tango] loaded from {config_path.name}: {', '.join(loaded)}")
    return workflow.get("writer"), workflow.get("reviewer"), spec_dirs, plan_dirs


# ---------------------------------------------------------------------------
# Agent runners
# ---------------------------------------------------------------------------


def _popen(cmd, cwd):
    return subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True)


def _stream_print(label, text):
    if text:
        print(f"[{label}] {text}", end="", flush=True)


def _save_session(agent, session_id):
    if not SESSION_FILE or not session_id:
        return
    try:
        data = json.loads(SESSION_FILE.read_text()) if SESSION_FILE.exists() else {}
    except (json.JSONDecodeError, OSError):
        data = {}
    data[agent] = session_id
    data["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    SESSION_FILE.write_text(json.dumps(data, indent=2))
    resume_cmd = (f"claude --resume {session_id}" if agent == "claude"
                  else f"codex exec resume {session_id} '<prompt>'")
    print(f"\n[tango] {agent} session_id={session_id}  resume: {resume_cmd}")


def run_claude(prompt, cwd, allowed_tools, permission_mode="bypassPermissions", tag=""):
    fmt = "stream-json" if STREAM else "json"
    cmd = ["claude", "-p", prompt,
           "--output-format", fmt,
           "--allowedTools", allowed_tools,
           "--permission-mode", permission_mode]
    if RESUME_CLAUDE:
        cmd += ["--resume", RESUME_CLAUDE]
    if model := _resolve(AGENT_MODELS, "claude", tag):
        cmd += ["--model", model]
    if effort := _resolve(AGENT_EFFORTS, "claude", tag):
        cmd += ["--effort", effort]

    if STREAM:
        cmd += ["--verbose"]
        proc = _popen(cmd, cwd)
        result = ""
        print()
        for line in proc.stdout:
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("type") == "system" and evt.get("subtype") == "init":
                _save_session("claude", evt.get("session_id"))
            elif evt.get("type") == "assistant":
                for block in evt.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        _stream_print("claude", block["text"])
            elif evt.get("type") == "result":
                result = evt.get("result", "")
        stderr = proc.stderr.read()
        proc.wait()
        print()
        if proc.returncode != 0:
            raise RuntimeError(f"claude exited {proc.returncode}\nSTDERR:\n{stderr[-2000:]}")
        return result

    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}\nSTDERR:\n{proc.stderr[-2000:]}")
    data = json.loads(proc.stdout)
    return data.get("result", "")


def run_codex(prompt, cwd, sandbox="workspace-write", schema_path=None, out_path=None, tag=""):
    if RESUME_CODEX:
        cmd = ["codex", "exec", "resume", RESUME_CODEX, prompt, "--json", "--sandbox", sandbox]
    else:
        cmd = ["codex", "exec", prompt, "--json", "--sandbox", sandbox]
    if sandbox == "read-only":
        # ponytail: unknown exact key — adjust if codex prompts for confirmation.
        cmd += ["-c", "approval_policy=\"never\""]
    else:
        # Note: also bypasses sandbox restriction (known limitation).
        cmd += ["--dangerously-bypass-approvals-and-sandbox"]
    if model := _resolve(AGENT_MODELS, "codex", tag):
        cmd += ["--model", model]
    if effort := _resolve(AGENT_EFFORTS, "codex", tag):
        cmd += ["-c", f"model_reasoning_effort=\"{effort}\""]
    if schema_path:
        cmd += ["--output-schema", str(schema_path)]
    if out_path:
        cmd += ["-o", str(out_path)]

    def _codex_agent_text(evt):
        """Extract agent message text from a codex JSON event."""
        if evt.get("type") == "item.completed":
            item = evt.get("item", {})
            if item.get("type") == "agent_message":
                return item.get("text", "")
        return ""

    def _parse_codex_jsonl(lines):
        text = ""
        for line in lines:
            try:
                text += _codex_agent_text(json.loads(line))
            except json.JSONDecodeError:
                pass
        return text

    if STREAM:
        proc = _popen(cmd, cwd)
        lines = []
        print()
        for line in proc.stdout:
            lines.append(line)
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("type") == "thread.started":
                _save_session("codex", evt.get("thread_id"))
            chunk = _codex_agent_text(evt)
            if chunk:
                _stream_print("codex", chunk)
        stderr = proc.stderr.read()
        proc.wait()
        print()
        if proc.returncode != 0:
            raise RuntimeError(f"codex exited {proc.returncode}\nSTDERR:\n{stderr[-2000:]}")
        if out_path:
            return out_path.read_text()
        return _parse_codex_jsonl(lines)

    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RuntimeError(f"codex exited {proc.returncode}\nSTDERR:\n{proc.stderr[-2000:]}")
    for line in proc.stdout.splitlines():
        try:
            evt = json.loads(line)
            if evt.get("type") == "thread.started":
                _save_session("codex", evt.get("thread_id"))
                break
        except json.JSONDecodeError:
            pass
    if out_path:
        return out_path.read_text()
    return _parse_codex_jsonl(proc.stdout.splitlines())


def extract_json(text):
    """Best-effort JSON extraction in case the model adds stray text."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    end = text.rfind("}")
    pos = 0
    while True:
        start = text.find("{", pos)
        if start == -1 or end == -1 or end <= start:
            break
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pos = start + 1
    raise ValueError(f"Could not parse a JSON verdict from response:\n{text[:800]}")


# ---------------------------------------------------------------------------
# Dry-run stubs  (activated by --dry-run; no agents called, no commits made)
# ---------------------------------------------------------------------------

DRY_RUN = False
STREAM = False
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
        text = run_claude(prompt, cwd, allowed_tools="Read,Edit,Write,Bash,Skill", permission_mode="bypassPermissions", tag=tag)
    elif agent == "codex":
        text = run_codex(prompt, cwd, sandbox="workspace-write", tag=tag)
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
        allowed = "Read,Bash(git diff *),Bash(git log *),Bash(git show *),Bash(cat *),Bash(*sed -n *),Skill"
        text = run_claude(prompt, cwd, allowed_tools=allowed, permission_mode="bypassPermissions", tag=tag)
        log(tag, phase, agent, prompt, text)
        return extract_json(text)
    elif agent == "codex":
        out_path = state_dir / f"verdict-{tag}-{phase}-{int(time.time() * 1000)}.json"
        schema_path = state_dir / "verdict-schema.json"
        text = run_codex(prompt, cwd, sandbox="read-only", schema_path=schema_path, out_path=out_path, tag=tag)
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


def merge_writer_worktrees(cwd, base_sha):
    """Merge any worktree branches that have commits past base_sha into cwd's current branch."""
    r = subprocess.run(["git", "worktree", "list", "--porcelain"],
                       cwd=cwd, capture_output=True, text=True, check=True)
    worktrees = []
    cur = {}
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            cur = {"path": line[9:]}
        elif line.startswith("HEAD "):
            cur["sha"] = line[5:]
        elif line.startswith("branch "):
            cur["branch"] = line[7:]
        elif line == "" and cur:
            if cur.get("path") != str(cwd):
                worktrees.append(cur)
            cur = {}
    if cur and cur.get("path") != str(cwd):
        worktrees.append(cur)

    merged = []
    for wt in worktrees:
        sha = wt.get("sha", "")
        branch_ref = wt.get("branch", "")
        if not sha or not branch_ref:
            continue
        r2 = subprocess.run(["git", "log", "--oneline", f"{base_sha}..{sha}"],
                             cwd=cwd, capture_output=True, text=True)
        if r2.returncode != 0 or not r2.stdout.strip():
            continue
        branch = branch_ref.replace("refs/heads/", "")
        print(f"[tango] merging worktree branch '{branch}' ({sha[:7]}) into current branch.")
        mr = subprocess.run(
            ["git", "merge", "--no-ff", branch, "-m", f"Merge worktree branch {branch}"],
            cwd=cwd, capture_output=True, text=True,
        )
        if mr.returncode != 0:
            raise RuntimeError(f"Failed to merge worktree branch '{branch}':\n{mr.stderr}")
        merged.append(branch)
    return merged



_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".eggs"}


def _md_files_newer_than(cwd, since_time):
    results = []
    for p in pathlib.Path(cwd).rglob("*.md"):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        try:
            if p.stat().st_mtime > since_time:
                results.append(p)
        except OSError:
            pass
    return results


def resolve_plan(phase, plan_override, plans_dirs, cwd, since_time=None):
    if plan_override:
        p = pathlib.Path(plan_override)
        if p.is_absolute():
            p = p.resolve()
        else:
            p = (cwd / p).resolve()
            if not p.exists():
                for d in plans_dirs:
                    candidate = d / plan_override
                    if candidate.exists():
                        return candidate
        if not p.exists():
            sys.exit(f"Plan file not found: {p} (also tried: "
                      f"{', '.join(str(d / plan_override) for d in plans_dirs)})")
        return p
    for d in plans_dirs:
        candidate = d / f"phase-{phase}.md"
        if candidate.exists():
            return candidate
    conventional = plans_dirs[0] / f"phase-{phase}.md"
    if since_time is None:
        sys.exit(f"No plan file found (tried: {', '.join(str(d) for d in plans_dirs)}). "
                 f"Pass --plan <path> or rerun the plan step.")
    candidates = _md_files_newer_than(cwd, since_time)
    if len(candidates) == 1:
        print(f"[tango] auto-detected plan file: {candidates[0]}")
        return candidates[0]
    if len(candidates) > 1:
        sys.exit(f"Multiple .md files written since writer ran; specify --plan <path>:\n  " +
                 "\n  ".join(str(c) for c in candidates))
    sys.exit(f"No plan file at {conventional} and no new .md files found after writer ran.")


def resolve_spec(phase, spec_override, phases_dirs, cwd=None):
    if spec_override:
        p = pathlib.Path(spec_override)
        if p.is_absolute():
            p = p.resolve()
        else:
            p = (cwd / p).resolve() if cwd else p.resolve()
            if not p.exists():
                for d in phases_dirs:
                    candidate = d / spec_override
                    if candidate.exists():
                        return candidate
        if not p.exists():
            if DRY_RUN:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(f"[dry-run] Spec for phase {phase}\n")
                print(f"[DRY-RUN] created fake spec at {p}")
            else:
                sys.exit(f"Spec file not found: {p} (also tried: "
                          f"{', '.join(str(d / spec_override) for d in phases_dirs)})")
        return p
    for d in phases_dirs:
        candidate = d / f"phase-{phase}.md"
        if candidate.exists():
            return candidate
    spec = phases_dirs[0] / f"phase-{phase}.md"
    if DRY_RUN:
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text(f"[dry-run] Spec for phase {phase}\n")
        print(f"[DRY-RUN] created fake spec at {spec}")
    else:
        sys.exit(f"Missing phase spec (tried: {', '.join(str(d / f'phase-{phase}.md') for d in phases_dirs)}). "
                 f"Write it there, or pass --spec <path>.")
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


def run_planning(phase, writer, reviewer, cwd, max_iters, phases_dirs, plans_dirs, state_dir, spec_override=None, plan_override=None):
    state = load_state(state_dir, phase)

    if state.get("plan_approved"):
        print(f"[phase {phase}] plan already approved, skipping.")
        return True

    phase_spec = resolve_spec(phase, spec_override, phases_dirs, cwd)
    # Writer saves to --plan's path if given (existing copy, or plans_dirs[0] if new);
    # otherwise the conventional phase-<N>.md path. resolve_plan is used after write.
    if plan_override:
        op = pathlib.Path(plan_override)
        if op.is_absolute():
            write_plan_path = op.resolve()
        else:
            write_plan_path = (cwd / op).resolve()
            if not write_plan_path.exists():
                for d in plans_dirs:
                    candidate = d / plan_override
                    if candidate.exists():
                        write_plan_path = candidate
                        break
                else:
                    write_plan_path = plans_dirs[0] / plan_override
    else:
        write_plan_path = plans_dirs[0] / f"phase-{phase}.md"
    write_plan_path.parent.mkdir(parents=True, exist_ok=True)

    if write_plan_path.exists():
        print(f"[phase {phase}] plan file exists, skipping write step.")
        write_time = None
    else:
        write_time = time.time()
        call_writer(
            writer,
            PLAN_WRITE_PROMPT.format(phase_spec=phase_spec, plan_path=write_plan_path, phase=phase),
            cwd, phase, "plan_write",
        )

    # After write, resolve actual plan path (may differ if writer saved elsewhere)
    plan_path = resolve_plan(phase, plan_override, plans_dirs, cwd, since_time=write_time)

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


def run_implementing(phase, writer, reviewer, cwd, max_iters, phases_dirs, plans_dirs, state_dir, spec_override=None, plan_override=None, base_sha_override=None):
    state = load_state(state_dir, phase)

    if state.get("code_approved"):
        print(f"[phase {phase}] code already approved, skipping.")
        return True

    phase_spec = resolve_spec(phase, spec_override, phases_dirs, cwd)
    plan_path = resolve_plan(phase, plan_override, plans_dirs, cwd)

    # base_sha is captured once before first write and persisted; no commit prefix required.
    if base_sha_override:
        base_sha = base_sha_override
        state["base_sha"] = base_sha
        save_state(state_dir, phase, state)
        print(f"[phase {phase}] base_sha set to {base_sha[:7]} (from --base-sha).")
    elif state.get("base_sha"):
        base_sha = state["base_sha"]
        print(f"[phase {phase}] base_sha {base_sha[:7]} (from saved state).")
    else:
        base_sha = get_head(cwd)
        state["base_sha"] = base_sha
        save_state(state_dir, phase, state)
        print(f"[phase {phase}] base_sha set to HEAD ({base_sha[:7]}).")

    if git_advanced(cwd, base_sha, phase=phase):
        print(f"[phase {phase}] commits exist past base_sha, skipping write step.")
    else:
        call_writer(
            writer,
            CODE_WRITE_PROMPT.format(plan_path=plan_path, phase_spec=phase_spec, phase=phase),
            cwd, phase, "code_write",
        )
        if not DRY_RUN:
            merge_writer_worktrees(cwd, base_sha)
        if not git_advanced(cwd, base_sha, phase=phase):
            sys.exit(f"{writer} did not commit anything for phase {phase}. Check {LOG_DIR} and fix manually.")

    fix_iter = state.get("code_fix_iter", 0)
    remaining = max_iters - fix_iter

    for i in range(1, remaining + 1):
        abs_iter = fix_iter + i
        head_sha = get_head(cwd)
        verdict = call_reviewer(
            reviewer,
            CODE_REVIEW_PROMPT.format(phase=phase, phase_spec=phase_spec, plan_path=plan_path,
                                       base_sha=base_sha, head_sha=head_sha),
            cwd, phase, "code_review", state_dir,
        )
        print(f"[phase {phase}] code review {abs_iter}/{max_iters} ({reviewer}): {verdict['verdict']}")
        if verdict["verdict"] == "APPROVED":
            state["code_approved"] = True
            save_state(state_dir, phase, state)
            print(f"[phase {phase}] code approved. Range: {base_sha[:7]}..{head_sha[:7]}")
            return True
        feedback = "\n".join(f"- [{x['severity']}] {x['description']}" for x in verdict["issues"]) or verdict["summary"]
        pre_fix_sha = get_head(cwd)
        call_writer(
            writer,
            CODE_FIX_PROMPT.format(feedback=feedback, phase=phase),
            cwd, phase, "code_fix",
        )
        if not DRY_RUN:
            merge_writer_worktrees(cwd, pre_fix_sha)
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

    global DRY_RUN, STREAM
    script_dir = pathlib.Path(__file__).parent

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", choices=["plan", "implement", "phase"])
    parser.add_argument("--phase", required=True,
                        help="Phase identifier (used for naming, state, and commit-message grep).")
    parser.add_argument("--spec", default=None,
                        help="Path to spec file. Overrides the default phases/phase-<N>.md lookup.")
    parser.add_argument("--plan", default=None,
                        help="Path to plan file for the reviewer. Auto-detected by mtime scan if omitted.")
    parser.add_argument("--writer", required=True, choices=["claude", "codex"])
    parser.add_argument("--reviewer", required=True, choices=["claude", "codex"])
    parser.add_argument("--repo-dir", default=os.environ.get("TANGO_REPO_DIR", "."))
    parser.add_argument("--max-iters", type=int, default=5)
    parser.add_argument("--config", default=None, help="Path to config TOML file (default: tango-prompts.toml next to script).")
    parser.add_argument("--claude-model", default=None, help="Model for claude agent (e.g. claude-opus-4-8).")
    parser.add_argument("--codex-model", default=None, help="Model for codex agent (e.g. o4-mini).")
    parser.add_argument("--claude-effort", default=None,
                        choices=["low", "medium", "high", "xhigh", "max"],
                        help="Effort level for claude (low/medium/high/xhigh/max).")
    parser.add_argument("--codex-effort", default=None,
                        choices=["low", "medium", "high"],
                        help="Reasoning effort for codex (low/medium/high).")
    parser.add_argument("--base-sha", default=None, metavar="SHA",
                        help="Override base commit for the implement step (skips write if commits already exist past SHA).")
    parser.add_argument("--reset", action="store_true", help="Clear saved state for this phase and start fresh.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate all steps without calling agents or committing. Shows prompts and state.")
    parser.add_argument("--no-stream", dest="stream", action="store_false", default=True,
                        help="Disable real-time agent output (streaming is on by default).")
    parser.add_argument("--resume-claude", default=None, metavar="SESSION_ID",
                        help="Resume a previous claude session by ID (from sessions.json).")
    parser.add_argument("--resume-codex", default=None, metavar="THREAD_ID",
                        help="Resume a previous codex session by thread ID (from sessions.json).")
    args = parser.parse_args()

    cwd = pathlib.Path(args.repo_dir).resolve()
    if not (cwd / ".git").exists():
        sys.exit(f"{cwd} doesn't look like a git repo root (no .git dir). "
                  f"Pass --repo-dir or set TANGO_REPO_DIR.")
    print(f"[tango] repo dir: {cwd}")

    cfg_writer, cfg_reviewer, cfg_spec_dirs, cfg_plan_dirs = load_prompts_config(
        script_dir, config_override=args.config, base_dir=cwd)
    args.writer = args.writer or cfg_writer
    args.reviewer = args.reviewer or cfg_reviewer
    for f in ("phase", "writer", "reviewer"):
        if getattr(args, f) is None:
            parser.error(f"argument --{f} is required in workflow mode (or set workflow.{f} in the config file)")

    # Conventional dir always searched first; configured extra dirs (workflow.spec_dirs /
    # workflow.plan_dirs, relative to repo_dir) are searched after it, in order.
    phases_dirs = [cwd / PHASES_DIR_NAME] + [cwd / d for d in cfg_spec_dirs]
    plans_dirs = [cwd / PLANS_DIR_NAME] + [cwd / d for d in cfg_plan_dirs]
    state_dir = cwd / STATE_DIR_NAME
    LOG_DIR = state_dir / "logs"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "verdict-schema.json").write_text(json.dumps(VERDICT_SCHEMA, indent=2))

    global RESUME_CLAUDE, RESUME_CODEX, SESSION_FILE
    SESSION_FILE = state_dir / "sessions.json"
    if args.resume_claude:
        RESUME_CLAUDE = args.resume_claude
        print(f"[tango] resuming claude session: {RESUME_CLAUDE}")
    if args.resume_codex:
        RESUME_CODEX = args.resume_codex
        print(f"[tango] resuming codex thread: {RESUME_CODEX}")

    if args.claude_model:
        AGENT_MODELS["claude"][""] = args.claude_model
    if args.codex_model:
        AGENT_MODELS["codex"][""] = args.codex_model
    if any(m for m in AGENT_MODELS.values()):
        for k, m in AGENT_MODELS.items():
            if m:
                print(f"[tango] {k} model(s): {m}")
    if args.claude_effort:
        AGENT_EFFORTS["claude"][""] = args.claude_effort
        print(f"[tango] claude effort: {args.claude_effort}")
    if args.codex_effort:
        AGENT_EFFORTS["codex"][""] = args.codex_effort
        print(f"[tango] codex reasoning effort: {args.codex_effort}")

    if args.stream:
        STREAM = True
        print("[tango] streaming mode: agent output printed in real-time.")
    else:
        print("[tango] streaming disabled (--no-stream).")

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
                           phases_dirs, plans_dirs, state_dir, spec_override=args.spec, plan_override=args.plan)
        if not ok:
            sys.exit(1)
    if args.step in ("implement", "phase"):
        ok = run_implementing(args.phase, args.writer, args.reviewer, cwd, args.max_iters,
                               phases_dirs, plans_dirs, state_dir, spec_override=args.spec, plan_override=args.plan,
                               base_sha_override=args.base_sha)
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
