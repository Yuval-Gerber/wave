"""M1 shadow-model selection (ML MASTER PLAN, 2026-09-23): config.ml_model
points the SHADOW brain slot at an artifact by name — default "v1" keeps
behavior identical, a missing/refused artifact falls back to v1 loudly.
Nothing here may influence trading: whatever loads stays shadow-only."""

from __future__ import annotations

import logging

import numpy as np
import pytest


def _make_artifact(path, trained_at: str) -> None:
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression

    from waveapp.engine.brain import save_brain

    rng = np.random.default_rng(3)
    x = rng.normal(size=(200, 3))
    y = (x[:, 0] + rng.normal(scale=0.5, size=200) > 0).astype(int)
    models = [HistGradientBoostingClassifier(max_iter=20, random_state=0).fit(x, y)]
    raw = models[0].predict_proba(x)[:, 1]
    platt = LogisticRegression().fit(raw.reshape(-1, 1), y)
    save_brain(
        models,
        platt,
        ["f0", "f1", "f2"],
        x[:5],
        {"trained_at": trained_at, "n_trades": 200, "base_wr": 0.5},
        path=path,
    )


@pytest.fixture
def support(tmp_path, monkeypatch):
    import waveapp.engine.brain as brain_mod

    monkeypatch.setattr(brain_mod, "support_dir", lambda: tmp_path)
    return tmp_path


def test_config_default_is_v1(tmp_path):
    from waveapp.config import AppConfig

    assert AppConfig().ml_model == "v1"
    # an existing config.toml without the key also yields the default
    path = tmp_path / "config.toml"
    path.write_text('ml_mode = "shadow"\n')
    assert AppConfig.load(path).ml_model == "v1"


def test_loader_picks_artifact_per_config(support):
    from waveapp.engine.brain import load_shadow_brain

    _make_artifact(support / "brain_v1.joblib", "V1-STAMP")
    _make_artifact(support / "brain_v2test.joblib", "V2-STAMP")

    assert load_shadow_brain("v1").trained_at == "V1-STAMP"
    assert load_shadow_brain("v2test").trained_at == "V2-STAMP"
    # blank/None behave like the default
    assert load_shadow_brain("").trained_at == "V1-STAMP"


def test_missing_v2_falls_back_to_v1_with_warning(support, caplog):
    from waveapp.engine.brain import load_shadow_brain

    _make_artifact(support / "brain_v1.joblib", "V1-STAMP")
    with caplog.at_level(logging.WARNING, logger="wave.brain"):
        brain = load_shadow_brain("v2.0-2099-01-01")
    assert brain is not None and brain.trained_at == "V1-STAMP"
    assert any("falling back to v1" in r.message for r in caplog.records)


def test_no_artifacts_at_all_is_rules_only(support, caplog):
    from waveapp.engine.brain import load_shadow_brain

    with caplog.at_level(logging.WARNING, logger="wave.brain"):
        assert load_shadow_brain("v2test") is None
