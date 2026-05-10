from __future__ import annotations

from wxclient.agents import ChatAgent, build_chat_agent
from wxclient.config import Settings
from wxclient.queue import TaskStore, TaskType


class AgentBridge:
    def __init__(
        self,
        store: TaskStore,
        settings: Settings | None = None,
        agent: ChatAgent | None = None,
    ) -> None:
        self.store = store
        self.agent = agent or build_chat_agent(settings or Settings())

    def handle_message(self, account_id: str, friend: str, text: str) -> str:
        reply = self.agent.reply(text).strip()
        if not reply:
            return ""
        self.store.create_task(
            TaskType.SEND_MESSAGE,
            {"friend": friend, "text": reply, "dry_run": True, "source": "agentbridge"},
            account_id,
        )
        return reply
