"""Serve the web app on this computer only, and make the browser check for new files on every load.

`python -m http.server` lets the browser reuse an old app.js for a while, so after a code update the
previous map could still show. This server adds "Cache-Control: no-cache" to every answer.

Run (bash ember.sh serve does it):
    python -m ember_bridge.serve --port 8000 --directory web
"""
from __future__ import annotations

import argparse
import functools
import http.server


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()


def make_server(directory: str, port: int = 8000, bind: str = "127.0.0.1") -> http.server.ThreadingHTTPServer:
    return http.server.ThreadingHTTPServer((bind, port), functools.partial(NoCacheHandler, directory=directory))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--directory", default="web")
    a = ap.parse_args()
    with make_server(a.directory, a.port, a.bind) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
