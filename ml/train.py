#!/usr/bin/env python3
"""Train and evaluate the traffic classifier.

  python ml/train.py
  python ml/train.py --min-kbps 20     # drop very quiet (background) rows
"""
import argparse
import glob

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix
from sklearn.tree import DecisionTreeClassifier

META = ["session_id", "label", "app", "note"]


def load(min_kbps):
    files = glob.glob("data/raw/*.csv")
    if not files:
        raise SystemExit("No CSV files in data/raw/")
    df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    df = df.replace([np.inf, -np.inf], np.nan).fillna(0)
    if min_kbps > 0:
        before = len(df)
        df = df[df["bytes_per_s"] * 8 / 1000 >= min_kbps]
        print(f"--min-kbps {min_kbps}: kept {len(df)} of {before} rows")
    return df.reset_index(drop=True)


def split(df, test_frac=0.3, gap_frac=0.05):
    """Whole-session split if possible, otherwise a time-block split."""
    sessions = df.groupby("label")["session_id"].nunique()
    if (sessions >= 2).all():
        rng = np.random.RandomState(42)
        test_ids = []
        for _, g in df.groupby("label"):
            ids = sorted(g["session_id"].unique())
            n_test = max(1, int(round(len(ids) * test_frac)))
            test_ids += list(rng.choice(ids, n_test, replace=False))
        is_test = df["session_id"].isin(test_ids)
        print("Split: WHOLE SESSIONS held out ->", sorted(test_ids))
        return df[~is_test], df[is_test]

    print("WARNING: some class has only 1 session, so a time-block split is used.")
    print("         Results are optimistic. Record more sessions per class.")
    train_parts, test_parts = [], []
    for _, g in df.groupby("session_id", sort=False):
        n = len(g)
        cut = int(n * (1 - test_frac))
        gap = int(n * gap_frac)
        train_parts.append(g.iloc[: max(cut - gap, 0)])
        test_parts.append(g.iloc[cut:])
    return pd.concat(train_parts), pd.concat(test_parts)


def evaluate(name, model, Xtr, ytr, Xte, yte):
    model.fit(Xtr, ytr)
    pred = model.predict(Xte)
    print(f"\n===== {name} =====")
    print(f"Accuracy: {accuracy_score(yte, pred):.3f}")
    print(classification_report(yte, pred, zero_division=0))
    labels = sorted(set(yte) | set(pred))
    cm = pd.DataFrame(confusion_matrix(yte, pred, labels=labels),
                      index=[f"true_{l}" for l in labels],
                      columns=[f"pred_{l}" for l in labels])
    print("Confusion matrix (rows = truth):")
    print(cm)
    return model


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-kbps", type=float, default=0.0)
    args = ap.parse_args()

    df = load(args.min_kbps)
    features = [c for c in df.columns if c not in META]

    print("\nRows per label:\n", df["label"].value_counts().to_string())
    print("Sessions per label:\n", df.groupby("label")["session_id"].nunique().to_string())

    train, test = split(df)
    print(f"\nTrain rows: {len(train)}   Test rows: {len(test)}")
    missing = set(train["label"]) ^ set(test["label"])
    if missing:
        print("WARNING: these classes are missing from train or test:", missing)

    Xtr, ytr = train[features], train["label"]
    Xte, yte = test[features], test["label"]

    evaluate("Decision Tree (baseline)",
             DecisionTreeClassifier(max_depth=6, random_state=0), Xtr, ytr, Xte, yte)

    rf = evaluate("Random Forest",
                  RandomForestClassifier(n_estimators=200, class_weight="balanced",
                                         min_samples_leaf=2, random_state=0, n_jobs=-1),
                  Xtr, ytr, Xte, yte)

    # Is the model's confidence trustworthy? The policy relies on it.
    conf = rf.predict_proba(Xte).max(axis=1)
    pred = rf.predict(Xte)
    print("\n===== Confidence check (Random Forest) =====")
    for thr in (0.5, 0.6, 0.7, 0.8):
        keep = conf >= thr
        if keep.any():
            acc = accuracy_score(yte[keep], pred[keep])
            print(f"confidence >= {thr}: covers {keep.mean():.0%} of rows, accuracy {acc:.3f}")

    imp = pd.Series(rf.feature_importances_, index=features).sort_values(ascending=False)
    print("\nTop 10 features:\n", imp.head(10).round(3).to_string())

    # Final model for the live system: train on ALL data
    final = RandomForestClassifier(n_estimators=200, class_weight="balanced",
                                   min_samples_leaf=2, random_state=0, n_jobs=-1)
    final.fit(df[features], df["label"])
    import os
    os.makedirs("ml/models", exist_ok=True)
    joblib.dump({"model": final, "features": features, "labels": list(final.classes_)},
                "ml/models/model.pkl")
    print("\nSaved ml/models/model.pkl")