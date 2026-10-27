"""Explainable logistic-regression risk classifier for SQL action plans."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import joblib
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.metrics import classification_report, f1_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.linear_model import LogisticRegression

try:
    from .build_guardrail_dataset import DEFAULT_OUTPUT, build_dataset
    from .risk_features import FEATURE_COLUMNS, extract_features
    from .sql_validator import SQLValidator
except ImportError:
    from build_guardrail_dataset import DEFAULT_OUTPUT, build_dataset
    from risk_features import FEATURE_COLUMNS, extract_features
    from sql_validator import SQLValidator


ROOT = Path(__file__).parent
DEFAULT_MODEL = ROOT.parent / "models" / "risk_model.joblib"
NUMERIC_FEATURES = [feature for feature in FEATURE_COLUMNS if feature != "action_type"]


def _new_model() -> Pipeline:
    preprocessing = ColumnTransformer(
        [
            ("action_type", OneHotEncoder(handle_unknown="ignore"), ["action_type"]),
            ("numeric", StandardScaler(), NUMERIC_FEATURES),
        ]
    )
    return Pipeline(
        [
            ("features", preprocessing),
            ("classifier", LogisticRegression(max_iter=2000, random_state=2025)),
        ]
    )


class RiskClassifier:
    def __init__(
        self,
        model_path: Path = DEFAULT_MODEL,
        model: Pipeline | None = None,
        validator: SQLValidator | None = None,
    ) -> None:
        self.model_path = Path(model_path)
        self.validator = validator or SQLValidator()
        self.model = model if model is not None else joblib.load(self.model_path)["model"]

    def predict(self, plan: Any, context: dict[str, Any] | None = None) -> tuple[str, dict[str, float]]:
        features = extract_features(plan, context, self.validator)
        probabilities = self.model.predict_proba(pd.DataFrame([features], columns=FEATURE_COLUMNS))[0]
        classes = [str(label) for label in self.model.classes_]
        result = {label: float(probability) for label, probability in zip(classes, probabilities, strict=True)}
        label = max(result, key=result.get)
        return label, result


def train(
    dataset_path: Path = DEFAULT_OUTPUT,
    model_path: Path = DEFAULT_MODEL,
    seed: int = 2025,
) -> dict[str, Any]:
    if not dataset_path.exists():
        build_dataset(dataset_path, seed=seed)
    dataset = pd.read_csv(dataset_path)
    features = dataset[FEATURE_COLUMNS]
    labels = dataset["label"]
    x_train, x_test, y_train, y_test = train_test_split(
        features,
        labels,
        test_size=0.25,
        random_state=seed,
        stratify=labels,
    )
    evaluation_model = _new_model()
    evaluation_model.fit(x_train, y_train)
    predictions = evaluation_model.predict(x_test)
    report = classification_report(
        y_test,
        predictions,
        labels=["ALLOW", "CONFIRM", "BLOCK"],
        output_dict=True,
        zero_division=0,
    )
    macro_f1 = float(f1_score(y_test, predictions, average="macro"))

    final_model = _new_model()
    final_model.fit(features, labels)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    metrics = {"macro_f1": macro_f1, "classification_report": report}
    joblib.dump(
        {
            "model": final_model,
            "metrics": metrics,
            "feature_columns": FEATURE_COLUMNS,
            "seed": seed,
        },
        model_path,
    )
    print(json.dumps(metrics, indent=2))
    print(f"Saved model to {model_path}")
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description="Train the Week 5 guardrail risk classifier.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    args = parser.parse_args()
    train(args.dataset, args.model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
