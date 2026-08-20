"""Platform path resolution: POSIX XDG vs Windows LOCALAPPDATA, with env
overrides authoritative everywhere. The Windows branch is exercised on a POSIX
runner by patching ``os.name`` (default_config_path/default_data_dir read it at
call time and do no filesystem I/O)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import conftest_paths  # noqa: F401

from exi import config


class WindowsPathTest(unittest.TestCase):
    # is_windows is injected (not via os.name) so pathlib on the POSIX runner
    # still constructs plain PosixPaths from the Windows-looking strings; the
    # comparison target is built the same way, so the branch logic is verified.
    def test_data_dir_uses_localappdata(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\me\AppData\Local"}, clear=False):
            p = config.default_data_dir(is_windows=True)
        self.assertEqual(p, Path(r"C:\Users\me\AppData\Local") / "codex-feedback-guard")

    def test_config_path_uses_localappdata(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\me\AppData\Local"}, clear=False):
            p = config.default_config_path(is_windows=True)
        self.assertEqual(p, Path(r"C:\Users\me\AppData\Local") / "codex-feedback-guard" / "config.json")

    def test_localappdata_fallback_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOCALAPPDATA", None)
            p = config.default_data_dir(is_windows=True)
        self.assertEqual(p, Path.home() / "AppData" / "Local" / "codex-feedback-guard")


class PosixPathTest(unittest.TestCase):
    def test_data_dir_uses_xdg(self):
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/xdg/data"}, clear=False):
            p = config.default_data_dir(is_windows=False)
        self.assertEqual(p, Path("/xdg/data") / "codex-feedback-guard")

    def test_data_dir_default_home(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XDG_DATA_HOME", None)
            p = config.default_data_dir(is_windows=False)
        self.assertEqual(p, Path.home() / ".local" / "share" / "codex-feedback-guard")


class EnvOverrideAuthoritativeTest(unittest.TestCase):
    def test_exi_data_dir_wins(self):
        # EXI_DATA_DIR is authoritative before any platform branch is consulted.
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"EXI_DATA_DIR": d}, clear=False):
                self.assertEqual(config.data_dir(), Path(d))

    def test_exi_config_wins(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "cfg.json"
            cfg_path.write_text('{"feedback": {"inject_min_count": 9}}', encoding="utf-8")
            with mock.patch.dict(os.environ, {"EXI_CONFIG": str(cfg_path)}, clear=False):
                cfg = config.load_config()
            self.assertEqual(cfg["feedback"]["inject_min_count"], 9)


if __name__ == "__main__":
    unittest.main()
