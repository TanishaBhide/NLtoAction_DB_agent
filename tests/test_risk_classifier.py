from pathlib import Path
import json

from database.build_guardrail_dataset import build_dataset
from database.guardrails import GuardrailEngine
from database.llm_client import MockLLMClient
from database.risk_classifier import RiskClassifier, train
from database.sql_generator import ActionPlan, SQLGenerator


def test_classifier_trains_loads_and_returns_valid_probabilities(tmp_path: Path) -> None:
    dataset_path = tmp_path / "dataset.csv"
    model_path = tmp_path / "risk_model.joblib"
    dataset = build_dataset(dataset_path, seed=42)
    metrics = train(dataset_path, model_path, seed=42)
    classifier = RiskClassifier(model_path)
    plan = ActionPlan(
        action_type="read",
        sql="SELECT country FROM customers WHERE country = 'France' LIMIT 10",
        tables=["customers"],
        columns=["country"],
        explanation="Fixture.",
        confidence=1.0,
        count_sql=None,
        blocked=False,
        block_reason=None,
    )

    label, probabilities = classifier.predict(plan)

    assert len(dataset) == 240
    assert dataset["label"].value_counts().to_dict() == {"ALLOW": 80, "BLOCK": 80, "CONFIRM": 80}
    assert 0 <= metrics["macro_f1"] <= 1
    assert label in {"ALLOW", "CONFIRM", "BLOCK"}
    assert set(probabilities) == {"ALLOW", "CONFIRM", "BLOCK"}
    assert abs(sum(probabilities.values()) - 1.0) < 1e-8


def test_classifier_cannot_downgrade_rule_based_block() -> None:
    class AlwaysAllow:
        def predict(self, plan, context=None):
            return "ALLOW", {"ALLOW": 1.0}

    plan = ActionPlan(
        action_type="write",
        sql="DELETE FROM customers",
        tables=["customers"],
        columns=[],
        explanation="Fixture.",
        confidence=1.0,
        count_sql=None,
        blocked=False,
        block_reason=None,
    )
    result = GuardrailEngine(classifier=AlwaysAllow()).evaluate(plan)
    assert result.verdict == "BLOCK"


def test_required_safe_queries_remain_allowed_with_trained_classifier() -> None:
    root = Path(__file__).resolve().parents[1]
    cases = json.loads((root / "tests" / "representative_queries.json").read_text(encoding="utf-8"))
    selected = [
        case
        for case in cases
        if case["query"]
        in {
            "how many invoices per country",
            "show customers from France",
            "top 5 products by quantity sold",
        }
    ]
    generator = SQLGenerator(
        llm_client=MockLLMClient([case["mock_response"] for case in selected])
    )
    guardrails = GuardrailEngine(generator.metadata_path)

    for index, case in enumerate(selected):
        plan = generator.generate(case["query"], session_id=f"safe-risk-{index}")
        result = guardrails.evaluate(plan)
        assert result.verdict == "ALLOW", (case["query"], result.reasons)
