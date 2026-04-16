"""
Phase 3 — Train win-probability model with Optuna + LightGBM.

Reads data/processed/training_rows.parquet, optimizes hyperparameters via
200 Optuna trials with 5-fold group-stratified CV on log-loss, then trains
a final model and saves it.

Optuna study is persisted in SQLite so optuna-dashboard can visualize live:
  optuna-dashboard sqlite:///data/models/optuna_study.db

Output:
  data/models/winprob_lgbm.joblib  — trained LightGBM model
  data/models/study.json           — Optuna study results
  data/models/optuna_study.db      — SQLite DB for optuna-dashboard
"""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
import optuna
import pandas as pd
from sklearn.metrics import log_loss, roc_auc_score
from sklearn.model_selection import GroupKFold

warnings.filterwarnings("ignore", category=UserWarning)
optuna.logging.set_verbosity(optuna.logging.WARNING)

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
PROCESSED_DIR = DATA_DIR / "processed"
MODELS_DIR = DATA_DIR / "models"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

OPTUNA_DB = f"sqlite:///{MODELS_DIR / 'optuna_study.db'}"
STUDY_NAME = "lol_winprob_v1"

FEATURES_FULL = [
    "game_minute",
    "kill_diff",
    "tower_diff",
    "drake_diff",
    "baron_diff",
    "inhib_diff",
    "gold_diff",
    "herald_diff",
    "total_kills",
    "total_objectives",
]

FEATURES_NO_GOLD = [f for f in FEATURES_FULL if f != "gold_diff"]

# Production model uses FEATURES_NO_GOLD because LLF doesn't provide gold data.
FEATURES = FEATURES_NO_GOLD

LABEL = "blue_won"
GROUP = "game_id"
N_FOLDS = 5
N_TRIALS = 200
SEED = 42


def load_data() -> pd.DataFrame:
    path = PROCESSED_DIR / "training_rows.parquet"
    if not path.exists():
        print(f"[train] ERROR: {path} not found. Run build_dataset.py first.")
        sys.exit(1)
    df = pd.read_parquet(path)
    print(f"[train] loaded {len(df):,} rows, {df[GROUP].nunique():,} games")
    return df


def objective(trial: optuna.Trial, X: np.ndarray, y: np.ndarray, groups: np.ndarray) -> float:
    params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "verbosity": -1,
        "boosting_type": "gbdt",
        "seed": SEED,
        "num_leaves": trial.suggest_int("num_leaves", 15, 256),
        "learning_rate": trial.suggest_float("learning_rate", 0.005, 0.3, log=True),
        "n_estimators": trial.suggest_int("n_estimators", 100, 2000),
        "min_child_samples": trial.suggest_int("min_child_samples", 5, 100),
        "reg_alpha": trial.suggest_float("reg_alpha", 1e-8, 10.0, log=True),
        "reg_lambda": trial.suggest_float("reg_lambda", 1e-8, 10.0, log=True),
        "subsample": trial.suggest_float("subsample", 0.5, 1.0),
        "colsample_bytree": trial.suggest_float("colsample_bytree", 0.5, 1.0),
        "max_depth": trial.suggest_int("max_depth", 3, 12),
    }

    gkf = GroupKFold(n_splits=N_FOLDS)
    fold_losses = []

    for train_idx, val_idx in gkf.split(X, y, groups):
        X_tr, X_val = X[train_idx], X[val_idx]
        y_tr, y_val = y[train_idx], y[val_idx]

        model = lgb.LGBMClassifier(**params)
        model.fit(
            X_tr, y_tr,
            eval_set=[(X_val, y_val)],
            callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(-1)],
        )
        preds = model.predict_proba(X_val)[:, 1]
        fold_losses.append(log_loss(y_val, preds))

    return np.mean(fold_losses)


def train_final_model(params: dict, X: np.ndarray, y: np.ndarray) -> lgb.LGBMClassifier:
    model = lgb.LGBMClassifier(**params)
    model.fit(X, y)
    return model


