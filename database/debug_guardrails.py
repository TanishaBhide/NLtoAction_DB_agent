"""Print the generated plan and each Week 5 guardrail decision."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from typing import Iterable

try:
    from .guardrails import GuardrailEngine, POLICY_RULES
    from .sql_generator import SQLGenerator
except ImportError:
    from guardrails import GuardrailEngine, POLICY_RULES
    from sql_generator import SQLGenerator


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Debug plan identifiers and guardrail decisions.")
    parser.add_argument("request", help="Natural-language database request")
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--no-db", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    generator = SQLGenerator()
    plan = generator.generate(args.request, session_id=args.session_id)
    validation = generator.validator.validate(
        plan.sql,
        plan.action_type,
        require_limit=False,
    ) if plan.sql else None
    engine = GuardrailEngine(generator.metadata_path)
    result = engine.evaluate(
        plan,
        context={"session_id": args.session_id, "no_db": args.no_db},
    )
    active = {rule["rule_type"]: rule for rule in result.triggered_rules}
    output = {
        "plan": asdict(plan),
        "extracted_identifiers": {
            "tables": validation.tables if validation else [],
            "columns": validation.columns if validation else [],
            "column_references": validation.column_references if validation else [],
            "projection_column_references": validation.projection_column_references if validation else [],
            "wildcard_tables": validation.wildcard_tables if validation else [],
            "validation_errors": validation.errors if validation else [],
        },
        "rule_decisions": [
            {
                "rule_type": rule_type,
                "decision": "TRIGGERED" if rule_type in active else "NOT_TRIGGERED",
                "reason": active[rule_type]["reason"] if rule_type in active else None,
            }
            for rule_type in POLICY_RULES
        ],
        "classifier": {
            "label": result.classifier_label,
            "probabilities": result.classifier_probabilities,
        },
        "final_result": asdict(result),
    }
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
