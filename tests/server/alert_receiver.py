"""The rehearsal's alert receiver: what ``CAVE_ALERT_URL`` points at inside the machine.

It takes ntfy's format (docs/operations.md, "Alerts without Home Assistant"), an HTTP POST with
the text as the body and the title in a ``Title`` header, and records every request as one line
of JSON in the file named by its first argument: when it came, the path, the title, the body, and
whether it carried the token of the URL (``http://:<token>@127.0.0.1:8090/cave``, which ntfy reads
as HTTP basic authentication with an empty user name). A request without the token is answered
401, as ntfy does. The token comes from the environment, ``ALERT_TOKEN``; it is made in the job
and is no secret.

Standard library only; it runs on the machine's own Python as a systemd service, so that it is
there again after a reboot or a power cut.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

ADDRESS = ("127.0.0.1", 8090)


class Receiver(BaseHTTPRequestHandler):
    log_file = ""
    expected = ""

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        authorised = self.headers.get("Authorization") == self.expected
        entry = {
            "time": time.time(),
            "path": self.path,
            "title": self.headers.get("Title"),
            "authorised": authorised,
            "body": body,
        }
        with open(self.log_file, "a", encoding="utf-8") as log:
            log.write(json.dumps(entry) + "\n")
            log.flush()
            os.fsync(log.fileno())
        self.send_response(200 if authorised else 401)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        """Quiet: the record is the file."""


def main() -> None:
    Receiver.log_file = sys.argv[1]
    token = os.environ["ALERT_TOKEN"]
    Receiver.expected = "Basic " + base64.b64encode(f":{token}".encode()).decode()
    HTTPServer(ADDRESS, Receiver).serve_forever()


if __name__ == "__main__":
    main()
