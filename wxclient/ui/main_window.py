from __future__ import annotations

import ast
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QFontDatabase, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QApplication,
)
from pydantic import SecretStr

from wxclient.config import Settings
from wxclient.logging_config import configure_logging
from wxclient.queue import OperationWorker, TaskStore
from wxclient.uia_unlock import (
    bootstrap_wechat_uia,
    get_screen_reader_active,
    probe_wechat_uia,
    set_screen_reader_active,
    try_unlock_without_restart,
    unlock_with_narrator,
)
from wxclient.wechat_api import WeChatApi

from .config import load_form_state, save_gui_settings
from .task_actions import (
    enqueue_add_friend,
    enqueue_check_new_friends,
    enqueue_post_moments,
    enqueue_reply_unread,
    enqueue_watch_chats,
)

APP_NAME = "AI微信自动化"


class MainWindow(QMainWindow):
    uia_probe_finished = Signal(dict)
    uia_unlock_finished = Signal(bool, str)
    uia_bootstrap_finished = Signal(dict)
    worker_start_finished = Signal(dict)

    def __init__(self, settings: Settings, config_path: Path) -> None:
        super().__init__()
        self.ui_log_lines: list[str] = []
        self._last_uia_signature: tuple[str, str] | None = None
        self._uia_probe_running = False
        self._closing = False
        self._last_operation_state: str | None = None
        self._worker_starting = False
        self.uia_probe_finished.connect(self._handle_uia_probe_result)
        self.uia_unlock_finished.connect(self._handle_uia_unlock_result)
        self.uia_bootstrap_finished.connect(self._handle_uia_bootstrap_result)
        self.worker_start_finished.connect(self._handle_worker_start_result)
        self._uia_unlock_running = False
        self._startup_uia_bootstrap_running = False
        self._append_log("正在启动 AI微信自动化。")
        _load_ui_font()
        self._append_log("界面字体已加载，准备初始化配置。")
        self.settings = settings
        self.config_path = config_path
        self._form_state = load_form_state(config_path)
        self._append_log(f"配置文件位置：{config_path}")
        self._append_log(f"数据目录：{settings.data_dir}")
        self.store = TaskStore(settings.resolved_database_path)
        self._append_log(f"本地任务库已连接：{settings.resolved_database_path}")
        self.api = WeChatApi(
            dry_run=settings.dry_run,
            wechat_path=settings.wechat_path,
        )
        self._append_log("微信自动化接口已初始化。")
        self.worker = OperationWorker(self.store, self.api, settings=settings)
        self._append_log("串行任务服务已准备，微信动作会按队列逐个执行。")
        self.worker_running = False
        self.log_path = settings.data_dir / "logs" / "ui.log"
        self.task_tables: list[tuple[QTableWidget, set[str] | None]] = []
        self._previous_screen_reader_state = get_screen_reader_active()
        if self._previous_screen_reader_state is None:
            self._append_log("未能读取系统屏幕阅读器标志，继续尝试开启。")
        else:
            state = "开启" if self._previous_screen_reader_state else "关闭"
            self._append_log(f"启动前屏幕阅读器标志为：{state}。")
        self.screen_reader_enabled = (
            set_screen_reader_active(True) or self._previous_screen_reader_state is True
        )
        if self.screen_reader_enabled:
            self._append_log("屏幕阅读器标志已开启，微信 UIA 节点检测可以开始。")
        else:
            self._append_log("屏幕阅读器标志开启失败，后续 UIA 检测可能不可用。")

        self.friend_timer = self._make_timer(self.enqueue_friend_check)
        self.unread_timer = self._make_timer(self.enqueue_unread_reply)
        self.watch_timer = self._make_timer(self.enqueue_watch_chats)
        self.refresh_timer = self._make_timer(self.refresh_all)
        self.uia_timer = self._make_timer(self.refresh_uia_status)
        self.worker_state_timer = self._make_timer(self.refresh_operation_state)
        self._append_log("定时器已注册：日志每 2 秒刷新，UIA 状态每 10 秒检测。")

        icon_path = _asset_path("app-icon.ico")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
            self._append_log("应用图标已加载。")
        else:
            self._append_log("未找到应用图标，使用系统默认图标。")
        self.setWindowTitle(APP_NAME)
        self._build_ui()
        self._show_logs()
        self._append_log("主界面已创建。")
        self.refresh_timer.start(2000)
        self.uia_timer.start(10000)
        self.worker_state_timer.start(500)
        self._append_log("后台状态刷新已启动。")
        self.refresh_all()
        self.refresh_uia_status()
        self.refresh_operation_state()
        self._bootstrap_uia_on_startup()

    def closeEvent(self, event: Any) -> None:  # type: ignore[override]
        self._closing = True
        self._worker_starting = False
        self._startup_uia_bootstrap_running = False
        self._persist_form_state()
        self.refresh_timer.stop()
        self.uia_timer.stop()
        self.worker_state_timer.stop()
        self.watch_timer.stop()
        self._append_log("正在关闭应用，准备停止后台服务。")
        self.worker.stop(wait=False)
        if self._previous_screen_reader_state is not None:
            set_screen_reader_active(self._previous_screen_reader_state)
            state = "开启" if self._previous_screen_reader_state else "关闭"
            self._append_log(f"屏幕阅读器标志已恢复为启动前状态：{state}。")
        super().closeEvent(event)

    def _make_timer(self, callback: Any):
        from PySide6.QtCore import QTimer

        timer = QTimer(self)
        timer.timeout.connect(callback)
        return timer

    def _build_ui(self) -> None:
        shell = QWidget()
        shell_layout = QHBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(248)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(14, 14, 14, 14)
        sidebar_layout.setSpacing(12)

        sidebar_layout.addWidget(self._brand_panel())
        sidebar_layout.addWidget(self._operation_status_panel())
        sidebar_layout.addWidget(self._uia_status_panel())
        sidebar_layout.addWidget(self._section_label("日志"))

        self.logs_button = QPushButton("运行日志")
        self.logs_button.setObjectName("navButton")
        self.logs_button.clicked.connect(self._show_logs)
        sidebar_layout.addWidget(self.logs_button)

        sidebar_layout.addWidget(self._section_label("自动化"))

        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        for text in ("好友检测", "未读回复", "消息监听", "添加好友", "朋友圈"):
            self.nav.addItem(QListWidgetItem(text))
        self.nav.currentRowChanged.connect(self._switch_page)
        sidebar_layout.addWidget(self.nav, 1)

        self.settings_button = QPushButton("设置")
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.clicked.connect(self._show_settings)
        sidebar_layout.addWidget(self.settings_button)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._overview_page())
        self.pages.addWidget(self._friend_automation_page())
        self.pages.addWidget(self._unread_automation_page())
        self.pages.addWidget(self._watch_automation_page())
        self.pages.addWidget(self._add_friend_page())
        self.pages.addWidget(self._moments_page())
        self.pages.addWidget(self._settings_page())

        shell_layout.addWidget(sidebar)
        shell_layout.addWidget(self.pages, 1)
        self.setCentralWidget(shell)
        self.statusBar().setObjectName("statusBar")
        self.setStyleSheet(_STYLE)

    def _brand_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("brandPanel")
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        logo_box = QFrame()
        logo_box.setObjectName("logoBox")
        logo_box.setFixedSize(46, 46)
        logo_layout = QGridLayout(logo_box)
        logo_layout.setContentsMargins(0, 0, 0, 0)
        logo_layout.setSpacing(0)

        logo = QLabel()
        logo.setObjectName("logo")
        logo.setFixedSize(42, 42)
        logo_path = _asset_path("app-logo-64.png")
        if logo_path.exists():
            pixmap = QPixmap(str(logo_path))
            logo.setPixmap(
                pixmap.scaled(42, 42, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        logo_layout.addWidget(logo, 0, 0, Qt.AlignCenter)

        self.uia_status_dot = QLabel()
        self.uia_status_dot.setObjectName("uiaStatusDot")
        self.uia_status_dot.setFixedSize(12, 12)
        self.uia_status_dot.setProperty("state", "unknown")
        self.uia_status_dot.setToolTip("正在检测 UIA 状态")
        logo_layout.addWidget(self.uia_status_dot, 0, 0, Qt.AlignRight | Qt.AlignBottom)

        layout.addWidget(logo_box)

        copy = QVBoxLayout()
        copy.setContentsMargins(0, 0, 0, 0)
        copy.setSpacing(2)
        title = QLabel(APP_NAME)
        title.setObjectName("brandTitle")
        subtitle = QLabel("微信 UIA 工作台")
        subtitle.setObjectName("brandSubtitle")
        copy.addWidget(title)
        copy.addWidget(subtitle)
        layout.addLayout(copy, 1)
        return panel

    def _section_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sectionLabel")
        return label

    def _operation_status_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("operationStatus")
        panel.setProperty("state", "stopped")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)

        self.operation_status_title = QLabel("服务未启动")
        self.operation_status_title.setObjectName("operationStatusTitle")
        self.operation_status_detail = QLabel("启动服务后开始处理任务")
        self.operation_status_detail.setObjectName("operationStatusDetail")
        self.operation_status_detail.setWordWrap(True)
        layout.addWidget(self.operation_status_title)
        layout.addWidget(self.operation_status_detail)
        return panel

    def _uia_status_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("uiaStatus")
        panel.setProperty("state", "unknown")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)

        header = QHBoxLayout()
        header.setSpacing(6)
        self.uia_panel_dot = QLabel()
        self.uia_panel_dot.setObjectName("uiaPanelDot")
        self.uia_panel_dot.setFixedSize(8, 8)
        self.uia_state_label = QLabel("正在检测...")
        self.uia_state_label.setObjectName("uiaStateLabel")
        header.addWidget(self.uia_panel_dot)
        header.addWidget(self.uia_state_label, 1)
        layout.addLayout(header)

        self.uia_detail_label = QLabel("微信 UIA 状态待检测")
        self.uia_detail_label.setObjectName("uiaDetailLabel")
        self.uia_detail_label.setWordWrap(True)
        layout.addWidget(self.uia_detail_label)

        self.uia_unlock_button = QPushButton("解锁 UIA")
        self.uia_unlock_button.setObjectName("uiaUnlockButton")
        self.uia_unlock_button.clicked.connect(self._try_unlock_uia)
        self.uia_unlock_button.setVisible(False)
        layout.addWidget(self.uia_unlock_button)
        return panel

    def _overview_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(14)

        header = QHBoxLayout()
        title = QLabel("运行日志")
        title.setObjectName("title")
        self.worker_button = QPushButton("启动服务")
        self.worker_button.setObjectName("primaryButton")
        self.worker_button.clicked.connect(self.toggle_worker)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.worker_button)
        layout.addLayout(header)

        layout.addWidget(self._logs_panel(), 1)
        return page

    def _tasks_panel(self, title: str = "任务列表", task_types: set[str] | None = None) -> QWidget:
        box = QGroupBox(title)
        layout = QVBoxLayout(box)
        table = QTableWidget(0, 7)
        table.setHorizontalHeaderLabels(
            ["任务 ID", "类型", "状态", "账号", "尝试", "更新时间", "错误"]
        )
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table)
        self.task_tables.append((table, task_types))
        return box

    def _logs_panel(self) -> QWidget:
        box = QGroupBox("运行日志")
        layout = QVBoxLayout(box)
        actions = QHBoxLayout()
        actions.addStretch(1)
        clear_button = QPushButton("清除日志")
        clear_button.clicked.connect(self._clear_logs)
        actions.addWidget(clear_button)
        layout.addLayout(actions)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view)
        return box

    def _friend_automation_page(self) -> QWidget:
        page = self._page("定时检测好友")
        form = self._form(page)
        self.friend_interval = _spin(10, 86400, self._form_state.get("friend_interval", self.settings.friend_check_interval_seconds))
        self.friend_limit = _spin(1, 100, self._form_state.get("friend_limit", 8))
        self.friend_verify = QCheckBox("检测到好友申请后自动同意")
        self.friend_verify.setChecked(self._form_state.get("friend_verify", False))
        self.friend_start = QPushButton("启动自动任务")
        self.friend_start.setObjectName("primaryButton")
        self.friend_start.clicked.connect(self.toggle_friend_timer)
        form.addRow("间隔秒数", self.friend_interval)
        form.addRow("处理数量", self.friend_limit)
        form.addRow("", self.friend_verify)
        form.addRow("", self.friend_start)
        page.layout().addWidget(  # type: ignore[union-attr]
            self._tasks_panel("好友检测任务", {"check_new_friends", "accept_new_friends"})
        )
        return page

    def _unread_automation_page(self) -> QWidget:
        page = self._page("定时检测未读并回复")
        form = self._form(page)
        self.unread_interval = _spin(10, 86400, self._form_state.get("unread_interval", self.settings.message_scan_interval_seconds))
        self.unread_friend = QLineEdit()
        self.unread_friend.setPlaceholderText("留空表示所有会话")
        self.unread_friend.setText(self._form_state.get("unread_friend", ""))
        self.unread_reply_all = QCheckBox("回复所有未读消息")
        self.unread_reply_all.setChecked(self._form_state.get("unread_reply_all", False))
        self.unread_start = QPushButton("启动自动任务")
        self.unread_start.setObjectName("primaryButton")
        self.unread_start.clicked.connect(self.toggle_unread_timer)
        add_watch_button = QPushButton("将当前会话加入监听")
        add_watch_button.clicked.connect(self._add_current_chat_to_watch)
        form.addRow("间隔秒数", self.unread_interval)
        form.addRow("目标会话", self.unread_friend)
        form.addRow("", self.unread_reply_all)
        form.addRow("", self.unread_start)
        form.addRow("", add_watch_button)
        page.layout().addWidget(  # type: ignore[union-attr]
            self._tasks_panel("未读回复任务", {"check_unread_messages", "reply_unread"})
        )
        return page

    def _watch_automation_page(self) -> QWidget:
        page = self._page("指定好友/群消息监听")
        form = self._form(page)
        self.watch_interval = _spin(10, 86400, self._form_state.get("watch_interval", self.settings.message_scan_interval_seconds))
        self.watch_duration = QLineEdit(self._form_state.get("watch_duration", "30s"))
        self.watch_targets = QTextEdit()
        self.watch_targets.setPlaceholderText("一行一个好友或群名，也可以用逗号分隔")
        self.watch_targets.setFixedHeight(92)
        saved_targets = self._form_state.get("watch_targets", "")
        if saved_targets:
            self.watch_targets.setPlainText(saved_targets)
        self.watch_auto_reply = QCheckBox("监听到新消息后自动调用 Agent 回复")
        self.watch_auto_reply.setChecked(self._form_state.get("watch_auto_reply", True))
        self.watch_reply_all = QCheckBox("回复本轮监听到的所有新消息")
        self.watch_reply_all.setChecked(self._form_state.get("watch_reply_all", False))
        self.watch_start = QPushButton("启动监听")
        self.watch_start.setObjectName("primaryButton")
        self.watch_start.clicked.connect(self.toggle_watch_timer)
        form.addRow("间隔秒数", self.watch_interval)
        form.addRow("每次监听时长", self.watch_duration)
        form.addRow("监听对象", self.watch_targets)
        form.addRow("", self.watch_auto_reply)
        form.addRow("", self.watch_reply_all)
        form.addRow("", self.watch_start)
        page.layout().addWidget(  # type: ignore[union-attr]
            self._tasks_panel("消息监听任务", {"watch_chats"})
        )
        return page

    def _add_friend_page(self) -> QWidget:
        page = self._page("添加好友")
        form = self._form(page)
        self.add_number = QLineEdit()
        self.add_number.setText(self._form_state.get("add_number", ""))
        self.add_greetings = QLineEdit(self._form_state.get("add_greetings", "你好"))
        self.add_remark = QLineEdit()
        self.add_remark.setText(self._form_state.get("add_remark", ""))
        button = QPushButton("提交添加好友")
        button.clicked.connect(self.enqueue_add_friend)
        form.addRow("手机号", self.add_number)
        form.addRow("招呼语", self.add_greetings)
        form.addRow("备注", self.add_remark)
        form.addRow("", button)
        page.layout().addWidget(self._tasks_panel("添加好友任务", {"add_friend"}))  # type: ignore[union-attr]
        return page

    def _moments_page(self) -> QWidget:
        page = self._page("发送朋友圈")
        form = self._form(page)

        self.moments_text = QTextEdit()
        self.moments_text.setPlaceholderText("输入朋友圈文字内容…")
        self.moments_text.setMaximumHeight(120)
        self.moments_text.setPlainText(self._form_state.get("moments_text", ""))

        self.moments_medias = QTextEdit()
        self.moments_medias.setPlaceholderText("每行一个图片或视频路径")
        self.moments_medias.setMaximumHeight(100)
        self.moments_medias.setPlainText(self._form_state.get("moments_medias", ""))

        browse_button = QPushButton("选择图片/视频")
        browse_button.clicked.connect(self._browse_moments_media)

        buttons = QHBoxLayout()
        send_button = QPushButton("发送朋友圈")
        send_button.setObjectName("primaryButton")
        send_button.clicked.connect(self.enqueue_post_moments)
        buttons.addStretch(1)
        buttons.addWidget(send_button)

        form.addRow("内容", self.moments_text)
        form.addRow("图片/视频", self.moments_medias)
        form.addRow("", browse_button)
        form.addRow("", buttons)
        page.layout().addWidget(self._tasks_panel("朋友圈任务", {"post_moments"}))  # type: ignore[union-attr]
        return page

    def _browse_moments_media(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择图片或视频",
            "",
            "媒体文件 (*.png *.jpg *.jpeg *.bmp *.mp4 *.mov);;所有文件 (*)",
        )
        if files:
            existing = self.moments_medias.toPlainText().strip()
            new_lines = "\n".join(files)
            self.moments_medias.setPlainText(
                f"{existing}\n{new_lines}".strip() if existing else new_lines
            )

    def enqueue_post_moments(self) -> None:
        text = self.moments_text.toPlainText().strip()
        raw = self.moments_medias.toPlainText().strip()
        medias = [line.strip() for line in raw.splitlines() if line.strip()] if raw else []
        if not text and not medias:
            QMessageBox.warning(self, "缺少内容", "请输入文字或选择图片/视频")
            return
        confirm = QMessageBox.question(
            self,
            "确认发送",
            f"即将发送朋友圈：\n文字：{text[:50] or '（无）'}\n图片/视频：{len(medias)} 个\n\n确认发送？",
        )
        if confirm != QMessageBox.Yes:
            return
        self._append_log(f"创建朋友圈任务：文字 {len(text)} 字，图片/视频 {len(medias)} 个。")
        task = enqueue_post_moments(
            self.store,
            self.settings.default_account_id,
            text=text,
            medias=medias,
            real=True,
        )
        self._task_created(task.id, task.type.value)

    def _settings_page(self) -> QWidget:
        page = self._page("Agent 设置")
        form = self._form(page)
        self.provider_input = QLineEdit(self.settings.agent_provider)
        self.base_url_input = QLineEdit(self.settings.llm_base_url)
        self.model_input = QLineEdit(self.settings.llm_model)
        self.api_key_input = QLineEdit(self.settings.llm_api_key.get_secret_value())
        self.api_key_input.setEchoMode(QLineEdit.Password)
        self.timeout_input = _spin(1, 300, int(self.settings.agent_timeout_seconds))
        self.context_count_input = _spin(0, 50, int(self.settings.agent_context_message_count))
        self.prompt_input = QTextEdit(self.settings.agent_system_prompt)
        save_button = QPushButton("保存设置")
        save_button.setObjectName("primaryButton")
        save_button.clicked.connect(self.save_settings)
        form.addRow("Provider", self.provider_input)
        form.addRow("Base URL", self.base_url_input)
        form.addRow("Model", self.model_input)
        form.addRow("API Key", self.api_key_input)
        form.addRow("Timeout", self.timeout_input)
        form.addRow("上下文消息数", self.context_count_input)
        form.addRow("System Prompt", self.prompt_input)
        form.addRow("", save_button)
        return page

    def _page(self, title: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(16)
        label = QLabel(title)
        label.setObjectName("title")
        layout.addWidget(label)
        return page

    def _set_sidebar_button_active(self, button: QPushButton, active: bool) -> None:
        button.setProperty("active", active)
        button.style().unpolish(button)
        button.style().polish(button)

    def _form(self, page: QWidget) -> QFormLayout:
        panel = QFrame()
        panel.setObjectName("panel")
        form = QFormLayout(panel)
        form.setContentsMargins(22, 20, 22, 20)
        form.setSpacing(14)
        form.setLabelAlignment(Qt.AlignLeft)
        page.layout().addWidget(panel)  # type: ignore[union-attr]
        return form

    def _switch_page(self, row: int) -> None:
        if row < 0:
            return
        self._set_sidebar_button_active(self.logs_button, False)
        self._set_sidebar_button_active(self.settings_button, False)
        self.pages.setCurrentIndex(row + 1)

    def _show_logs(self) -> None:
        self.nav.clearSelection()
        self.nav.setCurrentRow(-1)
        self.pages.setCurrentIndex(0)
        self._set_sidebar_button_active(self.logs_button, True)
        self._set_sidebar_button_active(self.settings_button, False)

    def _show_settings(self) -> None:
        self.nav.clearSelection()
        self.nav.setCurrentRow(-1)
        self._set_sidebar_button_active(self.logs_button, False)
        self._set_sidebar_button_active(self.settings_button, True)
        self.pages.setCurrentIndex(6)

    def toggle_worker(self) -> None:
        if self._worker_starting:
            self._append_log("服务正在启动中，请稍等。")
            return
        if self.worker_running:
            self._append_log("正在停止服务，请等待当前任务收尾。")
            self.worker.stop(wait=False)
            self.worker_running = False
            self.worker_button.setText("启动服务")
            self._append_log("停止信号已发送。若当前正在操作微信，会在本次动作结束后停止。")
            self.refresh_operation_state(force=True)
            return
        self._begin_worker_start()

    def _begin_worker_start(self) -> None:
        self._worker_starting = True
        self.worker_button.setEnabled(False)
        self.worker_button.setText("启动中...")
        self._append_log("正在后台检查 UIA、自动解锁并启动服务，请稍等。")
        thread = threading.Thread(
            target=self._start_worker_in_background,
            name="wx-ui-worker-start",
            daemon=True,
        )
        thread.start()

    def _start_worker_in_background(self) -> None:
        try:
            bootstrap = bootstrap_wechat_uia(
                wechat_path=self.settings.wechat_path,
                restart_if_needed=True,
            )
            probe = bootstrap.get("probe") if isinstance(bootstrap.get("probe"), dict) else {}
            if not bootstrap.get("success"):
                self.worker_start_finished.emit(
                    {
                        "success": False,
                        "reason": "uia_not_ready",
                        "probe": probe,
                        "bootstrap": bootstrap,
                    }
                )
                return
            if self._closing:
                self.worker_start_finished.emit(
                    {"success": False, "reason": "closed", "probe": probe}
                )
                return
            configure_logging(level="INFO", log_file=self.log_path)
            self.worker.start()
        except Exception as exc:
            self.worker_start_finished.emit(
                {"success": False, "reason": "error", "error": str(exc)}
            )
            return
        self.worker_start_finished.emit({"success": True, "probe": probe, "bootstrap": bootstrap})

    def _handle_worker_start_result(self, result: dict[str, Any]) -> None:
        self._worker_starting = False
        if self._closing:
            self.worker.stop(wait=False)
            return
        self.worker_button.setEnabled(True)
        if result.get("success"):
            bootstrap = result.get("bootstrap") if isinstance(result.get("bootstrap"), dict) else {}
            mode = str(bootstrap.get("mode") or "")
            if mode == "already":
                self._append_log("启动服务前 UIA 已可用。")
            if mode == "quick":
                self._append_log("启动服务前已自动解锁 UIA（无需重启微信）。")
            elif mode == "restart":
                self._append_log("启动服务前已自动解锁 UIA（已重启微信）。")
            self.worker_running = True
            self.worker_button.setText("停止服务")
            self._append_log(f"服务已启动，日志文件：{self.log_path}")
            self._append_log("服务线程已启动，开始轮询任务队列。")
            self.refresh_uia_status()
            self.refresh_operation_state(force=True)
            return

        self.worker_running = False
        self.worker_button.setText("启动服务")
        if result.get("reason") == "uia_not_ready":
            probe = result.get("probe") if isinstance(result.get("probe"), dict) else {}
            detail = str(probe.get("reason") or "自动解锁失败")
            self._append_log(f"UIA 自动解锁失败，服务未启动：{detail}")
            QMessageBox.warning(
                self,
                "UIA 未就绪",
                f"微信 UIA 节点仍未暴露，服务未启动。\n原因：{detail}",
            )
            self.refresh_uia_status()
            return
        detail = result.get("error") or "未知错误"
        self._append_log(f"服务启动失败：{detail}")
        QMessageBox.warning(self, "服务启动失败", str(detail))

    def toggle_friend_timer(self) -> None:
        if self.friend_timer.isActive():
            self.friend_timer.stop()
            self.friend_start.setText("启动自动任务")
            self._append_log("好友检测自动任务已停止。")
            return
        self.friend_timer.start(self.friend_interval.value() * 1000)
        self.friend_start.setText("停止自动任务")
        self._append_log(
            f"好友检测自动任务已启动，每 {self.friend_interval.value()} 秒执行一次。"
        )
        self.enqueue_friend_check()

    def toggle_unread_timer(self) -> None:
        if self.unread_timer.isActive():
            self.unread_timer.stop()
            self.unread_start.setText("启动自动任务")
            self._append_log("未读回复自动任务已停止。")
            return
        self.unread_timer.start(self.unread_interval.value() * 1000)
        self.unread_start.setText("停止自动任务")
        target = self.unread_friend.text().strip() or "所有会话"
        self._append_log(
            f"未读回复自动任务已启动，每 {self.unread_interval.value()} 秒检查一次，目标：{target}。"
        )
        self.enqueue_unread_reply()

    def toggle_watch_timer(self) -> None:
        if self.watch_timer.isActive():
            self.watch_timer.stop()
            self.watch_start.setText("启动监听")
            self._append_log("消息监听自动任务已停止。")
            return
        targets = _parse_chat_targets(self.watch_targets.toPlainText())
        if not targets:
            QMessageBox.warning(self, "缺少监听对象", "请输入至少一个好友或群名")
            return
        self.watch_timer.start(self.watch_interval.value() * 1000)
        self.watch_start.setText("停止监听")
        self._append_log(
            f"消息监听已启动，每 {self.watch_interval.value()} 秒执行一次，"
            f"每轮监听 {self.watch_duration.text().strip() or '30s'}，对象：{', '.join(targets)}。"
        )
        self.enqueue_watch_chats()

    def enqueue_friend_check(self) -> None:
        if self._skip_auto_task_when_busy("好友检测"):
            return
        mode = "检测并自动同意好友申请" if self.friend_verify.isChecked() else "仅检测好友申请"
        self._append_log(f"正在创建好友检测任务：{mode}，最多处理 {self.friend_limit.value()} 条。")
        task = enqueue_check_new_friends(
            self.store,
            self.settings.default_account_id,
            verify=self.friend_verify.isChecked(),
            limit=self.friend_limit.value(),
            real=True,
        )
        self._task_created(task.id, task.type.value)

    def enqueue_unread_reply(self) -> None:
        if self._skip_auto_task_when_busy("未读回复"):
            return
        target = self.unread_friend.text().strip() or "所有会话"
        reply_mode = "回复全部未读消息" if self.unread_reply_all.isChecked() else "只回复每个会话最后一条未读消息"
        self._append_log(f"正在创建未读回复任务：目标 {target}，{reply_mode}。")
        task = enqueue_reply_unread(
            self.store,
            self.settings.default_account_id,
            friend=self.unread_friend.text().strip() or None,
            reply_all=self.unread_reply_all.isChecked(),
            real=True,
        )
        self._task_created(task.id, task.type.value)

    def _add_current_chat_to_watch(self) -> None:
        try:
            current = self.api.read_current_chat(dry_run=True)
        except Exception as exc:
            self._append_log(f"读取当前会话失败：{exc}")
            return
        chat_name = str(current.get("chat") or "").strip()
        if not chat_name or chat_name == "当前会话":
            self._append_log("未检测到主窗口中选中的会话，请在微信中点击左侧会话列表。")
            return
        existing = _parse_chat_targets(self.watch_targets.toPlainText())
        if chat_name in existing:
            self._append_log(f"「{chat_name}」已在监听列表中。")
            return
        current_text = self.watch_targets.toPlainText().strip()
        new_text = f"{current_text}\n{chat_name}".strip() if current_text else chat_name
        self.watch_targets.setPlainText(new_text)
        self._persist_form_state()
        if self.watch_timer.isActive():
            self._append_log(f"已将「{chat_name}」加入监听列表。下一轮监听将包含该会话。")
        else:
            self._append_log(f"已将「{chat_name}」加入监听列表。请到「消息监听」页面启动监听。")

    def enqueue_watch_chats(self) -> None:
        if self._skip_auto_task_when_busy("消息监听"):
            return
        targets = _parse_chat_targets(self.watch_targets.toPlainText())
        if not targets:
            self._append_log("消息监听没有配置对象，本轮跳过。")
            return
        duration = self.watch_duration.text().strip() or "30s"
        mode = "自动回复" if self.watch_auto_reply.isChecked() else "只监听不回复"
        self._append_log(
            f"正在创建消息监听任务：{mode}，对象 {', '.join(targets)}，每个会话监听 {duration}。"
        )
        task = enqueue_watch_chats(
            self.store,
            self.settings.default_account_id,
            friends=targets,
            duration=duration,
            auto_reply=self.watch_auto_reply.isChecked(),
            reply_all=self.watch_reply_all.isChecked(),
            select_sender=False,
            real=True,
        )
        self._task_created(task.id, task.type.value)

    def _skip_auto_task_when_busy(self, task_name: str) -> bool:
        if not (self.worker_running and self.worker.busy):
            return False
        self._append_log(f"服务正在操作微信，本轮{task_name}自动任务已跳过。")
        self.refresh_operation_state(force=True)
        return True

    def enqueue_add_friend(self) -> None:
        number = self.add_number.text().strip()
        if not number:
            QMessageBox.warning(self, "缺少手机号", "请输入手机号")
            return
        self._append_log(f"正在创建添加好友任务：手机号 {number}。")
        task = enqueue_add_friend(
            self.store,
            self.settings.default_account_id,
            number=number,
            greetings=self.add_greetings.text().strip() or "你好",
            remark=self.add_remark.text().strip() or None,
            real=True,
        )
        self._task_created(task.id, task.type.value)

    def save_settings(self) -> None:
        self.settings.agent_provider = self.provider_input.text().strip() or "openai_agents"
        self.settings.llm_base_url = self.base_url_input.text().strip()
        self.settings.llm_model = self.model_input.text().strip()
        self.settings.llm_api_key = SecretStr(self.api_key_input.text())
        self.settings.agent_timeout_seconds = float(self.timeout_input.value())
        self.settings.agent_context_message_count = int(self.context_count_input.value())
        self.settings.agent_system_prompt = self.prompt_input.toPlainText().strip()
        save_gui_settings(
            self.config_path,
            self.settings,
            api_key=self.api_key_input.text().strip(),
            form_state=self._collect_form_state(),
        )
        self.worker.settings = self.settings
        self.worker._agent = None
        self._append_log(f"Agent 设置已保存：模型 {self.settings.llm_model}，地址 {self.settings.llm_base_url}。")
        QMessageBox.information(self, "已保存", f"设置已保存到 {self.config_path}")

    def _collect_form_state(self) -> dict[str, Any]:
        """Collect current values of all form fields for persistence."""
        return {
            "friend_interval": self.friend_interval.value(),
            "friend_limit": self.friend_limit.value(),
            "friend_verify": self.friend_verify.isChecked(),
            "unread_interval": self.unread_interval.value(),
            "unread_friend": self.unread_friend.text().strip(),
            "unread_reply_all": self.unread_reply_all.isChecked(),
            "watch_interval": self.watch_interval.value(),
            "watch_duration": self.watch_duration.text().strip(),
            "watch_targets": self.watch_targets.toPlainText().strip(),
            "watch_auto_reply": self.watch_auto_reply.isChecked(),
            "watch_reply_all": self.watch_reply_all.isChecked(),
            "add_number": self.add_number.text().strip(),
            "add_greetings": self.add_greetings.text().strip(),
            "add_remark": self.add_remark.text().strip(),
            "moments_text": self.moments_text.toPlainText().strip(),
            "moments_medias": self.moments_medias.toPlainText().strip(),
        }

    def _persist_form_state(self) -> None:
        """Save current form values to the settings file."""
        save_gui_settings(
            self.config_path,
            self.settings,
            api_key=self.settings.llm_api_key.get_secret_value(),
            form_state=self._collect_form_state(),
        )

    def _task_created(self, task_id: str, task_type: str | None = None) -> None:
        self.statusBar().showMessage(f"任务已创建: {task_id}", 5000)
        label = _task_type_label(task_type) if task_type else "任务"
        self._append_log(f"{label}已加入队列，任务编号：{task_id[:8]}。")
        self.refresh_tasks()

    def refresh_all(self) -> None:
        self.refresh_tasks()
        self.refresh_logs()

    def refresh_operation_state(self, force: bool = False) -> None:
        if self.worker.busy:
            state = "busy"
            title = "正在操作微信"
            detail = "请暂时不要操作鼠标或键盘"
            status = "正在操作微信，请暂时不要操作鼠标键盘"
        elif not self.worker_running:
            state = "stopped"
            title = "服务未启动"
            detail = "启动服务后开始处理任务"
            status = "服务未启动"
        else:
            state = "idle"
            title = "服务空闲"
            detail = "可以正常使用电脑"
            status = "服务空闲"

        if not force and state == self._last_operation_state:
            return
        previous = self._last_operation_state
        self._last_operation_state = state
        self.operation_status_title.setText(title)
        self.operation_status_detail.setText(detail)
        parent = self.operation_status_title.parentWidget()
        if parent is not None:
            parent.setProperty("state", state)
            parent.style().unpolish(parent)
            parent.style().polish(parent)
        self.statusBar().showMessage(status)

        if state == "busy" and previous != "busy":
            self._append_log("服务正在操作微信。请暂时不要操作鼠标或键盘。")
        if previous == "busy" and state == "idle":
            self._append_log("微信操作已结束，服务回到空闲状态。")

    def refresh_uia_status(self) -> None:
        if self.worker_running and self.worker.busy:
            return
        if self._closing or self._worker_starting or self._uia_probe_running:
            return
        self._uia_probe_running = True
        self.uia_status_dot.setProperty("state", "unknown")
        self.uia_status_dot.setToolTip("正在后台检测 UIA 状态")
        self.uia_status_dot.style().unpolish(self.uia_status_dot)
        self.uia_status_dot.style().polish(self.uia_status_dot)
        thread = threading.Thread(
            target=self._probe_uia_in_background,
            name="wx-ui-uia-probe",
            daemon=True,
        )
        thread.start()

    def _probe_uia_in_background(self) -> None:
        try:
            probe = probe_wechat_uia(max_depth=4, max_nodes=120)
        except Exception as exc:
            probe = {
                "logged_in": False,
                "login_window": False,
                "uia_exposed": False,
                "reason": f"probe failed: {exc}",
            }
        self.uia_probe_finished.emit(probe)

    def _handle_uia_probe_result(self, probe: dict[str, Any]) -> None:
        self._uia_probe_running = False
        if self._closing:
            return
        if probe.get("uia_exposed") is True:
            state = "ok"
            title = "UIA 正常"
            panel_title = "UIA 已解锁"
            panel_detail = "微信 UIA 节点已正常暴露"
        elif probe.get("login_window"):
            state = "bad"
            title = "微信未登录"
            panel_title = "微信未登录"
            panel_detail = "请先登录微信"
        elif probe.get("logged_in"):
            state = "warn"
            title = "UIA 未解锁"
            panel_title = "UIA 未解锁"
            panel_detail = "微信已运行但 UIA 节点未暴露"
        else:
            state = "bad"
            title = "未找到微信主窗口"
            panel_title = "未找到微信"
            panel_detail = "请先启动并登录微信"

        reader_state = "屏幕阅读器标志已开启" if self.screen_reader_enabled else "屏幕阅读器标志未开启"
        reason = _uia_reason_label(str(probe.get("reason") or ""))
        self.uia_status_dot.setProperty("state", state)
        self.uia_status_dot.setToolTip(f"{title}\n{reader_state}\n{reason}")
        self.uia_status_dot.style().unpolish(self.uia_status_dot)
        self.uia_status_dot.style().polish(self.uia_status_dot)

        panel = self.uia_state_label.parentWidget()
        if panel is not None:
            panel.setProperty("state", state)
            panel.style().unpolish(panel)
            panel.style().polish(panel)
        self.uia_panel_dot.setProperty("state", state)
        self.uia_panel_dot.style().unpolish(self.uia_panel_dot)
        self.uia_panel_dot.style().polish(self.uia_panel_dot)
        self.uia_state_label.setText(panel_title)
        self.uia_detail_label.setText(panel_detail)
        self.uia_unlock_button.setVisible(
            state != "ok"
            and not self._uia_unlock_running
            and not self._startup_uia_bootstrap_running
        )

        signature = (state, reason)
        if signature != self._last_uia_signature:
            self._last_uia_signature = signature
            self._append_log(f"UIA 检测完成：{title}。{reader_state}。{reason}")

    def _try_unlock_uia(self) -> None:
        if self._uia_unlock_running or self._closing:
            return
        self._uia_unlock_running = True
        self.uia_unlock_button.setEnabled(False)
        self.uia_unlock_button.setText("正在解锁...")
        self.uia_state_label.setText("正在解锁 UIA")
        self.uia_detail_label.setText("设置屏幕阅读器标志并广播通知...")
        self._append_log("正在解锁微信 UIA...")
        threading.Thread(
            target=self._run_unlock_in_background,
            name="wx-ui-uia-unlock",
            daemon=True,
        ).start()

    def _run_unlock_in_background(self) -> None:
        wechat_path = self.settings.wechat_path
        try:
            success = try_unlock_without_restart(wechat_path=wechat_path)
        except Exception as exc:
            self.uia_unlock_finished.emit(False, str(exc))
            return
        if success:
            self.uia_unlock_finished.emit(True, "quick")
            return
        self.uia_unlock_finished.emit(False, "need_restart")

    def _handle_uia_unlock_result(self, success: bool, detail: str) -> None:
        self._uia_unlock_running = False
        self.uia_unlock_button.setEnabled(True)
        self.uia_unlock_button.setText("解锁 UIA")
        if success:
            if detail == "restart":
                self._append_log("UIA 解锁成功（已重启微信）。")
            else:
                self._append_log("UIA 解锁成功（无需重启微信）。")
            self.refresh_uia_status()
            return
        if detail == "need_restart":
            self._append_log("广播后 UIA 未解锁，需要重启微信。")
            answer = QMessageBox.question(
                self,
                "UIA 未解锁",
                "广播后微信未响应，需要重启微信才能解锁 UIA。\n\n是否重启微信？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer == QMessageBox.Yes:
                self._start_restart_unlock()
            return
        self._append_log(f"UIA 解锁失败：{detail}")

    def _bootstrap_uia_on_startup(self) -> None:
        if self._closing or self._startup_uia_bootstrap_running:
            return
        self._startup_uia_bootstrap_running = True
        self._append_log("程序启动时自动尝试解锁 UIA。")
        threading.Thread(
            target=self._run_startup_uia_bootstrap_in_background,
            name="wx-ui-uia-bootstrap",
            daemon=True,
        ).start()

    def _run_startup_uia_bootstrap_in_background(self) -> None:
        try:
            result = bootstrap_wechat_uia(
                wechat_path=self.settings.wechat_path,
                restart_if_needed=False,
            )
        except Exception as exc:
            self.uia_bootstrap_finished.emit(
                {"success": False, "mode": "error", "error": str(exc)}
            )
            return
        self.uia_bootstrap_finished.emit(result)

    def _handle_uia_bootstrap_result(self, result: dict[str, Any]) -> None:
        self._startup_uia_bootstrap_running = False
        if self._closing:
            return
        probe = result.get("probe") if isinstance(result.get("probe"), dict) else {}
        if result.get("success"):
            mode = str(result.get("mode") or "")
            if mode == "already":
                self._append_log("程序启动时 UIA 已可用。")
            elif mode == "quick":
                self._append_log("程序启动时已自动解锁 UIA（无需重启微信）。")
            else:
                self._append_log("程序启动时 UIA 已就绪。")
        else:
            detail = str(result.get("error") or probe.get("reason") or "未能自动解锁 UIA")
            self._append_log(f"程序启动时自动解锁 UIA 失败：{detail}")
        self.refresh_uia_status()

    def _start_restart_unlock(self) -> None:
        self._uia_unlock_running = True
        self.uia_unlock_button.setEnabled(False)
        self.uia_unlock_button.setText("重启微信中...")
        self.uia_state_label.setText("正在重启微信解锁")
        self.uia_detail_label.setText("关闭并重新启动微信，请稍候...")
        self._append_log("正在重启微信以解锁 UIA...")
        threading.Thread(
            target=self._run_restart_unlock_in_background,
            name="wx-ui-uia-restart",
            daemon=True,
        ).start()

    def _run_restart_unlock_in_background(self) -> None:
        try:
            success = unlock_with_narrator(
                wechat_path=self.settings.wechat_path,
                restart_wechat=True,
                startup_wait=35.0,
                stop_narrator_after_wechat=True,
            )
        except Exception as exc:
            self.uia_unlock_finished.emit(False, str(exc))
            return
        if success:
            self.uia_unlock_finished.emit(True, "restart")
        else:
            self.uia_unlock_finished.emit(False, "重启微信后 UIA 仍未解锁")

    def refresh_tasks(self) -> None:
        tasks = self.store.list_tasks(80)
        for table, task_types in self.task_tables:
            visible_tasks = [
                task for task in tasks if task_types is None or task.type.value in task_types
            ]
            table.setRowCount(len(visible_tasks))
            for row, task in enumerate(visible_tasks):
                values = [
                    task.id,
                    task.type.value,
                    task.status.value,
                    task.account_id,
                    str(task.attempts),
                    task.updated_at,
                    task.last_error or "",
                ]
                for col, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setToolTip(value)
                    table.setItem(row, col, item)
            table.resizeColumnsToContents()

    def refresh_logs(self) -> None:
        self._render_logs()

    def _clear_logs(self) -> None:
        confirm = QMessageBox.question(
            self,
            "清除日志",
            "确认清空当前运行日志文件？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        self.ui_log_lines.clear()
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log_path.write_text("", encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "清除失败", f"无法清空运行日志：{exc}")
        self._render_logs()
        self.statusBar().showMessage("日志已清除。", 3000)

    def _render_logs(self) -> None:
        if not hasattr(self, "log_view"):
            return
        worker_lines: list[str] = []
        if self.log_path.exists():
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
            worker_lines = _humanize_log_lines(text.splitlines()[-500:])[-240:]
        lines = [*self.ui_log_lines[-160:], *worker_lines]
        self.log_view.setPlainText(
            "\n".join(lines) or "暂无日志。启动服务后，这里会显示任务运行记录。"
        )
        self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())

    def _append_log(self, message: str) -> None:
        self.ui_log_lines.append(f"{_now_label()}  {message}")
        if len(self.ui_log_lines) > 300:
            self.ui_log_lines = self.ui_log_lines[-300:]
        self._render_logs()


def _asset_path(name: str) -> Path:
    return Path(__file__).resolve().parent / "assets" / name


def _humanize_log_lines(lines: list[str]) -> list[str]:
    pretty: list[str] = []
    for line in lines:
        summary = _humanize_log_line(line)
        if summary:
            pretty.append(summary)
    return pretty


def _now_label() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _parse_chat_targets(value: str) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[\n,，;；]+", value):
        text = part.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        rows.append(text)
    return rows


def _format_log_timestamp(value: str) -> str:
    if not value:
        return _now_label()
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return value
    return parsed.astimezone().strftime("%H:%M:%S")


def _task_type_label(task_type: str | None) -> str:
    labels = {
        "add_friend": "添加好友任务",
        "check_new_friends": "好友检测任务",
        "accept_new_friends": "自动同意好友任务",
        "send_message": "发送消息任务",
        "scan_messages": "聊天监听任务",
        "read_visible_messages": "读取可见消息任务",
        "check_unread_messages": "未读检测任务",
        "reply_chat": "聊天回复任务",
        "reply_unread": "未读回复任务",
        "watch_chats": "消息监听任务",
    }
    return labels.get(task_type or "", task_type or "任务")


def _event_label(event: str) -> str:
    labels = {
        "worker.started": "服务已启动",
        "worker.stopped": "服务已停止",
        "task.running": "任务开始执行",
        "task.succeeded": "任务执行完成",
        "task.blocked": "任务被阻塞",
        "task.failed": "任务执行失败",
        "watch.chats.start": "消息监听开始",
        "watch.chats.read": "消息监听完成",
        "watch.chats.skipped": "消息监听跳过回复",
        "watch.chats.message": "消息监听读到消息",
        "watch.chats.replied": "消息监听已回复",
        "watch.chats.failed": "消息监听失败",
        "reply.chat_messages.message": "监听会话读到消息",
    }
    return labels.get(event, event or "日志")


def _uia_reason_label(reason: str) -> str:
    if reason == "UIA exposed":
        return "微信 UIA 节点已正常暴露"
    if reason == "WeChat UIA is not exposed":
        return "未检测到微信 UIA 节点"
    if reason == "UIA probing is only available on Windows":
        return "当前平台不支持 UIA 检测，仅 Windows 可用"
    if reason.startswith("probe failed:"):
        return f"检测失败：{reason.removeprefix('probe failed:').strip()}"
    return reason


def _humanize_log_line(line: str) -> str | None:
    stripped = line.strip()
    if not stripped:
        return None
    if _looks_like_traceback_line(stripped):
        return None

    payload: dict[str, Any] | None = None
    timestamp = ""
    level = ""
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            payload = parsed
            timestamp = _format_log_timestamp(str(parsed.get("timestamp") or ""))
            level = str(parsed.get("level") or "").upper()

    match = re.match(r"^\[(?P<time>[^\]]+)\]\s+(?P<level>\w+)\s+-\s+(?P<body>.*)$", stripped)
    if payload is None and not match:
        return stripped

    if payload is None and match is not None:
        level = match.group("level").upper()
        timestamp = match.group("time")
        body = match.group("body")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            try:
                payload = ast.literal_eval(body)
            except (SyntaxError, ValueError):
                return f"{timestamp}  {level}  {body}"
        if not isinstance(payload, dict):
            return f"{timestamp}  {level}  {body}"

    event = str(payload.get("event") or "")
    task_id = str(payload.get("task_id") or "")
    short_id = task_id[:8] if task_id else "-"
    task_type = _task_type_label(str(payload.get("task_type") or ""))
    attempts = payload.get("attempts")

    if event == "worker.started":
        return f"{timestamp}  服务已启动，轮询间隔 {payload.get('poll_interval', '-')} 秒"
    if event == "worker.stopped":
        return f"{timestamp}  服务已停止"
    if event == "task.running":
        return f"{timestamp}  开始执行「{task_type}」#{short_id}，第 {attempts} 次尝试"
    if event == "task.succeeded":
        detail = _summarize_task_result(payload.get("result"))
        suffix = f"：{detail}" if detail else ""
        return f"{timestamp}  完成「{task_type}」#{short_id}{suffix}"
    if event == "task.blocked":
        return f"{timestamp}  「{task_type}」#{short_id} 被阻塞：{payload.get('error', '')}"
    if event == "task.failed":
        return f"{timestamp}  「{task_type}」#{short_id} 执行失败：{payload.get('error', '')}"
    detail = _summarize_payload(payload)
    suffix = f"：{detail}" if detail else ""
    return f"{timestamp}  {level}  {_event_label(event)}{suffix}".strip()


def _summarize_task_result(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    parts: list[str] = []
    if result.get("dry_run") is True:
        parts.append("模拟执行")
    labels = {
        "reply_count": "已回复",
        "chat_count": "涉及会话",
        "message_count": "读取消息",
        "new_message_count": "发现新消息",
        "message_source": "读取方式",
        "friend_filter": "会话过滤",
        "error_count": "失败会话",
    }
    for key, label in labels.items():
        if key in result and result[key] is not None:
            parts.append(f"{label} {result[key]}")
    return "，".join(parts)


def _summarize_payload(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in payload.items():
        if key in {"event", "timestamp", "level", "exception"}:
            continue
        if value is None or value == "":
            continue
        label = {
            "poll_interval": "轮询间隔",
            "task_id": "任务编号",
            "task_type": "任务类型",
            "account_id": "账号",
            "attempts": "尝试次数",
            "error": "错误",
            "result": "结果",
        }.get(key, key)
        if key == "task_type":
            value = _task_type_label(str(value))
        if key == "task_id":
            value = str(value)[:8]
        parts.append(f"{label} {value}")
    return "，".join(parts)


def _looks_like_traceback_line(line: str) -> bool:
    prefixes = (
        "Traceback ",
        "File ",
        "During handling ",
        "pywinauto.",
        "ElementNotFoundError",
        "TimeoutError",
        "^",
    )
    return line.startswith(prefixes) or line.startswith('File "')


def _load_ui_font() -> None:
    app = QApplication.instance()
    if app is None:
        return
    candidates = (
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/msyh.ttf"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("Z:/Windows/Fonts/msyh.ttc"),
        Path("Z:/Windows/Fonts/msyh.ttf"),
        Path("Z:/Windows/Fonts/simhei.ttf"),
    )
    for font_path in candidates:
        if not font_path.exists():
            continue
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            app.setFont(QFont(families[0], 10))
            return


def _spin(minimum: int, maximum: int, value: int) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(minimum, maximum)
    spin.setValue(value)
    spin.setFixedWidth(150)
    return spin


_STYLE = """
QMainWindow, QWidget {
    background: #f7f7f4;
    color: #171717;
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "Segoe UI";
    font-size: 13px;
}
#sidebar {
    background: #f1f0ec;
    border-right: 1px solid #dedbd3;
}
#brandPanel {
    background: #ffffff;
    border: 1px solid #dedbd3;
    border-radius: 8px;
}
#logoBox {
    background: transparent;
    border: none;
}
#uiaStatusDot {
    background: #8a8a8a;
    border: 2px solid #ffffff;
    border-radius: 6px;
}
#uiaStatusDot[state="ok"] {
    background: #1f9d55;
}
#uiaStatusDot[state="warn"] {
    background: #d79600;
}
#uiaStatusDot[state="bad"] {
    background: #d64545;
}
#uiaStatusDot[state="unknown"] {
    background: #8a8a8a;
}
#brandTitle {
    color: #111111;
    font-size: 15px;
    font-weight: 700;
}
#brandSubtitle {
    color: #6b6b6b;
    font-size: 11px;
}
#sectionLabel {
    color: #77736b;
    font-size: 11px;
    font-weight: 700;
    padding: 4px 8px;
}
#operationStatus {
    background: #ffffff;
    border: 1px solid #dedbd3;
    border-radius: 8px;
}
#operationStatus[state="idle"] {
    background: #f6fbf7;
    border: 1px solid #bdddc7;
}
#operationStatus[state="busy"] {
    background: #fff7dc;
    border: 1px solid #e2bd54;
}
#operationStatus[state="stopped"] {
    background: #ffffff;
    border: 1px solid #dedbd3;
}
#operationStatusTitle {
    background: transparent;
    color: #111111;
    font-size: 12px;
    font-weight: 700;
}
#operationStatusDetail {
    background: transparent;
    color: #5f5f5f;
    font-size: 11px;
}
#operationStatus[state="busy"] #operationStatusTitle {
    color: #7a4d00;
}
#operationStatus[state="busy"] #operationStatusDetail {
    color: #7a4d00;
}
#uiaStatus {
    background: #ffffff;
    border: 1px solid #dedbd3;
    border-radius: 8px;
}
#uiaStatus[state="ok"] {
    background: #f6fbf7;
    border: 1px solid #bdddc7;
}
#uiaStatus[state="warn"] {
    background: #fff7f5;
    border: 1px solid #e8b4b4;
}
#uiaStatus[state="bad"] {
    background: #fff7f5;
    border: 1px solid #e8b4b4;
}
#uiaPanelDot {
    background: #8a8a8a;
    border: none;
    border-radius: 4px;
}
#uiaPanelDot[state="ok"] {
    background: #1f9d55;
}
#uiaPanelDot[state="warn"] {
    background: #d64545;
}
#uiaPanelDot[state="bad"] {
    background: #d64545;
}
#uiaPanelDot[state="unknown"] {
    background: #8a8a8a;
}
#uiaStateLabel {
    background: transparent;
    color: #111111;
    font-size: 12px;
    font-weight: 700;
}
#uiaDetailLabel {
    background: transparent;
    color: #5f5f5f;
    font-size: 11px;
}
#uiaUnlockButton {
    background: #ffffff;
    color: #242424;
    border: 1px solid #c2bdb3;
    border-radius: 5px;
    padding: 4px 12px;
    font-size: 11px;
}
#uiaUnlockButton:hover {
    background: #f0efeb;
    border-color: #8a8a8a;
}
#uiaUnlockButton:pressed {
    background: #e0dfdb;
}
#uiaUnlockButton:disabled {
    color: #aaa;
    border-color: #ddd;
}
#nav {
    background: transparent;
    border: none;
    color: #242424;
    outline: 0;
}
#nav::item {
    min-height: 34px;
    padding: 7px 10px;
    border-radius: 7px;
}
#nav::item:hover {
    background: #e8e5dd;
}
#nav::item:selected {
    background: #111111;
    color: #ffffff;
}
#settingsButton {
    background: transparent;
    color: #242424;
    border: 1px solid #dedbd3;
    border-radius: 7px;
    text-align: left;
}
#settingsButton:hover {
    background: #ffffff;
}
#settingsButton[active="true"] {
    background: #111111;
    color: #ffffff;
}
#navButton {
    background: transparent;
    color: #242424;
    border: 1px solid #dedbd3;
    border-radius: 7px;
    text-align: left;
}
#navButton:hover {
    background: #ffffff;
}
#navButton[active="true"] {
    background: #111111;
    color: #ffffff;
}
#title {
    color: #111111;
    font-size: 22px;
    font-weight: 700;
}
#panel, QGroupBox {
    background: #ffffff;
    border: 1px solid #dedbd3;
    border-radius: 8px;
}
QGroupBox {
    margin-top: 10px;
    padding: 14px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 14px;
    padding: 0 6px;
    color: #333333;
}
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QTableWidget {
    background: #ffffff;
    border: 1px solid #d6d2c9;
    border-radius: 7px;
    padding: 7px;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QSpinBox:focus {
    border: 1px solid #111111;
}
QPlainTextEdit {
    font-family: "Cascadia Mono", "Consolas";
    font-size: 12px;
}
QPushButton {
    background: #ffffff;
    color: #171717;
    border: 1px solid #d6d2c9;
    border-radius: 7px;
    padding: 6px 12px;
    min-height: 24px;
}
QPushButton:hover {
    background: #f1f0ec;
}
#primaryButton {
    background: #111111;
    color: #ffffff;
    border: 1px solid #111111;
}
#primaryButton:hover {
    background: #2b2b2b;
}
QCheckBox {
    spacing: 8px;
}
QHeaderView::section {
    background: #f1f0ec;
    border: none;
    border-bottom: 1px solid #dedbd3;
    padding: 8px;
    font-weight: 600;
}
QTableWidget {
    gridline-color: #ece9e2;
    alternate-background-color: #fafaf8;
}
#statusBar {
    background: #f7f7f4;
    color: #5f5f5f;
}
"""
