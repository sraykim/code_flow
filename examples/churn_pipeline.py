"""Train a churn model from raw customer events and write a scored file."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split

# --- settings ---
DATA_DIR = Path("data")
RAW_PATH = DATA_DIR / "events.csv"
STAGING_PATH = DATA_DIR / "staging.parquet"
SEED = 42

LABEL = "churned"


def read_config(path):
    """Load model + feature settings."""
    with open(path) as f:
        return json.load(f)


def ingest_events(path):
    df = pd.read_csv(path, parse_dates=["event_time"])
    df.to_parquet(STAGING_PATH)
    return len(df)


def load_customers(customer_file):
    return pd.read_csv(customer_file)


def _days_between(a, b):
    return (b - a).dt.days


def build_features(customers, config):
    events = pd.read_parquet(STAGING_PATH)
    last_seen = events.groupby("customer_id")["event_time"].max()
    feats = customers.set_index("customer_id")
    feats["recency"] = _days_between(last_seen, pd.Timestamp(config["as_of"]))
    feats["frequency"] = events.groupby("customer_id").size()
    return feats.fillna(0)


def split(features, test_size):
    X = features.drop(columns=[LABEL])
    y = features[LABEL]
    return train_test_split(X, y, test_size=test_size, random_state=SEED)


def train_model(X, y, params):
    model = GradientBoostingClassifier(random_state=SEED, **params)
    model.fit(X, y)
    return model


def evaluate(model, X, y):
    return roc_auc_score(y, model.predict_proba(X)[:, 1])


def unused_debug_helper(df):
    return df.head()


# --- pipeline ---
config = read_config("config.json")
n_events = ingest_events(RAW_PATH)
customers = load_customers(config["customer_file"])

features = build_features(customers, config)
X_train, X_test, y_train, y_test = split(features, config["test_size"])

model = train_model(X_train, y_train, config["model_params"])
auc = evaluate(model, X_test, y_test)
print(f"events={n_events} auc={auc:.3f}")

if auc > config["min_auc"]:
    scores = model.predict_proba(features.drop(columns=[LABEL]))[:, 1]
    out = pd.DataFrame({"customer_id": features.index, "score": np.round(scores, 4)})
    out.to_csv(DATA_DIR / "scores.csv", index=False)
