"""Provider-neutral structured-output clients for SQL generation."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol


class LLMUnavailableError(RuntimeError):
    """Raised when the configured LLM provider cannot complete a request."""


class LLMClient(Protocol):
    def complete(
        self,
        system: str,
        user: str,
        tool_schema: dict,
        *,
        context: str | None = None,
    ) -> dict:
        """Return the arguments supplied to the requested function tool."""


class OpenAIClient:
    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        *,
        timeout: float | None = None,
        max_retries: int = 0,
        rate_limit_retries: int = 3,
        base_url: str | None = None,
    ) -> None:
        from openai import OpenAI

        self._timeout = timeout if timeout is not None else _configured_timeout()
        if self._timeout <= 0:
            raise ValueError("LLM_TIMEOUT_SECONDS must be greater than zero.")
        client_options: dict[str, Any] = {
            "api_key": api_key,
            "timeout": self._timeout,
            "max_retries": max_retries,
        }
        if base_url:
            client_options["base_url"] = base_url
        self._client = OpenAI(**client_options)
        self._model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self._rate_limit_retries = rate_limit_retries
        self._call_latencies_seconds: list[float] = []
        self._latency_lock = threading.Lock()

    @property
    def call_latencies_seconds(self) -> list[float]:
        with self._latency_lock:
            return list(self._call_latencies_seconds)

    def complete(
        self,
        system: str,
        user: str,
        tool_schema: dict,
        *,
        context: str | None = None,
    ) -> dict:
        started_at = time.perf_counter()
        try:
            for attempt in range(self._rate_limit_retries + 1):
                try:
                    response = self._client.chat.completions.create(
                        model=self._model,
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        tools=[
                            {
                                "type": "function",
                                "function": tool_schema,
                            }
                        ],
                        tool_choice={
                            "type": "function",
                            "function": {"name": tool_schema["name"]},
                        },
                    )
                    break
                except Exception as error:
                    if _is_rate_limit_error(error) and attempt < self._rate_limit_retries:
                        time.sleep(
                            max(
                                _rate_limit_delay(error, attempt),
                                0.5 * (2 ** attempt),
                            )
                        )
                        continue
                    request_context = f" Context: {context}." if context else ""
                    short_reason = f"{type(error).__name__}: {error}"
                    raise LLMUnavailableError(
                        f"LLM request failed (timeout configured at {self._timeout:g}s): "
                        f"{short_reason}.{request_context}"
                    ) from error

            tool_calls = response.choices[0].message.tool_calls
            if not tool_calls:
                raise ValueError("The LLM response did not include the requested function call.")
            if len(tool_calls) != 1:
                raise ValueError(f"The LLM returned {len(tool_calls)} tool calls; expected exactly one.")
            function_call = tool_calls[0].function
            returned_name = _normalise_tool_name(function_call.name)
            arguments = json.loads(function_call.arguments)
            if not isinstance(arguments, dict):
                raise ValueError("The LLM function arguments must be a JSON object.")
            if (
                returned_name != _normalise_tool_name(tool_schema["name"])
                and not _matches_action_plan_schema(arguments)
            ):
                raise ValueError(
                    f"The LLM called unexpected function {function_call.name!r} "
                    "and its arguments do not match the ActionPlan schema."
                )
            return arguments
        finally:
            elapsed = time.perf_counter() - started_at
            with self._latency_lock:
                self._call_latencies_seconds.append(elapsed)


class GroqClient(OpenAIClient):
    """Groq's OpenAI-compatible chat-completions API client."""

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        *,
        timeout: float | None = None,
        max_retries: int = 0,
        rate_limit_retries: int = 3,
    ) -> None:
        super().__init__(
            api_key,
            model or os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b"),
            timeout=timeout,
            max_retries=max_retries,
            rate_limit_retries=rate_limit_retries,
            base_url="https://api.groq.com/openai/v1",
        )


class MockLLMClient:
    """Offline client that returns queued structured responses."""

    def __init__(self, responses: list[dict] | dict) -> None:
        self._responses = [responses] if isinstance(responses, dict) else list(responses)
        self.calls: list[dict[str, str | dict]] = []

    def complete(
        self,
        system: str,
        user: str,
        tool_schema: dict,
        *,
        context: str | None = None,
    ) -> dict:
        self.calls.append({"system": system, "user": user, "tool_schema": tool_schema})
        if not self._responses:
            raise RuntimeError("MockLLMClient has no response left for this call.")
        return self._responses.pop(0)


