from __future__ import annotations

from wxclient.config import Settings

from .base import ChatAgent
from .errors import AgentConfigError
from .openai_agents import OpenAIAgentsChatAgent, OpenAIAgentsConfig


def build_chat_agent(settings: Settings) -> ChatAgent:
    provider = settings.agent_provider.strip().lower()
    if provider != "openai_agents":
        raise AgentConfigError(
            f"Unsupported WXCLIENT_AGENT_PROVIDER={settings.agent_provider!r}. "
            "Use 'openai_agents'."
        )

    return OpenAIAgentsChatAgent(
        OpenAIAgentsConfig(
            base_url=settings.llm_base_url or "",
            api_key=settings.llm_api_key,
            model=settings.llm_model or "",
            system_prompt=settings.agent_system_prompt,
            timeout_seconds=settings.agent_timeout_seconds,
        )
    )
