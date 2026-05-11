from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QSplashScreen

from wxclient.logging_config import configure_logging

from .config import load_gui_settings
from .main_window import APP_NAME, MainWindow


def _close_pyinstaller_splash() -> None:
    try:
        import pyi_splash
    except Exception:
        return

    try:
        if getattr(pyi_splash, "is_alive", lambda: False)():
            pyi_splash.close()
    except Exception:
        return


def run_gui(config: Path | None = None) -> int:
    settings, config_path = load_gui_settings(config)
    configure_logging(level="INFO", log_file=settings.data_dir / "logs" / "ui.log")

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    icon_path = Path(__file__).resolve().parent / "assets" / "app-icon.ico"
    splash_path = Path(__file__).resolve().parent / "assets" / "app-logo-256.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    splash = None
    if splash_path.exists():
        pixmap = QPixmap(str(splash_path))
        if not pixmap.isNull():
            splash = QSplashScreen(pixmap, Qt.WindowStaysOnTopHint)
            splash.setEnabled(False)
            splash.showMessage(
                "正在启动 wxclient...",
                Qt.AlignBottom | Qt.AlignHCenter,
                Qt.white,
            )
            splash.show()
            app.processEvents()
    _close_pyinstaller_splash()
    window = MainWindow(settings=settings, config_path=config_path)
    window.resize(1280, 780)
    window.show()
    app.processEvents()
    if splash is not None:
        splash.finish(window)
    return app.exec()
