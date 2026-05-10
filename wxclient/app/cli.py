from __future__ import annotations

import json
from pathlib import Path

import typer

from wxclient.agents import AgentError, build_chat_agent
from wxclient.config import Settings, load_settings
from wxclient.logging_config import configure_logging
from wxclient.queue import OperationWorker, Task, TaskStore, TaskType
from wxclient.trigger import Scheduler, run_http_server
from wxclient.uia_unlock import probe_wechat_uia, unlock_with_narrator
from wxclient.wechat_api import WeChatApi

app = typer.Typer(help="Windows WeChat automation client")
_LOG_LEVEL = "INFO"


def _runtime(config: Path | None) -> tuple[Settings, TaskStore, WeChatApi]:
    settings = load_settings(config)
    store = TaskStore(settings.resolved_database_path)
    api = WeChatApi(
        dry_run=settings.dry_run,
        wechat_path=settings.wechat_path,
    )
    return settings, store, api


@app.callback()
def callback(log_level: str = typer.Option("INFO", help="Python logging level")) -> None:
    global _LOG_LEVEL
    _LOG_LEVEL = log_level
    configure_logging(level=log_level)


@app.command()
def serve(config: Path | None = typer.Option(None, help="Path to JSON config file")) -> None:
    settings, store, api = _runtime(config)
    configure_logging(level=_LOG_LEVEL, log_file=settings.data_dir / "logs" / "serve.log")
    worker = OperationWorker(store, api, settings=settings)
    scheduler = Scheduler(settings, store)
    worker.start()
    scheduler.start()
    typer.echo(f"wxclient listening on http://{settings.host}:{settings.port}")
    try:
        run_http_server(settings, store)
    finally:
        scheduler.stop()
        worker.stop()


@app.command()
def worker(config: Path | None = typer.Option(None, help="Path to JSON config file")) -> None:
    settings, store, api = _runtime(config)
    configure_logging(level=_LOG_LEVEL, log_file=settings.data_dir / "logs" / "worker.log")
    operation_worker = OperationWorker(store, api, settings=settings)
    try:
        operation_worker.run_forever()
    except KeyboardInterrupt:
        return


@app.command("gui")
def gui(config: Path | None = typer.Option(None, help="Path to JSON config file")) -> None:
    from wxclient.ui import run_gui

    raise typer.Exit(run_gui(config))


@app.command("list-tasks")
def list_tasks(
    config: Path | None = typer.Option(None, help="Path to JSON config file"),
) -> None:
    _, store, _ = _runtime(config)
    typer.echo(
        json.dumps([_task_to_dict(t) for t in store.list_tasks()], ensure_ascii=False, indent=2)
    )


@app.command("add-friend")
def add_friend(
    number: str,
    greetings: str = typer.Option("你好", help="Friend request greeting"),
    remark: str | None = typer.Option(None, help="Optional remark"),
    real: bool = typer.Option(False, help="Execute real WeChat action"),
    config: Path | None = typer.Option(None, help="Path to JSON config file"),
) -> None:
    settings, store, _ = _runtime(config)
    task = store.create_task(
        TaskType.ADD_FRIEND,
        {
            "number": number,
            "greetings": greetings,
            "remark": remark,
            "dry_run": not real,
        },
        settings.default_account_id,
    )
    typer.echo(task.id)


@app.command("check-new-friends")
def check_new_friends(
    verify: bool = typer.Option(False, help="Accept friend requests while checking"),
    limit: int = typer.Option(8, help="Maximum friend requests to accept"),
    real: bool = typer.Option(False, help="Execute real WeChat action"),
    config: Path | None = typer.Option(None, help="Path to JSON config file"),
) -> None:
    settings, store, _ = _runtime(config)
    task = store.create_task(
        TaskType.CHECK_NEW_FRIENDS,
        {"verify": verify, "limit": limit, "dry_run": not real},
        settings.default_account_id,
    )
    typer.echo(task.id)


@app.command("accept-new-friends")
def accept_new_friends(
    limit: int = typer.Option(8, help="Maximum friend requests to accept"),
    real: bool = typer.Option(False, help="Execute real WeChat action"),
    config: Path | None = typer.Option(None, help="Path to JSON config file"),
) -> None:
    settings, store, _ = _runtime(config)
    task = store.create_task(
        TaskType.ACCEPT_NEW_FRIENDS,
        {"limit": limit, "dry_run": not real},
        settings.default_account_id,
    )
    typer.echo(task.id)


@app.command("send-message")
def send_message(
    friend: str,
    text: str,
    real: bool = typer.Option(False, help="Execute real WeChat action"),
    config: Path | None = typer.Option(None, help="Path to JSON config file"),
) -> None:
    settings, store, _ = _runtime(config)
    task = store.create_task(
        TaskType.SEND_MESSAGE,
        {"friend": friend, "text": text, "dry_run": not real},
        settings.default_account_id,
    )
    typer.echo(task.id)


@app.command("listen-chat")
def listen_chat(
    friend: str,
    duration: str = typer.Option("30s", help="Listen duration, e.g. 30s, 1min, 1h"),
    select_sender: bool = typer.Option(False, help="Try to identify message senders"),
    save_file: bool = typer.Option(False, help="Save file messages"),
    save_media: bool = typer.Option(False, help="Save image/video messages"),
    real: bool = typer.Option(False, help="Execute real WeChat action"),
    config: Path | None = typer.Option(None, help="Path to JSON config file"),
) -> None:
    settings, store, _ = _runtime(config)
    task = store.create_task(
        TaskType.SCAN_MESSAGES,
        {
            "friend": friend,
            "duration": duration,
            "select_sender": select_sender,
            "save_file": save_file,
            "save_media": save_media,
            "dry_run": not real,
        },
        settings.default_account_id,
    )
    typer.echo(task.id)


