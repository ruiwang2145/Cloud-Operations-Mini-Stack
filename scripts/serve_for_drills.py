#!/usr/bin/env python
"""Serve the WSGI application without Django's development-server startup checks.

    python scripts/serve_for_drills.py                 # normal start
    DATABASE_URL=postgresql://bad:bad@127.0.0.1:59999/bad \
        python scripts/serve_for_drills.py             # readiness drill

Why this exists
---------------
The readiness drill in `docs/incident-drills.md` requires the service to be *up*
while its database is *down*. `manage.py runserver` cannot do that: it calls
`check_migrations()` during startup, which needs a database connection, so the
process exits before it can ever answer `/readyz/`.

That is a development-server artefact, not application behaviour. gunicorn does
no such check -- it imports the WSGI application and starts serving -- which is
exactly why a production container *can* be up-but-not-ready. This launcher
reproduces gunicorn's behaviour using only the standard library, so the drill
exercises what actually happens in production.

What it is not
--------------
Not a production server. It is single-process with a thread per request, has no
graceful reload and does not implement gunicorn's worker management. It exists to
make one drill possible without Docker.

See docs/native-monitoring.md.
"""
from __future__ import annotations

import os
import socketserver
import sys
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

HOST = os.getenv("SERVE_HOST", "127.0.0.1")
PORT = int(os.getenv("SERVE_PORT", "8000"))

# Running this file puts `scripts/` on sys.path, not the project root, so
# `my_cloudapp` would not be importable. The root is added explicitly so the
# script works from any working directory.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "my_cloudapp.settings")

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()


class ThreadedWSGIServer(socketserver.ThreadingMixIn, WSGIServer):
    """One thread per request.

    The default WSGIServer is single-threaded, which would serialise Prometheus
    scrapes behind readiness probes -- and a readiness probe that spends five
    seconds timing out would then stall the scrape as well, so the drill would
    distort the very metric it is measuring.
    """

    daemon_threads = True
    allow_reuse_address = True


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - signature fixed by the base class
        # The application emits its own structured access log. The dev server's
        # free-text line would be a second, contradictory source of truth.
        pass


def main() -> int:
    database = os.getenv("DATABASE_URL", "(default: sqlite)")
    print(f"serving on http://{HOST}:{PORT} with no startup database checks")
    print(f"  DATABASE_URL = {database}")
    print("  (if the database is unreachable, /healthz/ still answers 200 and")
    print("   /readyz/ returns 503 -- which is the behaviour being drilled)")
    with make_server(
        HOST,
        PORT,
        application,
        server_class=ThreadedWSGIServer,
        handler_class=QuietHandler,
    ) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
