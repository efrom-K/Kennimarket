"""Kennimarket как приложение macOS: тот же интерфейс (app.py) в нативном окне.

Запускается из Kennimarket.app (собирается macos/build_app.sh). Поднимает
Streamlit на своём порту, показывает его в окне WebKit и гасит сервер при
закрытии окна. Фоновый сбор лидов (если запущен) при этом продолжает работать.
"""
import socket
import subprocess
import sys
import time
from pathlib import Path

import webview

ROOT = Path(__file__).parent
PORT = 8502  # веб-версия по умолчанию на 8501 — не мешают друг другу
URL = f"http://localhost:{PORT}"

LOADING_HTML = """
<html><body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
font:15px -apple-system,sans-serif;color:#52514e;background:#fcfcfb">
<div style="text-align:center"><div style="font-size:48px">🏭</div><p>Запускаю Kennimarket…</p></div>
</body></html>
"""


def _listening() -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def _set_mac_app_identity() -> None:
    # Процесс — это python из .venv, поэтому без подмены macOS покажет в меню «Python».
    try:
        from AppKit import NSApplication, NSImage
        from Foundation import NSBundle

        NSBundle.mainBundle().infoDictionary()["CFBundleName"] = "Kennimarket"
        icon = ROOT / "macos" / "AppIcon.png"
        if icon.exists():
            NSApplication.sharedApplication().setApplicationIconImage_(NSImage.alloc().initWithContentsOfFile_(str(icon)))
    except Exception:
        pass


def main() -> None:
    server = None
    if not _listening():
        (ROOT / "logs").mkdir(exist_ok=True)
        log = open(ROOT / "logs" / "desktop-server.log", "w")
        server = subprocess.Popen(
            [sys.executable, "-m", "streamlit", "run", "app.py",
             "--server.headless", "true", "--server.port", str(PORT)],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
        )

    def load_when_ready(window):
        for _ in range(120):
            if _listening():
                window.load_url(URL)
                return
            time.sleep(0.5)
        window.load_html("<p style='font-family:sans-serif;padding:2em'>Не удалось запустить интерфейс. "
                         "Подробности в logs/desktop-server.log</p>")

    _set_mac_app_identity()
    webview.settings["ALLOW_DOWNLOADS"] = True  # кнопка «Скачать CSV»
    window = webview.create_window("Kennimarket", html=LOADING_HTML, width=1440, height=900, min_size=(960, 640))
    if server:
        # Cmd+Q завершает процесс через Cocoa в обход finally/atexit, поэтому гасим сервер по закрытию окна.
        window.events.closed += server.terminate
    webview.start(load_when_ready, window)


if __name__ == "__main__":
    main()
