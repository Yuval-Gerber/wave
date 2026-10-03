"""Brain artifact loader — Stage 1 SHADOW (the toe-to-toe order, 2026-08-31).

The trained ensemble (scripts/train_judge.py) is saved as one joblib bundle
with a SHA-256 recorded next to it and GOLDEN VECTORS inside it: fixed
feature rows plus the exact probabilities the ensemble produced at save
time. Loading re-scores the golden vectors and refuses the artifact unless
every probability matches — a wrong-version sklearn, a truncated file, or a
feature-order drift all fail CLOSED. On any failure Wave stays rules-only
and says so loudly; the Brain never guesses.
"""

from __future__ import annotations

import hashlib
import logging

import numpy as np

# module-level on purpose: the artifact unpickles sklearn estimators, and
# PyInstaller only bundles what it can SEE imported (2026-09-01: the packaged
# app shipped without sklearn and the Brain silently fell to rules-only)
from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: F401
from sklearn.linear_model import LogisticRegression  # noqa: F401

from waveapp.config import support_dir

logger = logging.getLogger("wave.brain")

ARTIFACT_NAME = "brain_v1.joblib"
CHECKSUM_NAME = "brain_v1.sha256"
GOLDEN_TOLERANCE = 1e-6


class Brain:
    """A loaded, self-tested ensemble. score() is SHADOW-ONLY in Stage 1."""

    def __init__(self, bundle: dict) -> None:
        self.models = bundle["models"]
        self.platt = bundle["platt"]
        self.feature_cols: list[str] = list(bundle["feature_cols"])
        self.trained_at: str = bundle.get("trained_at", "")
        self.n_trades: int = int(bundle.get("n_trades", 0))
        self.base_wr: float = float(bundle.get("base_wr", 0.0))

    def score(self, features: dict[str, float]) -> float:
        """Calibrated P(win) for one candidate. Missing features become 0.0
        (the training pipeline uses the same convention)."""
        row = np.array([[float(features.get(c, 0.0)) for c in self.feature_cols]])
        raw = float(np.mean([m.predict_proba(row)[:, 1] for m in self.models]))
        return float(self.platt.predict_proba([[raw]])[0, 1])

    def score_raw_matrix(self, x: np.ndarray) -> np.ndarray:
        raw = np.mean([m.predict_proba(x)[:, 1] for m in self.models], axis=0)
        return self.platt.predict_proba(raw.reshape(-1, 1))[:, 1]


def artifact_path():
    return support_dir() / ARTIFACT_NAME


def checksum_path():
    return support_dir() / CHECKSUM_NAME


def load_brain(path=None) -> Brain | None:
    """Load + verify the artifact. Returns None (rules-only) on ANY doubt."""
    import joblib

    path = path or artifact_path()
    try:
        if not path.exists():
            logger.info("brain artifact absent — rules-only")
            return None
        blob = path.read_bytes()
        digest = hashlib.sha256(blob).hexdigest()
        expected = None
        cpath = path.with_name(path.name.replace(".joblib", ".sha256"))
        if cpath.exists():
            expected = cpath.read_text().strip().split()[0]
        if expected is not None and digest != expected:
            logger.error("BRAIN REFUSED: artifact checksum mismatch — rules-only")
            return None
        bundle = joblib.load(path)
        brain = Brain(bundle)
        golden_x = np.asarray(bundle["golden_x"], dtype=np.float64)
        golden_p = np.asarray(bundle["golden_p"], dtype=np.float64)
        got = brain.score_raw_matrix(golden_x)
        if got.shape != golden_p.shape or np.max(np.abs(got - golden_p)) > GOLDEN_TOLERANCE:
            logger.error(
                "BRAIN REFUSED: golden-vector self-test failed (max diff %.3g) — rules-only",
                float(np.max(np.abs(got - golden_p))) if got.shape == golden_p.shape else -1.0,
            )
            return None
        logger.info(
            "brain loaded: trained %s on %d trades (base WR %.1f%%) — self-test passed",
            brain.trained_at,
            brain.n_trades,
            brain.base_wr * 100.0,
        )
        return brain
    except Exception:
        logger.exception("BRAIN REFUSED: artifact unreadable — rules-only")
        return None


def load_shadow_brain(model_name: str = "v1") -> Brain | None:
    """Resolve the shadow slot's artifact by config (ML MASTER PLAN M1,
    2026-09-23). "v1" → the default brain_v1.joblib. Any other value
    "<name>" → support_dir()/brain_<name>.joblib; when that artifact is
    missing or refused by the armor, WARN and fall back to v1. Whatever
    loads remains SHADOW-only — selection never changes what may act."""
    name = (model_name or "v1").strip()
    if name != "v1":
        path = support_dir() / f"brain_{name}.joblib"
        brain = load_brain(path=path)
        if brain is not None:
            logger.info("shadow brain: ml_model=%s → %s loaded", name, path.name)
            return brain
        logger.warning(
            "shadow brain: ml_model=%s artifact missing/refused (%s) — falling back to v1",
            name,
            path.name,
        )
    return load_brain()


def save_brain(models, platt, feature_cols, x_sample: np.ndarray, meta: dict, path=None) -> str:
    """Persist the ensemble with golden vectors + sidecar checksum. Returns
    the hex digest. Called by scripts/train_judge.py, never by the app."""
    import joblib

    path = path or artifact_path()
    golden_x = np.asarray(x_sample, dtype=np.float64)[:5]
    raw = np.mean([m.predict_proba(golden_x)[:, 1] for m in models], axis=0)
    golden_p = platt.predict_proba(raw.reshape(-1, 1))[:, 1]
    bundle = {
        "models": models,
        "platt": platt,
        "feature_cols": list(feature_cols),
        "golden_x": golden_x,
        "golden_p": golden_p,
        **meta,
    }
    joblib.dump(bundle, path, compress=3)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_name(path.name.replace(".joblib", ".sha256")).write_text(f"{digest}  {path.name}\n")
    return digest
