"""The local web server answers with no-cache, so a code update always reaches the browser."""
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ember_bridge.serve import make_server  # noqa: E402


def test_no_cache() -> None:
    with tempfile.TemporaryDirectory() as d:
        Path(d, "app.js").write_text("console.log('v8');")
        with make_server(d, port=0) as httpd:
            port = httpd.server_address[1]
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/app.js", timeout=10) as r:
                assert r.status == 200
                assert r.headers["Cache-Control"] == "no-cache"
                assert r.read() == b"console.log('v8');"
            httpd.shutdown()


if __name__ == "__main__":
    test_no_cache()
    print("ok")
