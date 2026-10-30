import glob
import pandas as pd

df = pd.concat([pd.read_csv(f) for f in glob.glob("data/raw/*.csv")], ignore_index=True)
df["kbps"] = df["bytes_per_s"] * 8 / 1000

for thr in (0, 20, 50):
    print(f"\nRows per label with rate >= {thr} kbps")
    print(df[df["kbps"] >= thr]["label"].value_counts().to_string())

print("\nMedian rate (kbps) per label")
print(df.groupby("label")["kbps"].median().round(1).to_string())