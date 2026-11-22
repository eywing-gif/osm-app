#!/usr/bin/env python3
"""Desktop window for Spatial Analytics.

Starts the FastAPI backend (from ./venv) on a free local port, shows the
frontend in a native GTK/WebKit window and stops the backend when the window
is closed. Uses the system Python, because GTK/WebKit bindings (python3-gi,
gir1.2-webkit2-4.1) come from the system packages, not from pip.
"""
import ctypes
import signal
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

try:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import GLib, Gtk, WebKit2  # noqa: E402
except (ImportError, ValueError):           # no GTK / WebKit: the app opens in the web browser instead
    gi = None

APP_ID = "spatial-analytics"
ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / "venv" / "bin" / "python"
ICON = ROOT / "frontend" / "icon.svg"
LOG = Path.home() / ".cache" / APP_ID / "server.log"

LOADING_HTML = """<html><body style="font:15px system-ui;display:grid;place-items:center;height:90vh;color:#555">
<div>Starting Spatial Analytics…</div></body></html>"""


def die_with_parent():
    """Linux: stop the backend even if this window process gets killed."""
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG


PREFERRED_PORT = 8765  # fixed, so the page keeps its saved settings (theme, map position)


def free_port() -> int:
    for port in (PREFERRED_PORT, 0):  # 0 = any free port, if 8765 is taken
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return s.getsockname()[1]


class App:
    def __init__(self):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}/"
        LOG.parent.mkdir(parents=True, exist_ok=True)
        self.log = open(LOG, "w")
        self.server = subprocess.Popen(
            [str(PYTHON), "-m", "uvicorn", "backend.main:app",
             "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT,
            preexec_fn=die_with_parent)

        self.win = Gtk.Window(title="Spatial Analytics")
        self.win.set_default_size(1200, 800)
        if ICON.exists():
            self.win.set_icon_from_file(str(ICON))
        self.win.connect("destroy", self.quit)

        self.view = WebKit2.WebView()
        try:   # drop pages cached by earlier versions of the app
            self.view.get_context().clear_cache()
        except Exception:
            pass
        self.view.connect("decide-policy", self.on_policy)
        self.view.connect("create", self.on_create)         # window.open(): the station window
        self.extra_windows = []
        self.view.load_html(LOADING_HTML, None)
        self.win.add(self.view)
        self.win.show_all()

        self.tries = 0
        GLib.timeout_add(200, self.wait_for_server)

    def wait_for_server(self):
        if self.server.poll() is not None:  # backend crashed
            self.log.flush()
            msg = GLib.markup_escape_text(LOG.read_text()[-3000:])
            self.view.load_html(
                "<html><body style='font:14px system-ui;padding:20px'><h2>Backend failed to start</h2>"
                "<p>Is PostgreSQL running and the <code>osm</code> database available?</p>"
                f"<pre style='white-space:pre-wrap;background:#f4f4f4;padding:10px'>{msg}</pre></body></html>", None)
            return False
        try:
            urllib.request.urlopen(self.url, timeout=0.5)
        except OSError:
            self.tries += 1
            return self.tries < 150  # give up after ~30 s
        self.view.load_uri(self.url)
        return False

    def on_create(self, view, navigation_action):
        """A page of the app opens a new window (e.g. the station details): give it a window of its own."""
        win = Gtk.Window(title="Spatial Analytics")
        win.set_default_size(660, 900)
        win.set_transient_for(self.win)
        if ICON.exists():
            win.set_icon_from_file(str(ICON))
        new_view = WebKit2.WebView.new_with_related_view(view)
        new_view.connect("decide-policy", self.on_policy)
        new_view.connect("create", self.on_create)
        new_view.connect("notify::title", lambda v, _p: win.set_title(v.get_title() or "Spatial Analytics"))
        new_view.connect("close", lambda *_: win.destroy())
        win.connect("destroy", lambda *_: self.extra_windows.remove(win) if win in self.extra_windows else None)
        win.add(new_view)
        win.show_all()
        self.extra_windows.append(win)
        return new_view

    def on_policy(self, view, decision, kind):
        """Open links leaving the app (OSM website, shop websites) in the normal browser."""
        if kind in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
                    WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
            uri = decision.get_navigation_action().get_request().get_uri()
            if uri.startswith(("http://", "https://")) and not uri.startswith(self.url):
                Gtk.show_uri_on_window(self.win, uri, Gtk.get_current_event_time())
                decision.ignore()
                return True
        return False

    def quit(self, *_):
        self.server.terminate()
        try:
            self.server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.server.kill()
        Gtk.main_quit()


def browser_mode():
    """Without GTK / WebKit: run the backend and show the app in the normal web browser (Ctrl+C stops it)."""
    import time
    import webbrowser
    port = free_port()
    url = f"http://127.0.0.1:{port}/"
    server = subprocess.Popen([str(PYTHON), "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port)], cwd=ROOT)
    for _ in range(150):
        try:
            urllib.request.urlopen(url, timeout=0.5)
            break
        except OSError:
            if server.poll() is not None:
                sys.exit("The backend did not start (see above). Is PostgreSQL running? Run:  sh install.sh")
            time.sleep(0.2)
    print(f"Spatial Analytics: {url}  (no desktop window: WebKitGTK missing) – stop with Ctrl+C")
    webbrowser.open(url)
    try:
        server.wait()
    except KeyboardInterrupt:
        server.terminate()


def main():
    if not PYTHON.exists() or subprocess.run([str(PYTHON), "-c", "import fastapi"], capture_output=True).returncode:
        sys.exit("The app is not installed completely (venv/). Run in the app folder:  sh install.sh")
    if gi is None:
        return browser_mode()
    GLib.set_prgname(APP_ID)  # matches the .desktop file, so GNOME shows the right icon
    GLib.set_application_name("Spatial Analytics")
    app = App()
    try:  # newer PyGObject moved this into GLibUnix
        gi.require_version("GLibUnix", "2.0")
        from gi.repository import GLibUnix
        signal_add = GLibUnix.signal_add
    except (ValueError, ImportError):
        signal_add = GLib.unix_signal_add
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal_add(GLib.PRIORITY_DEFAULT, sig, app.quit)
    Gtk.main()


if __name__ == "__main__":
    main()
