"""Week 9: session-level intent drift detection (no database needed)."""

from __future__ import annotations

from types import SimpleNamespace

from database.eval_week9 import eval_drift
from database.intent_drift import IntentDriftDetector, tokens

DETECTOR = IntentDriftDetector()


def turn(request, sql, rows=1):
    write = not sql.upper().startswith("SELECT")
    return SimpleNamespace(request=request, final_sql=sql, action_type="write" if write else "read",
                           row_count=None if write else rows, affected_rows=rows if write else None, tables=[])


READ_536365 = turn("show the line items of invoice 536365",
                   "SELECT stock_code, quantity FROM invoice_items WHERE invoice_no = '536365' LIMIT 100", 7)
UPDATE_A = turn("set the quantity of stock code 85123A on invoice 536365 to 12",
                "UPDATE invoice_items SET quantity = 12 WHERE invoice_no = '536365' AND stock_code = '85123A'")


def test_first_request_has_no_baseline():
    result = DETECTOR.assess("delete them", "write", "DELETE FROM invoice_items WHERE invoice_no = '1'", [])
    assert result.level == "NO_BASELINE" and not result.escalate and result.score == 0


def test_follow_up_that_turns_a_read_into_a_delete_escalates():
    result = DETECTOR.assess("delete them", "write", "DELETE FROM invoice_items WHERE invoice_no = '536365'",
                             [READ_536365], follow_up=True, estimated_rows=7)
    assert result.level == "DRIFT" and result.escalate
    assert result.features["action_escalation"] == 1.0 and result.features["follow_up_flip"] == 1.0
    assert any("follow-up" in reason for reason in result.reasons)


def test_repeating_an_established_write_workflow_is_stable():
    result = DETECTOR.assess(
        "set the quantity of stock code 71053 on invoice 536365 to 4", "write",
        "UPDATE invoice_items SET quantity = 4 WHERE invoice_no = '536365' AND stock_code = '71053'",
        [UPDATE_A], estimated_rows=1)
    assert result.level == "STABLE" and not result.escalate


def test_inspect_then_edit_same_entity_is_not_escalated():
    result = DETECTOR.assess(
        "set the quantity of stock code 85123A on invoice 536365 to 12", "write", UPDATE_A.final_sql,
        [READ_536365], estimated_rows=1)
    assert not result.escalate


def test_reads_never_escalate_even_when_they_drift():
    history = [UPDATE_A, UPDATE_A]
    result = DETECTOR.assess("show customer ids", "read", "SELECT customer_id FROM customers LIMIT 5", history,
                             follow_up=True)
    assert not result.escalate


def test_first_write_of_customer_identifiers_counts_as_sensitivity_escalation():
    history = [turn("show all products", "SELECT stock_code, description FROM products LIMIT 100", 13)]
    result = DETECTOR.assess("change customer 17850's id to 99999", "write",
                             "UPDATE customers SET customer_id = '99999' WHERE customer_id = '17850'", history)
    assert result.features["sensitivity_escalation"] == 1.0
    assert result.features["table_novelty"] == 1.0
    assert result.escalate


def test_scope_jump_is_scored_on_a_log_scale():
    history = [turn("show big items", "SELECT quantity FROM invoice_items WHERE quantity > 10 LIMIT 100", 40)]
    result = DETECTOR.assess("cap them", "write", "UPDATE invoice_items SET quantity = 10 WHERE quantity > 10",
                             history, estimated_rows=4000)
    assert result.features["scope_escalation"] == 1.0  # 100x the typical size


def test_disabled_feature_contributes_nothing():
    detector = IntentDriftDetector(disabled_features=["follow_up_flip"])
    result = detector.assess("delete them", "write", "DELETE FROM invoice_items WHERE invoice_no = '536365'",
                             [READ_536365], follow_up=True)
    assert result.contributions["follow_up_flip"] == 0


def test_tokens_drop_stopwords_and_plural_s():
    assert tokens("Show the products with hearts") == tokens("product heart")


def test_labelled_dataset_regression():
    """Guards the published Week 9 numbers: no false alarms, recall at least 0.7."""
    report = eval_drift()["detector"]
    assert report["fp"] == 0
    assert report["recall"] >= 0.7 and report["f1"] >= 0.8
