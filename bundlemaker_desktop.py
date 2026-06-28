"""
BundleMaker Desktop Launcher
Opens BundleMaker as a native desktop window (no browser needed).
"""
import sys
import os
import socket
import threading
import time
import webbrowser

# ── Determine base path (works both from source and PyInstaller bundle) ──────
if getattr(sys, "frozen", False):
    BASE_DIR = sys._MEIPASS
    DATA_DIR = os.path.join(os.path.expanduser("~"), "BundleMaker")
else:
    BASE_DIR = os.path.dirname(__file__)
    DATA_DIR = BASE_DIR

os.environ["DATA_DIR"] = DATA_DIR
os.makedirs(os.path.join(DATA_DIR, "uploads"), exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, "output"), exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, "sessions"), exist_ok=True)


def find_free_port():
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def start_server(port):
    # Import here so PyInstaller can find the module
    sys.path.insert(0, BASE_DIR)
    from waitress import serve
    import app as flask_app
    flask_app.UPLOAD_FOLDER  = os.path.join(DATA_DIR, "uploads")
    flask_app.OUTPUT_FOLDER  = os.path.join(DATA_DIR, "output")
    flask_app.SESSIONS_FOLDER = os.path.join(DATA_DIR, "sessions")
    serve(flask_app.app, host="127.0.0.1", port=port, threads=4)


def wait_for_server(port, timeout=15):
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main():
    port = find_free_port()
    url  = f"http://127.0.0.1:{port}"

    # Start Flask in background thread
    t = threading.Thread(target=start_server, args=(port,), daemon=True)
    t.start()

    if not wait_for_server(port):
        print("BundleMaker server failed to start.", file=sys.stderr)
        sys.exit(1)

    # Try native window via pywebview; fall back to system browser
    try:
        import webview
        webview.create_window(
            "BundleMaker",
            url,
            width=1280,
            height=840,
            min_size=(900, 640),
            text_select=True,
        )
        webview.start()
    except Exception:
        webbrowser.open(url)
        # Keep process alive while browser is open
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
