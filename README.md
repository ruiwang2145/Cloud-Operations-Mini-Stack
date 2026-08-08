# ☁️ Cloud Operations Mini Stack

[![Python](https://img.shields.io/badge/Python-3.10-blue.svg)](https://www.python.org/)
[![Django](https://img.shields.io/badge/Django-5.2-green.svg)](https://www.djangoproject.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Neon-blue.svg)](https://neon.tech/)
[![Sentry](https://img.shields.io/badge/Sentry-APM%2BErrors-purple.svg)](https://sentry.io/)
[![PythonAnywhere](https://img.shields.io/badge/PythonAnywhere-WSGI-orange.svg)](https://www.pythonanywhere.com/)
[![UptimeRobot](https://img.shields.io/badge/UptimeRobot-Monitoring-brightgreen.svg)](https://uptimerobot.com/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

An **end-to-end production-grade web service** demonstrating complete software lifecycle management and observability. Designed, implemented, and deployed as a minimal yet representative cloud operations stack that integrates out-of-the-box tools for monitoring, error tracking, logging, and continuous uptime verification.

---

## 📋 Table of Contents

- [Architecture](#-architecture)
- [Project Structure](#-project-structure)
- [Observability Stack](#-observability-stack)
- [API Endpoints](#-api-endpoints)
- [Incident Drill Framework](#-incident-drill-framework)
- [Deployment](#-deployment)
- [Tech Stack](#-tech-stack)
- [Installation & Local Development](#-installation--local-development)
- [Documentation](#-documentation)
- [License](#-license)

---

## 🏗 Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     UptimeRobot                          │
│              (External health check polling)             │
└──────────────────────┬──────────────────────────────────┘
                       │ GET /healthz/ (every 5 min)
                       ▼
┌─────────────────────────────────────────────────────────┐
│                  PythonAnywhere (WSGI)                    │
│  ┌───────────────────────────────────────────────────┐  │
│  │              Gunicorn (WSGI Server)               │  │
│  │  ┌─────────────────────────────────────────────┐ │  │
│  │  │           Django 5.2 Application            │ │  │
│  │  │  ┌───────────┐  ┌───────────────────────┐  │ │  │
│  │  │  │  tasks    │  │   Observability       │  │ │  │
│  │  │  │  app      │  │   • Sentry SDK        │  │ │  │
│  │  │  │  (models) │  │   • JSON Logging      │  │ │  │
│  │  │  │           │  │   • /healthz/ endpoint │  │ │  │
│  │  │  └───────────┘  │   • /boom/ & sentry-   │  │ │  │
│  │  │                 │     debug/ test routes │  │ │  │
│  │  │                 └───────────────────────┘  │ │  │
│  │  └─────────────────────────────────────────────┘ │  │
│  └───────────────────────────────────────────────────┘  │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│              PostgreSQL (Neon Serverless)                 │
│          Managed cloud database with connection pooling   │
└─────────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│                    Sentry.io                              │
│       Error tracking · APM traces · Performance          │
│       profiling · Alerting · Issue management            │
└─────────────────────────────────────────────────────────┘
```

All configuration is externalized via `.env` and `dotenv`, with zero hardcoded secrets in source code.

---

## 📁 Project Structure

```
cloud-ops-mini-stack/
├── manage.py                         # Django management CLI
├── .gitignore                        # Python, Django, env, IDE ignores
├── README.md                         # You are here
├── my_cloudapp/                      # Django project package
│   ├── __init__.py
│   ├── settings.py                   # Centralized config: DB, Sentry, Logging
│   ├── urls.py                       # Route definitions + health/error endpoints
│   ├── wsgi.py                       # WSGI entry point for Gunicorn
│   └── asgi.py                       # ASGI entry point (reserved)
└── tasks/                            # Django app (minimal business logic)
    ├── __init__.py
    ├── admin.py
    ├── apps.py
    ├── models.py                     # Task model (title, done)
    ├── views.py
    ├── tests.py
    └── migrations/
        ├── __init__.py
        └── 0001_initial.py
```

---

## 🔭 Observability Stack

The project implements a **four-layer observability architecture**:

| Layer | Tool | What It Covers | Configuration |
|---|---|---|---|
| **Health Check** | `/healthz/` endpoint + **UptimeRobot** | External uptime polling every 5 minutes; alerts on downtime | `urls.py`, UptimeRobot dashboard |
| **Error Tracking** | **Sentry SDK** (DjangoIntegration) | Automatic capture of unhandled exceptions, stack traces, request context | `settings.py` → `SENTRY_DSN` env var |
| **Performance Monitoring** | **Sentry APM** (traces + profiles) | Request latency, slow endpoints, database query performance | `SENTRY_TRACES_SAMPLE_RATE`, `SENTRY_PROFILES_SAMPLE_RATE` |
| **Structured Logging** | **JSON Logging** (console + file) | Machine-parseable log output to stdout and `app.log` | `settings.py` → `LOGGING` dict |

### Sentry Configuration

```python
# In settings.py — all values from environment variables
sentry_sdk.init(
    dsn=SENTRY_DSN,
    integrations=[DjangoIntegration()],
    environment=SENTRY_ENV,            # "prod" / "staging"
    traces_sample_rate=0.2,            # APM sampling at 20%
    profiles_sample_rate=0.0,          # Performance profiling (opt-in)
    send_default_pii=True,
)
```

### JSON Logging Format

```json
{"time":"2025-09-05 19:52:00","level":"INFO","name":"django.request","msg":"GET /healthz/ 200"}
```

---

## 🔌 API Endpoints

| Method | Endpoint | Purpose | Observability Role |
|---|---|---|---|
| GET | `/healthz/` | Health check — returns `{"status": "ok"}` | UptimeRobot target; signals liveness |
| GET | `/boom/` | Simulated runtime error (`RuntimeError`) | Trigger Sentry alert; validate error capture pipeline |
| GET | `/sentry-debug/` | Simulated crash (`ZeroDivisionError`) | End-to-end Sentry integration test |
| GET | `/admin/` | Django admin panel | Standard admin interface |

---

## 🧪 Incident Drill Framework

The project includes built-in **incident simulation endpoints** to validate the full troubleshooting workflow:

```
Detection → Alerting → Diagnosis → Recovery
```

| Drill | Endpoint | Error Type | Expected Behavior |
|---|---|---|---|
| **Smoke Test** | `GET /healthz/` | — | Returns 200 → UptimeRobot confirms service live |
| **Error Alert** | `GET /boom/` | `RuntimeError` | Sentry captures; alert fires; issue created with stack trace |
| **Crash Test** | `GET /sentry-debug/` | `ZeroDivisionError` | Sentry captures unhandled exception; APM trace records the failing request |
| **Downtime Simulation** | Stop WSGI process | Service unavailable | UptimeRobot detects outage; alert triggers within 5-10 minutes |

These drills support the **Post-Incident RCA (Root Cause Analysis)** documentation workflow, covering the full incident lifecycle from detection through resolution.

---

## 🚀 Deployment

### Platform: PythonAnywhere (WSGI)

| Component | Configuration |
|---|---|
| **Hosting** | PythonAnywhere (WSGI-based Python hosting) |
| **WSGI Server** | Gunicorn (configured via PythonAnywhere WSGI tab) |
| **WSGI Entry** | `my_cloudapp.wsgi:application` |
| **Database** | PostgreSQL via Neon (serverless, connection string in `DATABASE_URL`) |
| **Environment** | `.env` file with `python-dotenv` — secrets never committed |
| **Static Files** | `STATIC_ROOT = BASE_DIR / "staticfiles"` |

### Environment Variables (`.env`)

```bash
DEBUG=False
SECRET_KEY=django-insecure-xxxxx
ALLOWED_HOSTS=yourusername.pythonanywhere.com,localhost
DATABASE_URL=postgresql://user:pass@ep-xxxx.us-east-2.aws.neon.tech/dbname?sslmode=require
SENTRY_DSN=https://xxxxx@oxxxxx.ingest.de.sentry.io/xxxxx
SENTRY_ENV=prod
SENTRY_TRACES_SAMPLE_RATE=0.2
```

### Deployment Checklist

- [ ] Push code to GitHub
- [ ] `git pull` on PythonAnywhere console
- [ ] Set up virtual environment: `mkvirtualenv --python=/usr/bin/python3.10 cloudops`
- [ ] Install dependencies: `pip install django gunicorn psycopg2-binary sentry-sdk python-dotenv dj-database-url`
- [ ] Create `.env` file with production values
- [ ] Run migrations: `python manage.py migrate`
- [ ] Collect static files: `python manage.py collectstatic --noinput`
- [ ] Configure WSGI file on PythonAnywhere to point to `my_cloudapp.wsgi`
- [ ] Reload web app
- [ ] Verify: `curl https://yourusername.pythonanywhere.com/healthz/`
- [ ] Configure UptimeRobot monitor → `https://yourusername.pythonanywhere.com/healthz/`

---

## 🛠 Tech Stack

| Layer | Technology | Version / Provider |
|---|---|---|
| **Language** | Python | 3.10 |
| **Web Framework** | Django | 5.2 |
| **WSGI Server** | Gunicorn | PythonAnywhere-managed |
| **Database** | PostgreSQL (Neon Serverless) | Cloud-hosted |
| **Error Tracking** | Sentry SDK (Django) | sentry.io |
| **APM / Traces** | Sentry Performance | sentry.io |
| **Uptime Monitoring** | UptimeRobot | External polling |
| **Logging** | JSON (console + file) | Django built-in |
| **Config Management** | python-dotenv | `.env` file |
| **Database URL** | dj-database-url | Connection string parser |
| **Hosting** | PythonAnywhere | WSGI platform |
| **Version Control** | Git + GitHub | — |

---

## 💻 Installation & Local Development

### Prerequisites

| Tool | Version |
|---|---|
| Python | 3.10+ |
| pip | latest |
| Git | — |

### Setup

```bash
# Clone the repository
git clone https://github.com/ruiwang2145/Cloud-Operations-Mini-Stack.git
cd Cloud-Operations-Mini-Stack

# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows

# Install dependencies
pip install django gunicorn psycopg2-binary sentry-sdk python-dotenv dj-database-url

# Create .env file (use SQLite for local dev)
echo 'DEBUG=True
SECRET_KEY=dev-secret-key-change-me
ALLOWED_HOSTS=localhost,127.0.0.1
DATABASE_URL=sqlite:///db.sqlite3
SENTRY_DSN=
SENTRY_ENV=dev' > .env

# Run migrations
python manage.py migrate

# Start development server
python manage.py runserver
```

Visit `http://localhost:8000/healthz/` — you should see `{"status": "ok"}`.

---

## 📝 Documentation

The project includes the following operational documentation templates (maintained separately):

| Document | Purpose |
|---|---|
| **System Runbook** | Step-by-step operational procedures: deployment, restart, log access, database backup, environment variable management |
| **Post-Incident RCA Template** | Structured root cause analysis: timeline, impact assessment, root cause identification, corrective actions, prevention measures |
| **Incident Drill Log** | Record of simulated incidents: drill type, detection method, response time, resolution steps, lessons learned |

> These documents are designed to support production-grade operational workflows and can be adapted for any Django-on-PythonAnywhere deployment.

---

## 📄 License

This project is open-source under the MIT License. See [LICENSE](LICENSE) for details.
