"""Brain Stage-1 artifact tests: the loader trusts NOTHING it can't verify."""

from __future__ import annotations

import numpy as np
import pytest


@pytest.fixture
def tiny_artifact(tmp_path):
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression

    from waveapp.engine.brain import save_brain

    rng = np.random.default_rng(7)
    x = rng.normal(size=(300, 4))
    y = (x[:, 0] + 0.5 * x[:, 1] + rng.normal(scale=0.5, size=300) > 0).astype(int)
    models = []
    for seed in range(3):
        m = HistGradientBoostingClassifier(max_iter=30, random_state=seed)
        m.fit(x, y)
        models.append(m)
    raw = np.mean([m.predict_proba(x)[:, 1] for m in models], axis=0)
    platt = LogisticRegression().fit(raw.reshape(-1, 1), y)
    path = tmp_path / "brain_v1.joblib"
    digest = save_brain(
        models,
        platt,
        ["f0", "f1", "f2", "f3"],
        x[:5],
        {"trained_at": "2026-08-31T23:00:00+00:00", "n_trades": 300, "base_wr": 0.52},
        path=path,
    )
    return path, digest


def test_load_and_score(tiny_artifact):
    from waveapp.engine.brain import load_brain

    path, _digest = tiny_artifact
    brain = load_brain(path)
    assert brain is not None
    p = brain.score({"f0": 2.0, "f1": 1.0, "f2": 0.0, "f3": 0.0})
    q = brain.score({"f0": -2.0, "f1": -1.0, "f2": 0.0, "f3": 0.0})
    assert 0.0 <= q < p <= 1.0  # strong-up features score higher than strong-down
    assert brain.n_trades == 300


def test_missing_feature_defaults_to_zero(tiny_artifact):
    from waveapp.engine.brain import load_brain

    brain = load_brain(tiny_artifact[0])
    assert brain.score({}) == pytest.approx(brain.score({"f0": 0.0, "f1": 0.0}), abs=1e-12)


def test_tampered_artifact_is_refused(tiny_artifact):
    from waveapp.engine.brain import load_brain

    path, _ = tiny_artifact
    blob = bytearray(path.read_bytes())
    blob[len(blob) // 2] ^= 0xFF  # flip one byte in the middle
    path.write_bytes(bytes(blob))
    assert load_brain(path) is None  # checksum mismatch → rules-only


def test_wrong_checksum_sidecar_is_refused(tiny_artifact):
    from waveapp.engine.brain import load_brain

    path, _ = tiny_artifact
    path.with_name("brain_v1.sha256").write_text("deadbeef" * 8 + "  brain_v1.joblib\n")
    assert load_brain(path) is None


def test_absent_artifact_is_quietly_rules_only(tmp_path):
    from waveapp.engine.brain import load_brain

    assert load_brain(tmp_path / "nope.joblib") is None
