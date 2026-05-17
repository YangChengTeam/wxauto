from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
from openai import AsyncOpenAI
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
        self._client = AsyncOpenAI(
            base_url=normalize_openai_base_url(config.base_url),
            api_key=config.api_key.get_secret_value(),
            http_client=_build_http_client(),
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
                self._client.chat.completions.create(
                    model=self.config.model,
                    messages=[
                        {"role": "system", "content": self.config.system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                ),
                timeout=self.config.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise AgentExecutionError(
                f"OpenAI agent timed out after {self.config.timeout_seconds:g} seconds"
            ) from exc
        except Exception as exc:
            raise AgentExecutionError(f"OpenAI agent call failed: {exc}") from exc

        choice = result.choices[0] if getattr(result, "choices", None) else None
        message_obj = getattr(choice, "message", None) if choice is not None else None
        text = _stringify_output(getattr(message_obj, "content", "")).strip()
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
        return (
            "Conversation context:\n"
            "The history lines are role-labeled: 我 = messages I sent, 对方 = the other side, "
            "群成员 = another member in the group chat.\n"
            f"{history}\n\nLatest message:\n{message}"
        )


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
