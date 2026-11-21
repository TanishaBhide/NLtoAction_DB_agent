"""Week 7: session memory for the ReAct agent.

Two layers:
  * short-term working memory: the ordered list of turns in this session
    (request, final SQL, outcome, tables, row counts, errors), rendered into a
    compact context block that is given to the LLM on every new request;
  * durable memory in PostgreSQL: agent_sessions.current_state (JSONB) holds
    the rolling summary and session_events stores every ReAct thought, action
    and observation as an ordered weak entity (session_id, event_seq).

The intent parser keeps its own follow-up state per session_id (Week 3), so a
request such as "only the cancelled ones" is resolved against the previous turn.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

try:
    from .db import connect
except ImportError:
    from db import connect

MAX_TURNS_KEPT = 20
CONTEXT_TURNS = 3


@dataclass
class Turn:
    turn_no: int
    request: str
    action_type: str
    status: str
    final_sql: str | None
    tables: list[str] = field(default_factory=list)
    row_count: int | None = None
    affected_rows: int | None = None
    attempts: int = 1
    error: str | None = None
    result_preview: list[list[Any]] = field(default_factory=list)
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))


class SessionMemory:
    def __init__(self, session_id: str | None = None, *, persist: bool = True, database_url: str | None = None) -> None:
        self.session_id = (session_id or str(uuid.uuid4()))[:36]
        self.persist = persist
        self.database_url = database_url
        self.turns: list[Turn] = []
        self.event_seq = 0
        self.warning: str | None = None
        if persist:
            self._load()

    # ------------------------------------------------------------ persistence
    def _load(self) -> None:
        try:
            connection = connect(self.database_url)
        except Exception as error:  # noqa: BLE001
            self.persist, self.warning = False, f"session memory is in-process only: {error}"
            return
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO agent_sessions (session_id, user_label, purpose) VALUES (%s, %s, %s) "
                    "ON CONFLICT (session_id) DO NOTHING",
                    (self.session_id, "cli", "NL-to-Action ReAct agent session"),
                )
                cursor.execute("SELECT current_state FROM agent_sessions WHERE session_id = %s", (self.session_id,))
                row = cursor.fetchone()
                state = row[0] if row and row[0] else {}
                self.turns = [Turn(**turn) for turn in state.get("turns", [])]
                cursor.execute(
                    "SELECT COALESCE(MAX(event_seq), 0) FROM session_events WHERE session_id = %s",
                    (self.session_id,),
                )
                self.event_seq = int(cursor.fetchone()[0])
        except Exception as error:  # noqa: BLE001
            self.persist, self.warning = False, f"session memory is in-process only: {error}"
        finally:
            connection.close()

    def _save_state(self) -> None:
        if not self.persist:
            return
        state = {
            "turns": [asdict(turn) for turn in self.turns[-MAX_TURNS_KEPT:]],
            "last_tables": self.turns[-1].tables if self.turns else [],
        }
        try:
            connection = connect(self.database_url)
            try:
                with connection, connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE agent_sessions SET current_state = %s::jsonb WHERE session_id = %s",
                        (json.dumps(state, default=str), self.session_id),
                    )
            finally:
                connection.close()
        except Exception as error:  # noqa: BLE001
            self.warning = f"could not save session state: {error}"

    def log_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Append one ReAct step (thought/action/observation/answer) to session_events."""
        self.event_seq += 1
        if not self.persist:
            return
        try:
            connection = connect(self.database_url)
            try:
                with connection, connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO session_events (session_id, event_seq, event_type, event_payload) "
                        "VALUES (%s, %s, %s, %s::jsonb)",
                        (self.session_id, self.event_seq, event_type[:50], json.dumps(payload, default=str)),
                    )
            finally:
                connection.close()
        except Exception as error:  # noqa: BLE001
            self.warning = f"could not log session event: {error}"

    # ----------------------------------------------------------------- memory
    def add_turn(self, **values: Any) -> Turn:
        # Number from the last turn, not len(): the list is capped at MAX_TURNS_KEPT,
        # and turn numbers key the Week 8 audit rows (session_id, turn_no).
        turn = Turn(turn_no=(self.turns[-1].turn_no if self.turns else 0) + 1, **values)
        self.turns.append(turn)
        self.turns = self.turns[-MAX_TURNS_KEPT:]
        self._save_state()
        return turn

    def context_text(self, n: int = CONTEXT_TURNS) -> str | None:
        """Compact summary of the last n turns, given to the LLM as context."""
        if not self.turns:
            return None
        lines = []
        for turn in self.turns[-n:]:
            outcome = turn.status
            if turn.row_count is not None and turn.action_type == "read":
                outcome += f", {turn.row_count} row(s)"
            if turn.affected_rows:
                outcome += f", {turn.affected_rows} row(s) changed"
            if turn.error:
                outcome += f", error: {turn.error}"
            line = f"Turn {turn.turn_no}: \"{turn.request}\" -> {outcome}"
            if turn.final_sql:
                line += f"\n  SQL: {turn.final_sql}"
            if turn.result_preview:
                line += f"\n  first rows: {json.dumps(turn.result_preview[:3], default=str)}"
            lines.append(line)
        return "\n".join(lines)

    def history(self) -> list[Turn]:
        return list(self.turns)
