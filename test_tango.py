import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import tango


SCRIPT = pathlib.Path(__file__).with_name("tango.py")


class ConfiguredAgentsTest(unittest.TestCase):
    def test_config_supplies_writer_and_reviewer(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            (repo / ".git").mkdir()
            (repo / "phases").mkdir()
            (repo / "phases" / "phase-1.md").write_text("# Phase 1\n")
            config = repo / "agents.toml"
            config.write_text("[workflow]\nwriter = 'claude'\nreviewer = 'codex'\n")

            result = subprocess.run(
                [sys.executable, str(SCRIPT), "plan", "--phase", "1", "--config", str(config),
                 "--repo-dir", str(repo), "--max-iters", "3", "--dry-run"],
                text=True, capture_output=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_plan_is_written_to_configured_plan_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            (repo / ".git").mkdir()
            (repo / "phases").mkdir()
            (repo / "phases" / "phase-1.md").write_text("# Phase 1\n")
            config = repo / "agents.toml"
            config.write_text(
                "[workflow]\nwriter = 'claude'\nreviewer = 'codex'\nplan_dirs = ['workflow-plans']\n"
            )

            result = subprocess.run(
                [sys.executable, str(SCRIPT), "plan", "--phase", "1", "--config", str(config),
                 "--repo-dir", str(repo), "--max-iters", "3", "--dry-run"],
                text=True, capture_output=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((repo / "workflow-plans" / "phase-1.md").exists())

    def test_plan_writer_must_create_the_requested_plan_before_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            (repo / "phases").mkdir()
            (repo / "phases" / "phase-1.md").write_text("# Phase 1\n")
            state_dir = repo / ".agent-workflow"
            state_dir.mkdir()

            with mock.patch.object(tango, "call_writer"), mock.patch.object(tango, "call_reviewer") as reviewer:
                with self.assertRaisesRegex(SystemExit, "did not write the requested plan"):
                    tango.run_planning(
                        "1", "claude", "codex", repo, 1,
                        [repo / "phases"], [repo / "plans"], state_dir,
                    )

            reviewer.assert_not_called()

    def test_code_fix_is_not_started_after_final_review_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            (repo / "phases").mkdir()
            (repo / "plans").mkdir()
            (repo / "phases" / "phase-1.md").write_text("# Phase 1\n")
            (repo / "plans" / "phase-1.md").write_text("# Plan 1\n")
            state_dir = repo / ".agent-workflow"
            state_dir.mkdir()
            tango.LOG_DIR = state_dir / "logs"

            verdict = {"verdict": "CHANGES_NEEDED", "summary": "needs work", "issues": []}
            with (
                mock.patch.object(tango, "get_head", return_value="abcdef1234567890"),
                mock.patch.object(tango, "git_advanced", return_value=True),
                mock.patch.object(tango, "call_reviewer", return_value=verdict),
                mock.patch.object(tango, "merge_writer_worktrees"),
                mock.patch.object(tango, "call_writer") as writer,
            ):
                ok = tango.run_implementing(
                    "1", "claude", "codex", repo, 1,
                    [repo / "phases"], [repo / "plans"], state_dir,
                )

            self.assertFalse(ok)
            writer.assert_not_called()

    def test_examples_config_is_valid_for_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            (repo / ".git").mkdir()
            (repo / "phases").mkdir()
            (repo / "phases" / "phase-1.md").write_text("# Phase 1\n")

            result = subprocess.run(
                [sys.executable, str(SCRIPT), "plan", "--phase", "1",
                 "--writer", "claude", "--reviewer", "codex",
                 "--config", str(SCRIPT.parent / "examples" / "tango-prompts.toml"),
                 "--repo-dir", str(repo), "--max-iters", "3", "--dry-run"],
                text=True, capture_output=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
