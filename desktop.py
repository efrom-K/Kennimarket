"""Kennimarket как приложение macOS: тот же интерфейс (app.py) в нативном окне.

Запускается из Kennimarket.app (собирается macos/build_app.sh). Поднимает
Streamlit на своём порту, показывает его в окне WebKit и гасит сервер при
закрытии окна. Фоновый сбор лидов (если запущен) при этом продолжает работать.
"""
import re
import socket
import subprocess
import sys
import time
import webbrowser
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


# WKWebView в pywebview пропускает наружу только клики по <a target=_blank>, а
# window.open() (им открывает ссылки таблица Streamlit) молча игнорирует.
# Поэтому перехватываем оба способа и открываем ссылки сами.
OPEN_LINKS_JS = """
if (!window.__kmLinks) {
  window.__kmLinks = true;
  const ext = u => { if (u && /^https?:|^tg:/.test(String(u)) && !String(u).startsWith(location.origin))
                       { window.pywebview.api.open_external(String(u)); return true; } return false; };
  window.open = (u) => { ext(u); return null; };
  document.addEventListener('click', e => {
    const a = e.target.closest && e.target.closest('a[href]');
    if (a && ext(a.href)) { e.preventDefault(); e.stopPropagation(); }
  }, true);
}
"""


def telegram_app_url(url: str) -> str:
    """t.me/name[/post] -> tg://resolve…: сразу в приложении Telegram, без браузера."""
    m = re.match(r"https?://t\.me/([A-Za-z0-9_]{4,})(?:/(\d+))?/?$", url)
    if not m:
        return url
    return f"tg://resolve?domain={m.group(1)}" + (f"&post={m.group(2)}" if m.group(2) else "")


class Api:
    def open_external(self, url: str) -> None:
        webbrowser.open(telegram_app_url(url))


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
    window = webview.create_window("Kennimarket", html=LOADING_HTML, width=1440, height=900, min_size=(960, 640),
                                   js_api=Api())
    window.events.loaded += lambda: window.evaluate_js(OPEN_LINKS_JS)
    if server:
        # Cmd+Q завершает процесс через Cocoa в обход finally/atexit, поэтому гасим сервер по закрытию окна.
        window.events.closed += server.terminate
    webview.start(load_when_ready, window)




def _self_check() -> None:
    assert telegram_app_url("https://t.me/mossdelka/124221") == "tg://resolve?domain=mossdelka&post=124221"
    assert telegram_app_url("https://t.me/Terner_chat") == "tg://resolve?domain=Terner_chat"
    assert telegram_app_url("https://example.com/x") == "https://example.com/x"


if __name__ == "__main__":
    if "--check" in sys.argv:
        _self_check()
        print("ok")
    else:
        main()
