from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import SecretStr

from .errors import AgentConfigError, AgentExecutionError


DEFAULT_SYSTEM_PROMPT = (
    "You are a WeChat assistant. Reply in Chinese unless the user asks otherwise. "
    "Keep the response concise, natural, and directly useful."
)


@dataclass(frozen=True)
class OpenAIAgentsConfig:
    base_url: str
    api_key: SecretStr
    model: str
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    timeout_seconds: float = 30.0


class OpenAIAgentsChatAgent:
    def __init__(self, config: OpenAIAgentsConfig) -> None:
        self.config = config
        self._validate_config(config)

        try:
            from agents import (  # type: ignore[import-not-found]
                Agent,
                OpenAIChatCompletionsModel,
                Runner,
                set_tracing_disabled,
            )
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise AgentConfigError(
                "OpenAI Agents SDK is not installed. Install project dependencies first."
            ) from exc

        set_tracing_disabled(disabled=True)
        client = AsyncOpenAI(
            base_url=normalize_openai_base_url(config.base_url),
            api_key=config.api_key.get_secret_value(),
            http_client=_build_http_client(),
        )
        model = OpenAIChatCompletionsModel(
            model=config.model,
            openai_client=client,
        )
        self._runner = Runner
        self._agent = Agent(
            name="WeChat Reply Agent",
            instructions=config.system_prompt,
            model=model,
        )

    def reply(self, message: str, context: list[str] | None = None) -> str:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.areply(message, context=context))
        raise AgentExecutionError("OpenAIAgentsChatAgent.reply cannot run inside an event loop")

    async def areply(self, message: str, context: list[str] | None = None) -> str:
        prompt = self._build_prompt(message, context)
        try:
            result = await asyncio.wait_for(
                self._runner.run(self._agent, prompt),
                timeout=self.config.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise AgentExecutionError(
                f"OpenAI agent timed out after {self.config.timeout_seconds:g} seconds"
            ) from exc
        except Exception as exc:
            raise AgentExecutionError(f"OpenAI agent call failed: {exc}") from exc

        text = _stringify_output(result.final_output).strip()
        if not text:
            raise AgentExecutionError("OpenAI agent returned an empty reply")
        return text

    @staticmethod
    def _validate_config(config: OpenAIAgentsConfig) -> None:
        if not config.base_url:
            raise AgentConfigError("WXCLIENT_LLM_BASE_URL is required")
        if not config.api_key.get_secret_value():
            raise AgentConfigError("WXCLIENT_LLM_API_KEY is required")
        if not config.model:
            raise AgentConfigError("WXCLIENT_LLM_MODEL is required")
        if config.timeout_seconds <= 0:
            raise AgentConfigError("WXCLIENT_AGENT_TIMEOUT_SECONDS must be greater than 0")

    @staticmethod
    def _build_prompt(message: str, context: list[str] | None) -> str:
        if not context:
            return message
        history = "\n".join(context)
        return f"Conversation context:\n{history}\n\nLatest message:\n{message}"


def normalize_openai_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    if not base_url:
        return base_url
    if base_url.endswith("/v1"):
        return base_url
    return f"{base_url}/v1"


def _stringify_output(value: Any) -> str:
    if isinstance(value, str):
        return value
    return str(value)


def _build_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(trust_env=False)
