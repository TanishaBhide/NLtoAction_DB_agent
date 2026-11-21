"""Larger dataset generator, .env loading and the LLM fallback (no database needed)."""

from __future__ import annotations

import os
import re

from database.env import load_env
from database.generate_retail_data import SAMPLE_INVOICES, generate
from database.llm_client import LLMUnavailableError
from database.rule_planner import OfflinePlannerClient
from database.sql_generator import SQLGenerator


def test_generator_is_deterministic_and_hits_the_requested_size():
    sql, stats = generate(2000)
    assert generate(2000)[0] == sql
    assert stats["total_invoice_items"] == 2000
    item_rows = re.findall(r"^    \('([AC]?\d+)', '([^']+)', (-?\d+), (-?\d+\.\d\d)\)", sql, re.MULTILINE)
    assert len(item_rows) == stats["invoice_items"]


def test_generated_rows_respect_the_business_rules():
    sql, _ = generate(2000)
    items = re.findall(r"^    \('([AC]?\d+)', '([^']+)', (-?\d+), (-?\d+\.\d\d)\)", sql, re.MULTILINE)
    invoices = set(re.findall(r"^    \('([AC]?\d+)', '\d{4}-\d\d-\d\d \d\d:\d\d'", sql, re.MULTILINE))
    assert not invoices & SAMPLE_INVOICES                                  # sample rows stay untouched
    assert {number for number, *_ in items} == invoices                    # every invoice has line items
    for number, _, qty, price in items:
        assert int(qty) != 0
        assert int(qty) > 0 or number.startswith("C")                       # negative qty only on cancellations
        assert float(price) >= 0 or number.startswith("A")                  # negative price only on adjustments


def test_env_loader_parses_spaces_quotes_and_keeps_existing_values(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nLLM_PROVIDER = groq\nexport TEST_QUOTED="a b"\nTEST_KEEP=from-file\nTEST_X=1 # note\n',
                   encoding="utf-8")
    for key in ("LLM_PROVIDER", "TEST_QUOTED", "TEST_X"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TEST_KEEP", "from-env")
    loaded = load_env(env)
    assert os.environ["LLM_PROVIDER"] == "groq" and os.environ["TEST_QUOTED"] == "a b" and os.environ["TEST_X"] == "1"
    assert os.environ["TEST_KEEP"] == "from-env" and "TEST_KEEP" not in loaded
    for key in ("LLM_PROVIDER", "TEST_QUOTED", "TEST_X"):
        monkeypatch.delenv(key, raising=False)


def test_llm_outage_falls_back_to_the_offline_planner():
    from webapp.server import FallbackPlannerClient

    class Down:
        def complete(self, *args, **kwargs):
            raise LLMUnavailableError("rate limited")

    generator = SQLGenerator(llm_client=FallbackPlannerClient(Down(), OfflinePlannerClient(), "Groq LLM"))
    plan = generator.generate("show customers from France")
    assert not plan.blocked and "WHERE customers.country = 'France'" in plan.sql
    assert plan.explanation.startswith("[LLM unavailable: rate limited; offline planner used]")


def test_live_plans_are_labelled_with_the_model():
    from webapp.server import FallbackPlannerClient

    class Up:
        def complete(self, system, user, tool_schema, *, context=None):
            return OfflinePlannerClient().complete(system, user, tool_schema, context=context)

    plan = SQLGenerator(llm_client=FallbackPlannerClient(Up(), OfflinePlannerClient(), "Groq LLM · m")).generate(
        "show customers from France")
    assert plan.explanation.startswith("[Groq LLM · m]")
