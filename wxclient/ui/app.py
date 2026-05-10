from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from wxclient.logging_config import configure_logging

from .config import load_gui_settings
from .main_window import APP_NAME, MainWindow


def run_gui(config: Path | None = None) -> int:
    settings, config_path = load_gui_settings(config)
    configure_logging(level="INFO", log_file=settings.data_dir / "logs" / "worker.log")

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    icon_path = Path(__file__).resolve().parent / "assets" / "app-icon.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    window = MainWindow(settings=settings, config_path=config_path)
    window.resize(1280, 780)
    window.show()
    return app.exec()
