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

# Production features match what LLF provides live: no gold, no heralds.
# Momentum features (kill_diff_delta_3m, peak_kill_diff, lead_retraction,
# obj_diff_delta_3m) capture trajectory, not just snapshot.
# comp_diff is blue_team_avg_champ_winrate - red_team_avg_champ_winrate,
# LOO-adjusted and shrunk toward the global mean.
FEATURES_LIVE = [
    "game_minute",
    "kill_diff",
    "tower_diff",
    "drake_diff",
    "baron_diff",
    "inhib_diff",
    "total_kills",
    "total_objectives",
    # Momentum
    "kill_diff_delta_3m",
    "obj_diff_delta_3m",
    "peak_kill_diff",
    "lead_retraction",
    # Composition
    "comp_diff",
]

FEATURES = FEATURES_LIVE

# Monotonicity: more diff in blue's favor → higher P(blue wins).
# Momentum features are unconstrained — "delta" can legitimately cut either
# way conditional on other features (e.g. losing a lead near minute 30 is
# different from losing it at minute 5). Totals are unconstrained too.
# comp_diff is monotone +1: better comp for blue must not decrease P(blue).
MONOTONE_CONSTRAINTS = [
    0,   # game_minute
    1,   # kill_diff
    1,   # tower_diff
    1,   # drake_diff
    1,   # baron_diff
    1,   # inhib_diff
    0,   # total_kills
    0,   # total_objectives
    0,   # kill_diff_delta_3m
    0,   # obj_diff_delta_3m
    0,   # peak_kill_diff
    0,   # lead_retraction
    1,   # comp_diff — blue comp stronger → higher P(blue wins)
]

# Baseline subset for A/B comparison — same features as the currently-deployed
# model. If the new FEATURES fails to beat this on CV log-loss, we don't ship.
FEATURES_BASELINE = [
    "game_minute",
    "kill_diff",
    "tower_diff",
    "drake_diff",
    "baron_diff",
    "inhib_diff",
    "herald_diff",
    "total_kills",
    "total_objectives",
]
MONOTONE_BASELINE = [0, 1, 1, 1, 1, 1, 1, 0, 0]

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


def objective(trial: optuna.Trial, X: np.ndarray, y: np.ndarray, groups: np.ndarray,
              monotone: list[int]) -> float:
    params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "verbosity": -1,
        "boosting_type": "gbdt",
        "seed": SEED,
        "monotone_constraints": monotone,
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


def train_one(label: str, features: list[str], monotone: list[int], df: pd.DataFrame,
              study_name: str, output_stem: str, n_trials: int = N_TRIALS) -> dict:
    """Run Optuna + final train for one feature set. Returns summary dict."""
    X = df[features].values
    y = df[LABEL].values
    groups = df[GROUP].values

    print(f"\n{'='*70}\n[{label}] features ({len(features)}): {features}")
    print(f"[{label}] {len(df):,} rows, {df[GROUP].nunique():,} games, blue rate={y.mean():.3f}")
    print(f"[{label}] Optuna: {n_trials} trials, {N_FOLDS}-fold group CV")

    storage = optuna.storages.RDBStorage(OPTUNA_DB)
    try:
        optuna.delete_study(study_name=study_name, storage=storage)
    except KeyError:
        pass
    study = optuna.create_study(
        study_name=study_name,
        direction="minimize",
        sampler=optuna.samplers.TPESampler(seed=SEED),
        storage=storage,
    )
    study.optimize(
        lambda trial: objective(trial, X, y, groups, monotone),
        n_trials=n_trials, show_progress_bar=True,
    )
    best = study.best_trial
    print(f"\n[{label}] best trial #{best.number}: CV log-loss = {best.value:.5f}")

    final_params = {
        "objective": "binary", "metric": "binary_logloss", "verbosity": -1,
        "boosting_type": "gbdt", "seed": SEED,
        "monotone_constraints": monotone,
        **best.params,
    }

    print(f"\n[{label}] calibration analysis:")
    temp_model = lgb.LGBMClassifier(**final_params)
    logloss, auc, _, _ = calibration_analysis(temp_model, X, y, groups)

    print(f"\n[{label}] training final model on full dataset...")
    model = train_final_model(final_params, X, y)

    fi = pd.Series(model.feature_importances_, index=features).sort_values(ascending=False)
    print(f"\n  [{label}] Feature Importance (split count):")
    for feat, imp in fi.items():
        bar = "█" * int(imp / fi.max() * 30)
        print(f"    {feat:<22} {imp:>6}  {bar}")

    model_path = MODELS_DIR / f"{output_stem}.joblib"
    joblib.dump({"model": model, "features": features, "params": final_params}, model_path)
    print(f"\n[{label}] saved model → {model_path}")

    study_data = {
        "label": label, "best_trial": best.number, "best_logloss": best.value,
        "cv_auc": auc, "best_params": best.params, "n_trials": n_trials,
        "n_games": int(df[GROUP].nunique()), "n_rows": len(df),
        "features": features, "monotone_constraints": monotone,
    }
    study_path = MODELS_DIR / f"{output_stem}_study.json"
    study_path.write_text(json.dumps(study_data, indent=2))
    return study_data


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("ab", "baseline", "live"), default="ab",
                    help="ab=train both for comparison, baseline=just old feats, live=just new feats")
    ap.add_argument("--trials", type=int, default=N_TRIALS)
    args = ap.parse_args()

    df = load_data()
    summaries = []

    if args.mode in ("ab", "baseline"):
        summaries.append(train_one(
            label="baseline", features=FEATURES_BASELINE, monotone=MONOTONE_BASELINE,
            df=df, study_name=f"{STUDY_NAME}_baseline",
            output_stem="winprob_lgbm_baseline", n_trials=args.trials,
        ))

    if args.mode in ("ab", "live"):
        summaries.append(train_one(
            label="live_v2", features=FEATURES_LIVE, monotone=MONOTONE_CONSTRAINTS,
            df=df, study_name=f"{STUDY_NAME}_live", output_stem="winprob_lgbm_v2",
            n_trials=args.trials,
        ))

    if len(summaries) == 2:
        b, v = summaries
        print(f"\n{'='*70}\n[A/B COMPARISON]")
        print(f"  {'label':<15}{'CV log-loss':>14}{'CV AUC':>10}{'features':>11}")
        for s in summaries:
            print(f"  {s['label']:<15}{s['best_logloss']:>14.5f}{s['cv_auc']:>10.5f}{len(s['features']):>11}")
        d_ll = v["best_logloss"] - b["best_logloss"]
        d_auc = v["cv_auc"] - b["cv_auc"]
        print(f"\n  Δ log-loss (live − baseline): {d_ll:+.5f}  ({'BETTER' if d_ll < 0 else 'worse'})")
        print(f"  Δ AUC      (live − baseline): {d_auc:+.5f}  ({'BETTER' if d_auc > 0 else 'worse'})")
        if d_ll < -0.001 and d_auc > 0.001:
            print("\n  → New model beats baseline. Safe to promote (update event_impact.py + deploy).")
        elif d_ll < 0 and d_auc > 0:
            print("\n  → New model marginally better. Look at calibration + feature importance before promoting.")
        else:
            print("\n  → New model does NOT beat baseline. Do not promote. Investigate features.")


if __name__ == "__main__":
    main()
