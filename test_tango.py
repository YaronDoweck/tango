import pathlib
import subprocess
import sys
import tempfile
import unittest


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

