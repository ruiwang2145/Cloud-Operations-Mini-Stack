"""Application config for the ``ops`` app.

``ops`` holds everything that exists to make the service *operable* rather than
to serve a business requirement: health probes, metrics, structured logging, the
SLO reporter and the fault-injection endpoints used by the incident drills.

Keeping these in their own app rather than scattering them across the project is
what makes the claim "the operational surface is separable from the product
surface" true in practice -- you can read `ops/` top to bottom and know exactly
what the service exposes to an operator.
"""
from django.apps import AppConfig


class OpsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "ops"
    verbose_name = "Operations & observability"

    def ready(self) -> None:
        """Publish build metadata as soon as the app is loaded.

        Doing this here rather than in a view means ``app_build_info`` is present
        on the very first scrape, so "which version is this?" is answerable
        immediately after a deploy -- including during the window when the new
        version is failing to serve requests.
        """
        from django.conf import settings

        from .metrics import describe_build

        describe_build(
            service=settings.SERVICE_NAME,
            version=settings.SERVICE_VERSION,
            environment=settings.DEPLOY_ENV,
        )
