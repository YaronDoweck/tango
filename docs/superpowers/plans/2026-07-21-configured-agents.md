# Configured Agent Defaults Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Permit the config file to provide writer and reviewer when CLI flags are absent.

**Architecture:** Keep agent precedence unchanged: CLI values override TOML values. Remove argparse's premature required check so the existing post-config check can validate the resolved values.

**Tech Stack:** Python standard library (`argparse`, `unittest`, `tempfile`, `subprocess`)

## Global Constraints

- Do not add dependencies.
- Keep `--writer` and `--reviewer` choices restricted to `claude` and `codex`.

---

### Task 1: Allow config-only agent selection

**Files:**
- Create: `test_tango.py`
- Modify: `tango.py:883-884`
- Modify: `README.md:210-211`

**Interfaces:**
- Consumes: a TOML `[workflow]` section with `writer` and `reviewer`.
- Produces: `python tango.py plan --phase 1 --config FILE --dry-run` accepts those configured agents without CLI agent flags.

- [ ] **Step 1: Write the failing test**

```python
def test_config_supplies_writer_and_reviewer():
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        (repo / ".git").mkdir()
        (repo / "phases").mkdir()
        (repo / "phases" / "phase-1.md").write_text("# Phase 1\n")
        config = repo / "agents.toml"
        config.write_text("[workflow]\nwriter = 'claude'\nreviewer = 'codex'\n")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "plan", "--phase", "1", "--config", str(config), "--repo-dir", str(repo), "--max-iters", "3", "--dry-run"],
            text=True, capture_output=True,
        )
    assert result.returncode == 0, result.stderr
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_tango.ConfiguredAgentsTest.test_config_supplies_writer_and_reviewer -v`
Expected: FAIL because argparse requires `--writer` and `--reviewer`.

- [ ] **Step 3: Write minimal implementation**

```python
parser.add_argument("--writer", choices=["claude", "codex"],
                    help="Agent that plans and codes (or workflow.writer in config).")
parser.add_argument("--reviewer", choices=["claude", "codex"],
                    help="Agent that reviews (or workflow.reviewer in config).")
```

Update the README table to state each is required only when not set in `workflow.writer` or `workflow.reviewer`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest test_tango -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tango.py test_tango.py README.md
git commit -m "fix: allow configured workflow agents"
```
