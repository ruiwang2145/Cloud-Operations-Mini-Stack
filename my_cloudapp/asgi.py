"""ASGI entry point (reserved).

Nothing in the stack needs ASGI today -- the service is a plain request/response
API behind gunicorn. The entry point is kept so the project can move to an ASGI
server (uvicorn) without restructuring, which is the moment streaming responses
or websockets would actually be worth the extra operational surface.
"""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "my_cloudapp.settings")

application = get_asgi_application()
