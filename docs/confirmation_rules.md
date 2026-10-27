# Week 5 confirmation and blocking rules

| Verdict | Behaviour |
| --- | --- |
| `ALLOW` | The plan may proceed to a later execution stage. Week 5 does not execute it. |
| `CONFIRM` | Create a `PENDING` `approval_request`; execution must wait for approval. |
| `BLOCK` | Reject the plan and report the reasons. It cannot be approved through this flow. |

| Rule | Trigger | Outcome |
| --- | --- | --- |
| `WHERE_REQUIRED` | `UPDATE` or `DELETE` has no `WHERE` clause. | `BLOCK` |
| `ROW_LIMIT` | A write affects more than 100 rows, more than 1,000 rows, or its count cannot be determined. | `CONFIRM` above 100; `BLOCK` above 1,000; `CONFIRM` if unavailable |
| `PII_COLUMN` | `SELECT *` includes a table with HIGH-sensitivity columns; raw HIGH-sensitivity rows are returned without both a filter and a `LIMIT`; or a write touches a HIGH-sensitivity column. | `CONFIRM` |
| `DDL_BLOCK` | DDL or privilege-changing SQL (`DROP`, `TRUNCATE`, `ALTER`, `GRANT`, etc.). | `BLOCK` |
| `MULTI_STATEMENT` | Stacked statements or SQL comments are present. | `BLOCK` |
| `WRITE_TAUTOLOGY` | An `UPDATE` or `DELETE` predicate contains a tautology such as `1=1`. | `BLOCK` |
| Read-limit warning | A non-aggregate read has no `LIMIT`. | Adds risk but does not directly change the verdict. |
| Classifier escalation | Highest class probability is below 0.60, or the classifier predicts a stricter class than the rules. | Escalates to `CONFIRM` or `BLOCK`; never downgrades a rule verdict. |

The write row-count thresholds are fixed at 100 rows for confirmation and 1,000
rows for blocking. Literal `INSERT ... VALUES` rows are counted from the SQL
syntax tree; an `INSERT ... SELECT` with an unknown source size requires
confirmation rather than counting the target table. Aggregations and grouped
low-sensitivity values are not treated as raw sensitive data exposure.