def create_llm_client(provider: str | None = None) -> LLMClient:
    return create_configured_llm_client(provider)


def _configured_timeout() -> float:
    raw_timeout = os.environ.get("LLM_TIMEOUT_SECONDS", "45")
    try:
        timeout = float(raw_timeout)
    except ValueError as error:
        raise ValueError("LLM_TIMEOUT_SECONDS must be a positive number of seconds.") from error
    if timeout <= 0:
        raise ValueError("LLM_TIMEOUT_SECONDS must be greater than zero.")
    return timeout


def api_key_environment_variable(provider: str | None = None) -> str:
    selected_provider = (provider or os.environ.get("LLM_PROVIDER", "groq")).lower()
    if selected_provider == "groq":
        return "GROQ_API_KEY"
    if selected_provider == "openai":
        return "OPENAI_API_KEY"
    raise ValueError(
        f"Unsupported LLM_PROVIDER {selected_provider!r}; supported providers: 'groq', 'openai'."
    )


def _normalise_tool_name(name: str) -> str:
    return name.removeprefix("functions.")


def _matches_action_plan_schema(arguments: dict[str, Any]) -> bool:
    required = {
        "action_type",
        "sql",
        "tables",
        "columns",
        "explanation",
        "confidence",
        "count_sql",
        "blocked",
        "block_reason",
    }
    if not required.issubset(arguments):
        return False
    confidence = arguments["confidence"]
    return (
        arguments["action_type"] in {"read", "write", "ddl"}
        and isinstance(arguments["sql"], str)
        and isinstance(arguments["tables"], list)
        and all(isinstance(value, str) for value in arguments["tables"])
        and isinstance(arguments["columns"], list)
        and all(isinstance(value, str) for value in arguments["columns"])
        and isinstance(arguments["explanation"], str)
        and not isinstance(confidence, bool)
        and isinstance(confidence, (int, float))
        and 0 <= confidence <= 1
        and (arguments["count_sql"] is None or isinstance(arguments["count_sql"], str))
        and isinstance(arguments["blocked"], bool)
        and (arguments["block_reason"] is None or isinstance(arguments["block_reason"], str))
    )


def _is_rate_limit_error(error: Exception) -> bool:
    status_code = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)
    return status_code == 429 or "429" in str(error)


def _rate_limit_delay(error: Exception, attempt: int) -> float:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {}) if response is not None else {}
    retry_after = None
    if headers:
        retry_after = headers.get("Retry-After") or headers.get("retry-after")
    if retry_after is not None:
        try:
            return max(0.0, float(retry_after)) + 0.5
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(str(retry_after))
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds()) + 0.5
            except (TypeError, ValueError, OverflowError):
                pass
    hint = re.search(r"try again in\s+([0-9]+(?:\.[0-9]+)?)\s*(?:seconds?|secs?|s)?", str(error), re.IGNORECASE)
    if hint:
        return float(hint.group(1)) + 0.5
    return 0.5 * (2 ** attempt)


def create_configured_llm_client(
    provider: str | None = None,
    *,
    timeout: float | None = None,
    max_retries: int = 0,
    rate_limit_retries: int = 3,
) -> LLMClient:
    selected_provider = (provider or os.environ.get("LLM_PROVIDER", "groq")).lower()
    request_timeout = timeout if timeout is not None else _configured_timeout()
    if selected_provider == "groq":
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY must be set to use the Groq provider.")
        return GroqClient(
            api_key=api_key,
            timeout=request_timeout,
            max_retries=max_retries,
            rate_limit_retries=rate_limit_retries,
        )
    if selected_provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY must be set to use the OpenAI provider.")
        return OpenAIClient(
            api_key=api_key,
            timeout=request_timeout,
            max_retries=max_retries,
            rate_limit_retries=rate_limit_retries,
        )
    raise ValueError(
        f"Unsupported LLM_PROVIDER {selected_provider!r}; supported providers: 'groq', 'openai'."
    )
