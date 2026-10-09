import json
import pandas as pd


def read_config(path):
    with open(path) as f:
        return json.load(f)


def ingest_data(source):
    return pd.read_csv(source)


def preprocess_data(df, config):
    df = df.dropna(subset=config["required_columns"])
    return df[df["amount"] > config["min_amount"]]


raw = ingest_data("data/transactions.csv")
cfg = read_config("config.json")
clean = preprocess_data(raw, cfg)
print(clean.shape)
