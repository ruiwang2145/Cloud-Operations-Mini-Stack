"""WSGI entry point.

Gunicorn imports ``my_cloudapp.wsgi:application``; PythonAnywhere points its WSGI
configuration file at the same object.
"""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "my_cloudapp.settings")

application = get_wsgi_application()
