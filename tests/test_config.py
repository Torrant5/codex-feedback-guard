import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import conftest_paths  # noqa: F401

from exi import config

ENV_KEYS = ["HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "EXI_CONFIG", "EXI_DATA_DIR"]


class ConfigPathResolutionTest(unittest.TestCase):
    """Default path resolution under isolated HOME/XDG env — no EXI_* set.

    Guards against a regression to path resolution that only works for an
    editable install / checkout (see hookmerge's default-hook-command tests
    for the matching wheel-style-install coverage).
    """

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_default_config_path_uses_xdg_config_home(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["XDG_CONFIG_HOME"] = d
            self.assertEqual(
                config.default_config_path(),
                Path(d) / "codex-feedback-guard" / "config.json",
            )

    def test_default_data_dir_uses_xdg_data_home(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["XDG_DATA_HOME"] = d
            self.assertEqual(
                config.default_data_dir(),
                Path(d) / "codex-feedback-guard",
            )

    def test_default_config_path_falls_back_to_home_dot_config(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["HOME"] = d
            self.assertEqual(
                config.default_config_path(),
                Path(d) / ".config" / "codex-feedback-guard" / "config.json",
            )

    def test_default_data_dir_falls_back_to_home_local_share(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["HOME"] = d
            self.assertEqual(
                config.default_data_dir(),
                Path(d) / ".local" / "share" / "codex-feedback-guard",
            )

    def test_exi_env_vars_stay_authoritative_over_xdg(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["XDG_CONFIG_HOME"] = str(Path(d) / "xdg-config")
            os.environ["XDG_DATA_HOME"] = str(Path(d) / "xdg-data")
            os.environ["EXI_CONFIG"] = str(Path(d) / "explicit-config.json")
            os.environ["EXI_DATA_DIR"] = str(Path(d) / "explicit-data")
            cfg = config.load_config()
            data_path = config.data_dir()
        self.assertIn("guard", cfg)  # EXI_CONFIG override just doesn't exist -> pure defaults
        self.assertEqual(data_path, Path(d) / "explicit-data")

    def test_load_config_does_not_depend_on_root_relative_files(self):
        """Wheel-style install simulation: config.ROOT has neither
        config.default.json nor config.json under it, so any code path
        still reading relative to ROOT would raise. load_config() must
        still succeed because defaults are packaged inside the `exi`
        module itself (importlib.resources), independent of ROOT.
        """
        with tempfile.TemporaryDirectory() as d:
            os.environ["HOME"] = d
            with patch.object(config, "ROOT", Path(d) / "nonexistent-root"):
                cfg = config.load_config()
        self.assertIn("guard", cfg)
        self.assertEqual(cfg["guard"]["turn_soft_minutes"], 45)
        self.assertIn("tool_hard_count", cfg["guard"])

    def test_load_config_merges_user_config_from_xdg_path(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["XDG_CONFIG_HOME"] = d
            user_dir = Path(d) / "codex-feedback-guard"
            user_dir.mkdir(parents=True)
            (user_dir / "config.json").write_text(
                '{"guard": {"turn_soft_minutes": 5}}', encoding="utf-8"
            )
            cfg = config.load_config()
        self.assertEqual(cfg["guard"]["turn_soft_minutes"], 5)
        self.assertIn("tool_hard_count", cfg["guard"])  # untouched defaults still present

    def test_data_dir_creates_xdg_directory(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["XDG_DATA_HOME"] = d
            path = config.data_dir()
            self.assertEqual(path, Path(d) / "codex-feedback-guard")
            self.assertTrue(path.is_dir())


if __name__ == "__main__":
    unittest.main()
