"""Configuration + path resolution.

Loads the packaged config.default.json (bundled inside the `exi` package as
package data, so it survives a normal wheel install) and shallow-merges an
optional per-user config.json over it. Runtime paths (user config, data dir)
default to XDG locations so a plain `pip install` needs no environment
variables at all; EXI_CONFIG / EXI_DATA_DIR remain authoritative overrides
for anyone — including the test suite — who wants to relocate them.
"""
from __future__ import annotations

import json
import os
from importlib import resources
from pathlib import Path

# <root>/exi/config.py -> root is parent of the exi package dir. Used only
# for dev-checkout conveniences (see hookmerge.guard_bin()); config and data
# resolution below never depends on this directory being writable, or even
# present, inside an installed package.
ROOT = Path(__file__).resolve().parent.parent

APP_NAME = "codex-feedback-guard"


def _xdg_home(env_var: str, fallback: Path) -> Path:
    override = os.environ.get(env_var)
    return Path(override) if override else fallback


def _windows_base() -> Path:
    """Per-user writable base on Windows: %LOCALAPPDATA% with a sensible fallback.

    ``%LOCALAPPDATA%`` (e.g. ``C:\\Users\\me\\AppData\\Local``) is the correct
    home for non-roaming per-user application state; when the variable is unset
    (rare, but possible under a stripped service account) fall back to the
    conventional ``~/AppData/Local`` path.
    """
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) if base else Path.home() / "AppData" / "Local"


def _is_windows() -> bool:
    return os.name == "nt"


def default_config_path(is_windows: bool | None = None) -> Path:
    """Per-user config.json location.

    Windows: ``%LOCALAPPDATA%\\codex-feedback-guard\\config.json``.
    POSIX:   ``$XDG_CONFIG_HOME/codex-feedback-guard/config.json``.

    ``is_windows`` is injectable (defaults to the real platform) so the branch
    can be tested on either OS without patching ``os.name`` — patching it would
    also break ``pathlib`` on the test runner.
    """
    if _is_windows() if is_windows is None else is_windows:
        return _windows_base() / APP_NAME / "config.json"
    return _xdg_home("XDG_CONFIG_HOME", Path.home() / ".config") / APP_NAME / "config.json"


def default_data_dir(is_windows: bool | None = None) -> Path:
    """Per-user runtime-data dir.

    Windows: ``%LOCALAPPDATA%\\codex-feedback-guard``.
    POSIX:   ``$XDG_DATA_HOME/codex-feedback-guard``.
    """
    if _is_windows() if is_windows is None else is_windows:
        return _windows_base() / APP_NAME
    return _xdg_home("XDG_DATA_HOME", Path.home() / ".local" / "share") / APP_NAME


def data_dir() -> Path:
    """Directory holding runtime state (observations, index, guard state).

    EXI_DATA_DIR is authoritative when set (also used by tests to isolate
    state); otherwise defaults to a per-user XDG data directory so a normal
    install needs no environment variables.
    """
    d = os.environ.get("EXI_DATA_DIR")
    path = Path(d) if d else default_data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_default_config() -> dict:
    text = resources.files("exi").joinpath("config.default.json").read_text(encoding="utf-8")
    cfg = json.loads(text)
    cfg.pop("_comment", None)
    return cfg


def load_config() -> dict:
    """Packaged defaults deep-merged with an optional per-user config.json.

    EXI_CONFIG is authoritative when set (also used by tests to isolate
    state); otherwise the user config is read from the per-user XDG config
    path, if it exists.
    """
    cfg = _load_default_config()

    override = os.environ.get("EXI_CONFIG")
    user_path = Path(override) if override else default_config_path()
    if user_path.exists():
        with open(user_path, encoding="utf-8") as f:
            user = json.load(f)
        user.pop("_comment", None)
        cfg = _deep_merge(cfg, user)
    return cfg