def calibration_analysis(model: lgb.LGBMClassifier, X: np.ndarray, y: np.ndarray, groups: np.ndarray):
    """Print calibration stats using held-out predictions from group CV."""
    gkf = GroupKFold(n_splits=N_FOLDS)
    all_preds = np.zeros(len(y))
    all_true = np.zeros(len(y))

    for train_idx, val_idx in gkf.split(X, y, groups):
        X_tr, X_val = X[train_idx], X[val_idx]
        y_tr, y_val = y[train_idx], y[val_idx]

        m = lgb.LGBMClassifier(**model.get_params())
        m.fit(X_tr, y_tr)
        all_preds[val_idx] = m.predict_proba(X_val)[:, 1]
        all_true[val_idx] = y_val

    logloss = log_loss(all_true, all_preds)
    auc = roc_auc_score(all_true, all_preds)
    print(f"\n  CV Log-Loss: {logloss:.5f}")
    print(f"  CV AUC:      {auc:.5f}")

    print("\n  Calibration (decile bins):")
    print(f"  {'Bin':>12} {'Predicted':>10} {'Actual':>10} {'Count':>8}")
    print(f"  {'-'*42}")

    bins = np.linspace(0, 1, 11)
    for i in range(10):
        mask = (all_preds >= bins[i]) & (all_preds < bins[i + 1])
        if mask.sum() == 0:
            continue
        pred_mean = all_preds[mask].mean()
        actual_mean = all_true[mask].mean()
        print(f"  {bins[i]:.1f}-{bins[i+1]:.1f}   {pred_mean:>10.4f} {actual_mean:>10.4f} {mask.sum():>8,}")

    return logloss, auc, all_preds, all_true


