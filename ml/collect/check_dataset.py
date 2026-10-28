import glob
import pandas as pd

files = sorted(glob.glob("data/raw/*.csv"))
if not files:
    raise SystemExit("No CSV files found in data/raw/. Run pcap_to_csv.py first.")

print(f"Found {len(files)} CSV files:")
for f in files:
    print("  ", f)

df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
print(f"\nTotal rows: {len(df)}\n")

print("Rows per label:")
print(df["label"].value_counts(), "\n")

print("Sessions and apps per label:")
print(df.groupby("label").agg(
    sessions=("session_id", "nunique"),
    apps=("app", "nunique"),
))

print("\nAverage of key features per label (sanity check):")
cols = ["bytes_per_s", "packets_per_s", "up_byte_share", "up_mean_ps", "down_mean_ps"]
print(df.groupby("label")[cols].mean().round(1))