from __future__ import annotations


class AgentError(RuntimeError):
    pass


class AgentConfigError(AgentError):
    pass


class AgentExecutionError(AgentError):
    pass
