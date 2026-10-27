"""Build a deterministic, balanced, template-derived Week 5 training dataset."""

from __future__ import annotations

from pathlib import Path
import random

import pandas as pd

try:
    from .risk_features import FEATURE_COLUMNS
except ImportError:
    from risk_features import FEATURE_COLUMNS


ROOT = Path(__file__).parent
DEFAULT_OUTPUT = ROOT / "guardrail_dataset.csv"
LABELS = ("ALLOW", "CONFIRM", "BLOCK")
REAL_IDENTIFIERS = {
    "ALLOW": ("customers", "country"),
    "CONFIRM": ("customers", "customer_id"),
    "BLOCK": ("invoices", "customer_id"),
}


def build_dataset(output_path: Path = DEFAULT_OUTPUT, seed: int = 2025) -> pd.DataFrame:
    rng = random.Random(seed)
    rows: list[dict[str, object]] = []
    examples = {
        "ALLOW": [
            ("read", 1, 0, True, False, False, False, False, 12, True, False),
            ("read", 2, 1, True, False, False, False, False, 45, True, False),
            ("read", 3, 2, True, False, False, False, True, 90, True, False),
            ("read", 2, 1, True, False, False, False, True, 35, True, True),
            ("read", 1, 0, True, False, True, False, False, 20, True, False),
            ("read", 2, 1, False, False, False, False, True, 30, True, False),
            ("read", 3, 2, False, False, False, False, True, 45, True, False),
            ("write", 1, 0, True, False, False, False, False, 1, False, False),
            ("write", 1, 0, True, False, False, False, False, 5, False, False),
            ("write", 2, 1, True, False, False, False, False, 10, False, False),
            ("write", 1, 0, True, False, False, False, False, 3, False, False),
            ("write", 1, 0, True, False, False, False, False, 8, False, False),
            ("write", 1, 0, True, False, False, False, False, 12, False, False),
        ],
        "CONFIRM": [
            ("read", 1, 0, False, False, True, False, False, 0, True, False),
            ("read", 1, 0, True, False, True, False, False, 40, False, False),
            ("read", 1, 0, False, False, False, True, False, 0, True, False),
            ("write", 1, 0, True, False, False, False, False, 150, False, False),
            ("write", 2, 1, True, False, True, False, False, 30, False, False),
            ("read", 2, 1, False, False, True, False, True, 0, True, False),
        ],
        "BLOCK": [
            ("write", 1, 0, False, False, False, False, False, 800, False, False),
            ("write", 1, 0, True, True, False, False, False, 50, False, False),
            ("write", 2, 1, True, False, False, False, False, 1500, False, False),
            ("ddl", 1, 0, False, False, False, False, False, 0, False, False),
            ("read", 2, 1, True, True, False, False, False, 50, True, False),
        ],
    }
    for label in LABELS:
        samples = examples[label]
        for index in range(80):
            base = samples[index % len(samples)]
            row = dict(zip(FEATURE_COLUMNS, base, strict=True))
            if label == "ALLOW":
                row["estimated_rows"] = (
                    rng.randint(0, 25) if row["action_type"] == "write" else rng.randint(0, 100)
                )
                if row["action_type"] == "read":
                    row["has_limit"] = True
            elif label == "CONFIRM":
                if row["action_type"] == "write" and row["estimated_rows"] == 0:
                    row["estimated_rows"] = rng.randint(101, 1000)
            else:
                if row["estimated_rows"] < 1001 and row["action_type"] != "ddl":
                    row["estimated_rows"] = rng.randint(1001, 5000)
            if label == "ALLOW" and row["action_type"] == "write":
                row["table_name"], row["column_name"] = (
                    ("products", "description"),
                    ("invoice_items", "unit_price"),
                    ("invoices", "status"),
                )[index % 3]
            else:
                row["table_name"], row["column_name"] = REAL_IDENTIFIERS[label]
            row["label"] = label
            rows.append(row)
    rng.shuffle(rows)
    dataset = pd.DataFrame(rows, columns=[*FEATURE_COLUMNS, "table_name", "column_name", "label"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    dataset.to_csv(output_path, index=False)
    return dataset


def main() -> int:
    dataset = build_dataset()
    print(f"Wrote {len(dataset)} rows to {DEFAULT_OUTPUT}")
    print(dataset["label"].value_counts().sort_index().to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
