"""Persist guardrail assessments and approvals, with an offline memory mode."""

from __future__ import annotations

import os
from dataclasses import asdict
from typing import Any

try:
    from .guardrails import GuardrailResult
except ImportError:
    from guardrails import GuardrailResult


class GuardrailStore:
    def __init__(self, *, in_memory: bool = False) -> None:
        self.in_memory = in_memory
        self.assessments: list[dict[str, Any]] = []

    @staticmethod
    def _connection_options() -> tuple[str | None, dict[str, str]]:
        database_url = os.environ.get("DATABASE_URL")
        options = {
            key: os.environ[variable]
            for variable, key in (
                ("PGHOST", "host"),
                ("PGPORT", "port"),
                ("PGDATABASE", "dbname"),
                ("PGUSER", "user"),
                ("PGPASSWORD", "password"),
            )
            if os.environ.get(variable)
        }
        return database_url, options

    def save_assessment(self, session_id: str | None, result: GuardrailResult) -> int:
        if self.in_memory:
            assessment_id = len(self.assessments) + 1
            record = {"assessment_id": assessment_id, "session_id": session_id, **asdict(result)}
            if result.verdict == "CONFIRM":
                record["approval_request"] = {"status": "PENDING", "resolved_at": None}
            self.assessments.append(record)
            return assessment_id

        database_url, options = self._connection_options()
        if not database_url and not options.get("dbname"):
            assessment_id = len(self.assessments) + 1
            record = {"assessment_id": assessment_id, "session_id": session_id, **asdict(result)}
            if result.verdict == "CONFIRM":
                record["approval_request"] = {"status": "PENDING", "resolved_at": None}
            self.assessments.append(record)
            return assessment_id

        import psycopg2

        connection = psycopg2.connect(database_url) if database_url else psycopg2.connect(**options)
        try:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO risk_assessment (session_id, risk_score, verdict)
                        VALUES (%s, %s, %s)
                        RETURNING assessment_id
                        """,
                        (session_id, result.risk_score, result.verdict),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise RuntimeError("Risk assessment insert did not return an assessment_id.")
                    assessment_id = int(row[0])
                    for rule in result.triggered_rules:
                        cursor.execute(
                            """
                            INSERT INTO checked_against (assessment_id, rule_id)
                            VALUES (%s, %s)
                            """,
                            (assessment_id, int(rule["rule_id"])),
                        )
                    if result.verdict == "CONFIRM":
                        cursor.execute(
                            """
                            INSERT INTO approval_request (assessment_id, status)
                            VALUES (%s, %s)
                            """,
                            (assessment_id, "PENDING"),
                        )
                    return assessment_id
        finally:
            connection.close()