def print_event_impact_examples(model: lgb.LGBMClassifier):
    """Show model-predicted impact of common events at realistic game states."""
    print("\n  Event Impact Examples (delta in P(blue_wins)):")
    print(f"  {'Scenario':<55} {'P_before':>8} {'P_after':>8} {'Delta':>8}")
    print(f"  {'-'*83}")

    # Each scenario: (label, before_state_dict, after_state_dict)
    # States must be self-consistent (total_kills = sum of both teams, etc.)
    scenarios = [
        ("First blood min 3, 0-0",
         {"game_minute": 3, "kill_diff": 0, "tower_diff": 0, "drake_diff": 0, "baron_diff": 0,
          "inhib_diff": 0, "herald_diff": 0, "total_kills": 0, "total_objectives": 0},
         {"game_minute": 3, "kill_diff": 1, "tower_diff": 0, "drake_diff": 0, "baron_diff": 0,
          "inhib_diff": 0, "herald_diff": 0, "total_kills": 1, "total_objectives": 0}),
        ("Kill min 15, 4-3 game",
         {"game_minute": 15, "kill_diff": 1, "tower_diff": 0, "drake_diff": 0, "baron_diff": 0,
          "inhib_diff": 0, "herald_diff": 0, "total_kills": 7, "total_objectives": 1},
         {"game_minute": 15, "kill_diff": 2, "tower_diff": 0, "drake_diff": 0, "baron_diff": 0,
          "inhib_diff": 0, "herald_diff": 0, "total_kills": 8, "total_objectives": 1}),
        ("Kill min 30, even 10-10",
         {"game_minute": 30, "kill_diff": 0, "tower_diff": 0, "drake_diff": 0, "baron_diff": 0,
          "inhib_diff": 0, "herald_diff": 0, "total_kills": 20, "total_objectives": 6},
         {"game_minute": 30, "kill_diff": 1, "tower_diff": 0, "drake_diff": 0, "baron_diff": 0,
          "inhib_diff": 0, "herald_diff": 0, "total_kills": 21, "total_objectives": 6}),
        ("Triple kill min 25, 8-6",
         {"game_minute": 25, "kill_diff": 2, "tower_diff": 1, "drake_diff": 0, "baron_diff": 0,
          "inhib_diff": 0, "herald_diff": 0, "total_kills": 14, "total_objectives": 4},
         {"game_minute": 25, "kill_diff": 5, "tower_diff": 1, "drake_diff": 0, "baron_diff": 0,
          "inhib_diff": 0, "herald_diff": 0, "total_kills": 17, "total_objectives": 4}),
        ("Dragon min 20, even 6-6",
         {"game_minute": 20, "kill_diff": 0, "tower_diff": 0, "drake_diff": 0, "baron_diff": 0,
          "inhib_diff": 0, "herald_diff": 0, "total_kills": 12, "total_objectives": 3},
         {"game_minute": 20, "kill_diff": 0, "tower_diff": 0, "drake_diff": 1, "baron_diff": 0,
          "inhib_diff": 0, "herald_diff": 0, "total_kills": 12, "total_objectives": 4}),
        ("Soul drake (4th) min 28, ahead +3 kills",
         {"game_minute": 28, "kill_diff": 3, "tower_diff": 1, "drake_diff": 2, "baron_diff": 0,
          "inhib_diff": 0, "herald_diff": 0, "total_kills": 18, "total_objectives": 7},
         {"game_minute": 28, "kill_diff": 3, "tower_diff": 1, "drake_diff": 3, "baron_diff": 0,
          "inhib_diff": 0, "herald_diff": 0, "total_kills": 18, "total_objectives": 8}),
        ("Baron min 25, even",
         {"game_minute": 25, "kill_diff": 0, "tower_diff": 0, "drake_diff": 0, "baron_diff": 0,
          "inhib_diff": 0, "herald_diff": 0, "total_kills": 14, "total_objectives": 5},
         {"game_minute": 25, "kill_diff": 0, "tower_diff": 0, "drake_diff": 0, "baron_diff": 1,
          "inhib_diff": 0, "herald_diff": 0, "total_kills": 14, "total_objectives": 6}),
        ("Baron min 35, +4 kills ahead",
         {"game_minute": 35, "kill_diff": 4, "tower_diff": 2, "drake_diff": 1, "baron_diff": 0,
          "inhib_diff": 0, "herald_diff": 0, "total_kills": 24, "total_objectives": 8},
         {"game_minute": 35, "kill_diff": 4, "tower_diff": 2, "drake_diff": 1, "baron_diff": 1,
          "inhib_diff": 0, "herald_diff": 0, "total_kills": 24, "total_objectives": 9}),
        ("Baron min 35, -4 kills behind",
         {"game_minute": 35, "kill_diff": -4, "tower_diff": -2, "drake_diff": -1, "baron_diff": 0,
          "inhib_diff": 0, "herald_diff": 0, "total_kills": 24, "total_objectives": 8},
         {"game_minute": 35, "kill_diff": -4, "tower_diff": -2, "drake_diff": -1, "baron_diff": 1,
          "inhib_diff": 0, "herald_diff": 0, "total_kills": 24, "total_objectives": 9}),
        ("Tower min 14, first tower",
         {"game_minute": 14, "kill_diff": 1, "tower_diff": 0, "drake_diff": 0, "baron_diff": 0,
          "inhib_diff": 0, "herald_diff": 0, "total_kills": 5, "total_objectives": 1},
         {"game_minute": 14, "kill_diff": 1, "tower_diff": 1, "drake_diff": 0, "baron_diff": 0,
          "inhib_diff": 0, "herald_diff": 0, "total_kills": 5, "total_objectives": 2}),
        ("Inhib min 30, 3 tower lead",
         {"game_minute": 30, "kill_diff": 5, "tower_diff": 3, "drake_diff": 1, "baron_diff": 1,
          "inhib_diff": 0, "herald_diff": 0, "total_kills": 22, "total_objectives": 10},
         {"game_minute": 30, "kill_diff": 5, "tower_diff": 3, "drake_diff": 1, "baron_diff": 1,
          "inhib_diff": 1, "herald_diff": 0, "total_kills": 22, "total_objectives": 10}),
        ("Kill when dominating 15-5 at min 20",
         {"game_minute": 20, "kill_diff": 10, "tower_diff": 3, "drake_diff": 1, "baron_diff": 0,
          "inhib_diff": 0, "herald_diff": 1, "total_kills": 20, "total_objectives": 5},
         {"game_minute": 20, "kill_diff": 11, "tower_diff": 3, "drake_diff": 1, "baron_diff": 0,
          "inhib_diff": 0, "herald_diff": 1, "total_kills": 21, "total_objectives": 5}),
        ("Kill when losing 5-15 at min 20",
         {"game_minute": 20, "kill_diff": -10, "tower_diff": -3, "drake_diff": -1, "baron_diff": 0,
          "inhib_diff": 0, "herald_diff": -1, "total_kills": 20, "total_objectives": 5},
         {"game_minute": 20, "kill_diff": -9, "tower_diff": -3, "drake_diff": -1, "baron_diff": 0,
          "inhib_diff": 0, "herald_diff": -1, "total_kills": 21, "total_objectives": 5}),
    ]

    for label, state_before, state_after in scenarios:
        X_before = np.array([[state_before[f] for f in FEATURES]])
        X_after = np.array([[state_after[f] for f in FEATURES]])

        p_before = model.predict_proba(X_before)[0, 1]
        p_after = model.predict_proba(X_after)[0, 1]
        delta = p_after - p_before

        print(f"  {label:<55} {p_before:>8.4f} {p_after:>8.4f} {delta:>+8.4f}")


