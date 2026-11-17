from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from promise_monitor.config import ConfigError, load_app_config
from tests.conftest import REPO_ROOT

CONFIG_DIR = REPO_ROOT / "config"


def test_repo_config_loads() -> None:
    cfg = load_app_config(CONFIG_DIR)
    assert len(cfg.states) == 37
    assert "Abuja" in cfg.states  # site spelling for FCT
    assert cfg.tunables.article.min_words == 95
    assert cfg.tunables.article.max_words == 105
    assert cfg.tunables.api.min_interval_seconds >= 1.0  # politeness rule (AGENTS.md #7)
    assert cfg.priority.rank_for("presidential") == 1
    assert cfg.priority.rank_for("state:Kano") == cfg.priority.default_rank
    assert all(t == t.lower() for t in cfg.banned_terms)


def _copy_config(tmp_path: Path) -> Path:
    dst = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, dst)
    return dst


def test_missing_file_raises(tmp_path: Path) -> None:
    cfg_dir = _copy_config(tmp_path)
    (cfg_dir / "states.yaml").unlink()
    with pytest.raises(ConfigError, match="missing config file"):
        load_app_config(cfg_dir)


def test_duplicate_states_rejected(tmp_path: Path) -> None:
    cfg_dir = _copy_config(tmp_path)
    (cfg_dir / "states.yaml").write_text("states: [Lagos, Lagos]\n")
    with pytest.raises(ConfigError, match="duplicate"):
        load_app_config(cfg_dir)


def test_unknown_tunable_key_rejected(tmp_path: Path) -> None:
    cfg_dir = _copy_config(tmp_path)
    settings = cfg_dir / "settings.yaml"
    settings.write_text(settings.read_text() + "\nunexpected_key: 1\n")
    with pytest.raises(ValueError, match="unexpected_key"):
        load_app_config(cfg_dir)
