"""The container contract.

Three files have to agree before ``docker compose up`` produces a service that
answers requests: ``Dockerfile`` decides who the process runs as and what it may
write, ``docker-compose.yml`` decides what it is told, and
``scripts/docker-entrypoint.sh`` decides what it does before the server starts.

They are written in three different languages, and when they disagree none of
them looks wrong on its own. That is the exact shape of the first bug this stack
produced in a container::

    PermissionError: [Errno 13] Permission denied: '/app/staticfiles'

The entrypoint ran ``collectstatic``; ``/app`` was owned by root because
``WORKDIR`` created it; ``COPY --chown`` had re-owned the files *inside* it but
not the directory itself, so the non-root user could not create anything there;
and ``restart: unless-stopped`` turned a two-second crash into a service that was
never reachable. Every individual file was correct.

These tests encode the invariants the image depends on, so that deleting a line
from the Dockerfile fails here -- in eight seconds, with a message that names the
line -- instead of in a container two minutes into a demo.

Nothing here needs Docker. It is all static agreement between files, which is the
point: the failure mode being guarded against is invisible until runtime.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.conf import settings

BASE = Path(settings.BASE_DIR)
DOCKERFILE = BASE / "Dockerfile"
COMPOSE_FILE = BASE / "docker-compose.yml"
DOCKERIGNORE = BASE / ".dockerignore"
ENTRYPOINT = BASE / "scripts" / "docker-entrypoint.sh"


def dockerfile_text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def instruction(name: str) -> str | None:
    """The value of the last ``NAME value`` line, ignoring comments.

    Last, not first, so that a later instruction overrides an earlier one the way
    Docker resolves it.
    """
    for line in reversed(dockerfile_text().splitlines()):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.upper().startswith(f"{name.upper()} "):
            return stripped.split(None, 1)[1].strip()
    return None


def instruction_line(name: str) -> int:
    for index, line in enumerate(dockerfile_text().splitlines()):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.upper().startswith(f"{name.upper()} "):
            return index
    raise AssertionError(f"no {name} instruction in the Dockerfile")


def compose() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def compose_environment(service: str) -> dict:
    return compose()["services"][service].get("environment") or {}


class TestTheRuntimeUser:
    def test_the_process_does_not_run_as_root(self):
        # "root inside a container" is one kernel bug away from root on the host.
        user = instruction("USER")
        assert user is not None, "the Dockerfile never drops privileges with USER"
        assert user not in {"root", "0", "root:root"}

    def test_the_user_is_created_before_it_is_switched_to(self):
        lines = dockerfile_text().splitlines()
        useradd_at = next((i for i, line in enumerate(lines) if "useradd" in line), None)
        assert useradd_at is not None, "no useradd to create the runtime user"
        assert useradd_at < instruction_line("USER"), (
            "USER names a user that has to exist already; create it before the switch"
        )


class TestTheStaticRootIsWritable:
    """The regression guard for the crash loop in the module docstring."""

    def static_root_in_image(self) -> str:
        """STATIC_ROOT as the container sees it: WORKDIR joined to the setting."""
        workdir = instruction("WORKDIR")
        assert workdir, "the Dockerfile sets no WORKDIR"
        relative = Path(settings.STATIC_ROOT).relative_to(BASE).as_posix()
        return f"{workdir}/{relative}"

    def test_the_image_creates_the_directory_the_setting_points_at(self):
        # Ties the Dockerfile to settings.py: move STATIC_ROOT and this fails
        # rather than producing a container that cannot start.
        target = self.static_root_in_image()
        assert f"mkdir -p {target}" in dockerfile_text(), (
            f"the Dockerfile does not create {target}, which is STATIC_ROOT. "
            "collectstatic runs at container start and will fail without it."
        )

    def test_the_directory_is_owned_by_the_runtime_user(self):
        target = self.static_root_in_image()
        user = instruction("USER")
        assert f"chown {user}:{user} {target}" in dockerfile_text(), (
            f"{target} must be owned by {user}. /app is created by WORKDIR as "
            "root, and COPY --chown only re-owns the files it copies, not the "
            "directory they land in -- so without this the non-root user cannot "
            "create anything inside it."
        )

    def test_the_directory_is_handed_over_before_privileges_are_dropped(self):
        # Order is the entire fix: chowning it after USER cannot work, and
        # creating it as the non-root user cannot either.
        target = self.static_root_in_image()
        user = instruction("USER")
        lines = dockerfile_text().splitlines()
        chown_at = next(
            (i for i, line in enumerate(lines) if f"chown {user}:{user} {target}" in line),
            None,
        )
        assert chown_at is not None, f"no chown of {target}"
        assert chown_at < instruction_line("USER"), (
            "the chown must happen while the image is still being built as root"
        )

    def test_the_build_context_excludes_staticfiles(self):
        # This is *why* the directory has to be created in the image: it is not
        # in the build context, so COPY cannot have brought it along.
        entries = {line.strip() for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()}
        assert "staticfiles/" in entries


class TestTheHealthcheck:
    def test_it_probes_liveness_not_readiness(self):
        # A container whose database is unreachable is not ready, but it is not
        # broken enough to be killed: killing it turns a database outage into a
        # restart loop that hammers the database harder.
        text = dockerfile_text()
        assert "HEALTHCHECK" in text
        probes = [
            line
            for line in text.splitlines()
            if "curl" in line and "http" in line and not line.strip().startswith("#")
        ]
        assert probes, "the HEALTHCHECK has no probe command"
        for probe in probes:
            assert "/healthz/" in probe
            assert "/readyz/" not in probe

    def test_it_allows_for_a_slow_first_boot(self):
        # Without a start period the first probe fails and the container is
        # reported unhealthy during a perfectly normal boot.
        assert "--start-period=" in dockerfile_text()

    def test_it_has_a_retry_budget(self):
        assert "--retries=" in dockerfile_text()


class TestTheEntrypointContract:
    def test_it_is_invoked_by_absolute_path(self):
        # The exec form does not go through a shell, so a relative path would be
        # resolved against WORKDIR rather than PATH -- which happens to work, and
        # couples the entrypoint to a WORKDIR change three lines up.
        assert 'ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]' in dockerfile_text()

    def test_it_is_made_executable_in_the_image(self):
        assert "chmod +x scripts/docker-entrypoint.sh" in dockerfile_text()

    def test_the_server_becomes_pid_1(self):
        # `exec "$@"` is what gives gunicorn the signals, so `docker stop` is a
        # graceful shutdown instead of a kill that drops in-flight requests.
        assert 'exec "$@"' in ENTRYPOINT.read_text(encoding="utf-8")

    def test_it_fails_fast_on_an_error(self):
        # Without `set -e` a failed migrate or collectstatic is logged and then
        # stepped over, and the container serves traffic against a half-built
        # schema while reporting itself healthy.
        assert "set -eu" in ENTRYPOINT.read_text(encoding="utf-8")

    def test_every_switch_it_reads_is_declared_in_compose(self):
        """A switch that never reaches the container looks like it worked.

        ``RUN_MIGRATIONS=false docker compose up`` with no ``RUN_MIGRATIONS`` line
        in the compose environment does not skip migrations. The variable simply
        never arrives, the default ``true`` applies, and the operator is left
        believing a setting they passed had an effect.
        """
        entrypoint = ENTRYPOINT.read_text(encoding="utf-8")
        switches = set(re.findall(r"\$\{(RUN_[A-Z_]+):-", entrypoint))
        assert switches, "no RUN_* switches found in the entrypoint"
        declared = set(compose_environment("web"))
        missing = sorted(switches - declared)
        assert not missing, (
            f"{missing} are read by the entrypoint but not passed by the compose "
            "web service, so overriding them from the shell would do nothing"
        )


class TestMetricsAreNotSlicedByWorkers:
    def test_gunicorn_runs_a_single_worker(self):
        # prometheus_client keeps counters in process memory. With N workers there
        # are N registries and Prometheus scrapes whichever one answers, so the
        # dashboards show a fraction of the real traffic changing unpredictably
        # from scrape to scrape. Metrics that look fine and are wrong.
        assert '"--workers", "1"' in dockerfile_text()


class TestLoggingInAContainer:
    def test_the_image_logs_to_stdout(self):
        # A file handler inside a container produces logs the log driver cannot
        # collect, in a writable layer that grows until the disk fills.
        assert "LOG_TO_FILE=False" in dockerfile_text()


class TestFaultEndpointsAreOptIn:
    def test_the_image_does_not_enable_them(self):
        # compose turns them on so the incident drills can run. The image must
        # not, or every deployment inherits a public endpoint that raises 500 on
        # request.
        assert "ENABLE_FAULT_ENDPOINTS" not in dockerfile_text()

    def test_compose_enables_them_so_the_drills_can_run(self):
        assert compose_environment("web").get("ENABLE_FAULT_ENDPOINTS") == "true"


class TestTheBuildContext:
    def test_secrets_and_history_are_excluded(self):
        # Anything not excluded is baked into a layer. `.env` holds credentials
        # and `.git` holds every secret ever committed and later removed.
        entries = {line.strip() for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()}
        for entry in (".env", ".git"):
            assert entry in entries, f"{entry} is not excluded from the build context"

    def test_dependencies_are_copied_before_the_application_code(self):
        # Layer caching, and the single biggest lever on CI build time: editing a
        # Python file must not reinstall the dependency set.
        text = dockerfile_text()
        assert text.index("COPY requirements.txt") < text.index("COPY --chown")


class TestTheComposeService:
    def test_the_database_is_gated_on_health_not_on_start_order(self):
        # `depends_on` alone only waits for the container to be created. Postgres
        # accepts connections a second or two later, which is long enough for a
        # first deploy to fail.
        depends = compose()["services"]["web"].get("depends_on") or {}
        assert depends.get("db", {}).get("condition") == "service_healthy"

    def test_the_database_healthcheck_actually_queries_the_database(self):
        db = compose()["services"]["db"]
        probe = " ".join(db["healthcheck"]["test"])
        assert "pg_isready" in probe

    def test_the_web_service_restarts_unless_stopped(self):
        # Also what turned the staticfiles permission error into a crash loop --
        # which is the correct behaviour, and the reason the bug was visible at
        # all rather than surfacing as a single exited container.
        assert compose()["services"]["web"].get("restart") == "unless-stopped"
