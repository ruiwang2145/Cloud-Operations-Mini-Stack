#!/usr/bin/env bash
# =============================================================================
# One-command local setup.
#
#   bash scripts/bootstrap.sh
#
# Creates a virtualenv, installs dependencies, writes a .env with a freshly
# generated SECRET_KEY, applies migrations and (optionally) creates a superuser.
#
# Safe to run repeatedly: every step checks whether it has already been done. The
# alternative -- a README with eleven copy-pasted commands -- is where "it works
# on my machine" comes from, because the twelfth command is always the one
# somebody skips.
#
# Environment switches:
#   PYTHON=python3.12      interpreter to build the venv from
#   VENV_DIR=.venv         where to put it
#   SKIP_SUPERUSER=1       do not prompt for an admin account
#   SKIP_INSTALL=1         assume dependencies are already installed
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VENV_DIR="${VENV_DIR:-.venv}"
PYTHON="${PYTHON:-python3}"
SKIP_SUPERUSER="${SKIP_SUPERUSER:-0}"
SKIP_INSTALL="${SKIP_INSTALL:-0}"

# --- output helpers ---------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    BOLD="$(printf '\033[1m')"; GREEN="$(printf '\033[32m')"
    YELLOW="$(printf '\033[33m')"; RED="$(printf '\033[31m')"; RESET="$(printf '\033[0m')"
else
    BOLD=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi

step() { printf '\n%s==>%s %s%s%s\n' "$GREEN" "$RESET" "$BOLD" "$*" "$RESET"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '%s  !  %s%s\n' "$YELLOW" "$*" "$RESET"; }
die()  { printf '%serror:%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

# --- 1. interpreter ---------------------------------------------------------
step "Checking the Python interpreter"
command -v "$PYTHON" >/dev/null 2>&1 || die "'$PYTHON' not found. Install Python 3.10+ or set PYTHON=/path/to/python."

PY_VERSION="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
info "using $PYTHON ($PY_VERSION)"

"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Python 3.10 or newer is required (Django 5.2 dropped older versions)."

# --- 2. virtualenv ----------------------------------------------------------
step "Preparing the virtual environment"
if [ -d "$VENV_DIR" ]; then
    info "$VENV_DIR already exists, reusing it"
else
    "$PYTHON" -m venv "$VENV_DIR"
    info "created $VENV_DIR"
fi

if [ -x "$VENV_DIR/bin/python" ]; then
    VENV_PY="$VENV_DIR/bin/python"
elif [ -x "$VENV_DIR/Scripts/python.exe" ]; then
    VENV_PY="$VENV_DIR/Scripts/python.exe"   # Git Bash on Windows
else
    die "no interpreter found inside $VENV_DIR"
fi

# --- 3. dependencies --------------------------------------------------------
if [ "$SKIP_INSTALL" = "1" ]; then
    step "Skipping dependency installation (SKIP_INSTALL=1)"
else
    step "Installing dependencies"
    "$VENV_PY" -m pip install --upgrade pip --quiet
    "$VENV_PY" -m pip install -r requirements.txt --quiet
    info "runtime dependencies installed"
    # Test tooling is optional: a reviewer who only wants to look at the service
    # should not have to wait for pytest and ruff.
    if [ "${INSTALL_DEV:-1}" = "1" ]; then
        "$VENV_PY" -m pip install -r requirements-dev.txt --quiet
        info "development dependencies installed"
    fi
fi

# --- 4. environment file ----------------------------------------------------
step "Preparing .env"
if [ -f .env ]; then
    info ".env already exists, leaving it untouched"
    info "(delete it and re-run to regenerate, or edit it by hand)"
else
    "$VENV_PY" - <<'PY'
import pathlib
import secrets

example = pathlib.Path(".env.example")
target = pathlib.Path(".env")

if not example.exists():
    raise SystemExit("error: .env.example is missing, cannot create .env")

text = example.read_text(encoding="utf-8")
key = secrets.token_urlsafe(50)

# Only the placeholder is replaced. Everything else keeps the value documented in
# the template, so the generated file stays readable and diffable.
if "SECRET_KEY=replace-me" not in text:
    raise SystemExit("error: .env.example no longer contains the SECRET_KEY placeholder")
text = text.replace("SECRET_KEY=replace-me", f"SECRET_KEY={key}", 1)

target.write_text(text, encoding="utf-8")
print("    wrote .env with a generated SECRET_KEY")
PY
fi

# --- 5. database ------------------------------------------------------------
step "Applying database migrations"
"$VENV_PY" manage.py migrate --noinput
info "database schema is up to date"

# --- 6. sanity check --------------------------------------------------------
step "Running Django's system checks"
"$VENV_PY" manage.py check

# --- 7. optional admin user -------------------------------------------------
if [ "$SKIP_SUPERUSER" != "1" ]; then
    step "Admin account (optional)"
    info "The admin is only needed to browse the data at /admin/."
    printf '    Create a superuser now? [y/N] '
    read -r answer || answer="n"
    case "$answer" in
        [yY]*) "$VENV_PY" manage.py createsuperuser ;;
        *)     info "skipped; run 'manage.py createsuperuser' later if you want one" ;;
    esac
fi

# --- 8. next steps ----------------------------------------------------------
cat <<EOF

${GREEN}${BOLD}Ready.${RESET}

  Start the service
    $VENV_PY manage.py runserver

  Then, in another terminal
    $VENV_PY scripts/smoke_test.py            # verify every endpoint
    $VENV_PY -m pytest                        # run the test suite

  Useful URLs
    http://localhost:8000/healthz/            liveness
    http://localhost:8000/readyz/             readiness (503 when a dependency is down)
    http://localhost:8000/metrics             Prometheus scrape target
    http://localhost:8000/api/tasks/          the REST API
    http://localhost:8000/api/slo/            availability SLO report
    http://localhost:8000/admin/              Django admin

  The whole stack, with Prometheus and Grafana
    docker compose up -d --build
    docker compose --profile demo up -d       # adds a load generator

EOF