@app.command("listen-chats")
def listen_chats(
    friends: list[str] = typer.Argument(..., help="One or more friend/group names"),
    duration: str = typer.Option("30s", help="Listen duration per chat, e.g. 30s, 1min, 1h"),
    auto_reply: bool = typer.Option(
        True,
        "--auto-reply/--no-auto-reply",
        help="Use the configured agent to reply to detected messages",
    ),
    reply_all: bool = typer.Option(False, help="Reply to every new message instead of only the last"),
    select_sender: bool = typer.Option(False, help="Try to identify group message senders"),
    real: bool = typer.Option(False, help="Execute real WeChat action"),
    config: Path | None = typer.Option(None, help="Path to JSON config file"),
) -> None:
    settings, store, _ = _runtime(config)
    task = store.create_task(
        TaskType.WATCH_CHATS,
        {
            "friends": _clean_friends(friends),
            "duration": duration,
            "auto_reply": auto_reply,
            "reply_all": reply_all,
            "select_sender": select_sender,
            "dry_run": not real,
        },
        settings.default_account_id,
    )
    typer.echo(task.id)


@app.command("read-visible-messages")
def read_visible_messages(
    friend: str,
    real: bool = typer.Option(False, help="Execute real WeChat action"),
    config: Path | None = typer.Option(None, help="Path to JSON config file"),
) -> None:
    settings, store, _ = _runtime(config)
    task = store.create_task(
        TaskType.READ_VISIBLE_MESSAGES,
        {"friend": friend, "dry_run": not real},
        settings.default_account_id,
    )
    typer.echo(task.id)


@app.command("check-unread-messages")
def check_unread_messages(
    real: bool = typer.Option(False, help="Read current unread WeChat messages"),
    config: Path | None = typer.Option(None, help="Path to JSON config file"),
) -> None:
    settings, store, _ = _runtime(config)
    task = store.create_task(
        TaskType.CHECK_UNREAD_MESSAGES,
        {"dry_run": not real},
        settings.default_account_id,
    )
    typer.echo(task.id)


@app.command("agent-reply")
def agent_reply(
    message: str,
    config: Path | None = typer.Option(None, help="Path to JSON config file"),
) -> None:
    settings = load_settings(config)
    try:
        reply = build_chat_agent(settings).reply(message)
    except AgentError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(reply)


@app.command("reply-chat")
def reply_chat(
    friend: str,
    duration: str = typer.Option("30s", help="Listen duration, e.g. 30s, 1min, 1h"),
    reply_all: bool = typer.Option(False, help="Reply to every new message instead of only the last"),
    latest_visible: bool = typer.Option(False, help="Reply to the latest currently visible message"),
    select_sender: bool = typer.Option(False, help="Try to identify message senders"),
    real: bool = typer.Option(False, help="Read and send through real WeChat"),
    config: Path | None = typer.Option(None, help="Path to JSON config file"),
) -> None:
    settings, store, _ = _runtime(config)
    task = store.create_task(
        TaskType.REPLY_CHAT,
        {
            "friend": friend,
            "duration": duration,
            "reply_all": reply_all,
            "select_sender": select_sender,
            "source": "visible" if latest_visible else "new",
            "dry_run": not real,
        },
        settings.default_account_id,
    )
    typer.echo(task.id)


@app.command("reply-unread")
def reply_unread(
    friend: str | None = typer.Option(None, help="Only reply to this chat name"),
    reply_all: bool = typer.Option(False, help="Reply to every unread message instead of only the last per chat"),
    real: bool = typer.Option(False, help="Read unread messages and send replies through real WeChat"),
    config: Path | None = typer.Option(None, help="Path to JSON config file"),
) -> None:
    settings, store, _ = _runtime(config)
    task = store.create_task(
        TaskType.REPLY_UNREAD,
        {
            "friend": friend,
            "reply_all": reply_all,
            "dry_run": not real,
        },
        settings.default_account_id,
    )
    typer.echo(task.id)


@app.command("probe-uia")
def probe_uia() -> None:
    typer.echo(json.dumps(probe_wechat_uia(), ensure_ascii=False, indent=2))


@app.command("unlock-uia")
def unlock_uia(
    restart: bool = typer.Option(False, help="Close and restart WeChat after Narrator starts"),
    stop_narrator: bool = typer.Option(True, help="Stop Narrator after UIA is exposed"),
    startup_wait: float = typer.Option(35.0, help="Seconds to wait for WeChat UIA"),
    config: Path | None = typer.Option(None, help="Path to JSON config file"),
) -> None:
    settings = load_settings(config)
    ok = unlock_with_narrator(
        wechat_path=settings.wechat_path,
        restart_wechat=restart,
        startup_wait=startup_wait,
        stop_narrator_after_wechat=stop_narrator,
    )
    typer.echo(json.dumps({"uia_exposed": ok}, ensure_ascii=False))


def _task_to_dict(task: Task) -> dict:
    return {
        "id": task.id,
        "type": task.type.value,
        "status": task.status.value,
        "account_id": task.account_id,
        "attempts": task.attempts,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "last_error": task.last_error,
        "result": task.result,
    }


def _clean_friends(values: list[str]) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in str(value).replace("，", ",").split(","):
            text = part.strip()
            if not text or text in seen:
                continue
            seen.add(text)
            rows.append(text)
    if not rows:
        raise typer.BadParameter("至少需要一个好友或群名")
    return rows


def main() -> None:
    app()


if __name__ == "__main__":
    main()
