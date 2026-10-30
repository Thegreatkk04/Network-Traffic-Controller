"""Loads the trained model and labels a flow's feature row."""
import joblib
import numpy as np
import pandas as pd


class Classifier:
    def __init__(self, path="ml/models/model.pkl"):
        bundle = joblib.load(path)
        self.model = bundle["model"]
        self.features = bundle["features"]     # same columns, same order as training
        self.labels = list(bundle["labels"])

    def predict(self, row_df):
        """row_df: DataFrame with one row of features. Returns (label, confidence)."""
        X = (row_df.reindex(columns=self.features)
                   .replace([np.inf, -np.inf], np.nan)
                   .fillna(0))
        proba = self.model.predict_proba(X)[0]
        i = int(np.argmax(proba))
        return self.labels[i], float(proba[i])