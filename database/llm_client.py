"""Provider-neutral structured-output clients for SQL generation."""

from __future__ import annotations

import json
import os
from typing import Protocol


class LLMClient(Protocol):
    def complete(self, system: str, user: str, tool_schema: dict) -> dict:
        """Return the arguments supplied to the requested function tool."""


class OpenAIClient:
    def __init__(self, api_key: str, model: str | None = None) -> None:
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)
        self._model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    def complete(self, system: str, user: str, tool_schema: dict) -> dict:
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
        tool_calls = response.choices[0].message.tool_calls
        if not tool_calls:
            raise ValueError("The LLM response did not include the requested function call.")
        function_call = tool_calls[0].function
        if function_call.name != tool_schema["name"]:
            raise ValueError(f"The LLM called unexpected function {function_call.name!r}.")
        arguments = json.loads(function_call.arguments)
        if not isinstance(arguments, dict):
            raise ValueError("The LLM function arguments must be a JSON object.")
        return arguments


class GroqClient(OpenAIClient):
    """Groq's OpenAI-compatible chat-completions API client."""

    def __init__(self, api_key: str, model: str | None = None) -> None:
        from openai import OpenAI

        self._client = OpenAI(
            api_key=api_key,
            base_url="https://api.groq.com/openai/v1",
        )
        self._model = model or os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")


class MockLLMClient:
    """Offline client that returns queued structured responses."""

    def __init__(self, responses: list[dict] | dict) -> None:
        self._responses = [responses] if isinstance(responses, dict) else list(responses)
        self.calls: list[dict[str, str | dict]] = []

    def complete(self, system: str, user: str, tool_schema: dict) -> dict:
        self.calls.append({"system": system, "user": user, "tool_schema": tool_schema})
        if not self._responses:
            raise RuntimeError("MockLLMClient has no response left for this call.")
        return self._responses.pop(0)


def create_llm_client(provider: str | None = None) -> LLMClient:
    selected_provider = (provider or os.environ.get("LLM_PROVIDER", "groq")).lower()
    if selected_provider == "groq":
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY must be set to use the Groq provider.")
        return GroqClient(api_key=api_key)
    if selected_provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY must be set to use the OpenAI provider.")
        return OpenAIClient(api_key=api_key)
    raise ValueError(
        f"Unsupported LLM_PROVIDER {selected_provider!r}; supported providers: 'groq', 'openai'."
    )