def main():
    df = load_data()
    X = df[FEATURES].values
    y = df[LABEL].values
    groups = df[GROUP].values

    print(f"[train] features: {FEATURES}")
    print(f"[train] label distribution: {y.mean():.3f} (blue win rate across all frames)")
    print(f"[train] starting Optuna search ({N_TRIALS} trials, {N_FOLDS}-fold group CV)...")

    storage = optuna.storages.RDBStorage(OPTUNA_DB)
    try:
        optuna.delete_study(study_name=STUDY_NAME, storage=storage)
    except KeyError:
        pass
    study = optuna.create_study(
        study_name=STUDY_NAME,
        direction="minimize",
        sampler=optuna.samplers.TPESampler(seed=SEED),
        storage=storage,
    )
    print(f"[train] Optuna dashboard: optuna-dashboard sqlite:///{MODELS_DIR / 'optuna_study.db'}")
    study.optimize(lambda trial: objective(trial, X, y, groups), n_trials=N_TRIALS, show_progress_bar=True)

    best = study.best_trial
    print(f"\n[train] best trial #{best.number}: log-loss={best.value:.5f}")
    print(f"[train] best params: {json.dumps(best.params, indent=2)}")

    final_params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "verbosity": -1,
        "boosting_type": "gbdt",
        "seed": SEED,
        **best.params,
    }

    print("\n[train] calibration analysis (group CV with best params):")
    temp_model = lgb.LGBMClassifier(**final_params)
    logloss, auc, _, _ = calibration_analysis(temp_model, X, y, groups)

    print("\n[train] training final model on full dataset...")
    model = train_final_model(final_params, X, y)

    fi = pd.Series(model.feature_importances_, index=FEATURES).sort_values(ascending=False)
    print("\n  Feature Importance (split count):")
    for feat, imp in fi.items():
        bar = "█" * int(imp / fi.max() * 30)
        print(f"    {feat:<20} {imp:>6}  {bar}")

    print_event_impact_examples(model)

    model_path = MODELS_DIR / "winprob_lgbm.joblib"
    joblib.dump({"model": model, "features": FEATURES, "params": final_params}, model_path)
    print(f"\n[train] model saved to {model_path}")

    study_data = {
        "best_trial": best.number,
        "best_logloss": best.value,
        "cv_auc": auc,
        "best_params": best.params,
        "n_trials": N_TRIALS,
        "n_games": int(df[GROUP].nunique()),
        "n_rows": len(df),
        "features": FEATURES,
    }
    study_path = MODELS_DIR / "study.json"
    study_path.write_text(json.dumps(study_data, indent=2))
    print(f"[train] study results saved to {study_path}")


if __name__ == "__main__":
    main()
