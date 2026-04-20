# fastapi-identity-service

<p>
  <img alt="Language" src="https://img.shields.io/badge/python-3.12%2B-blue?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.128-009688?logo=fastapi&logoColor=white">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green">
  <img alt="Status" src="https://img.shields.io/badge/status-stable-green">
</p>

> FastAPI microservice for authentication and user management, with optional LDAP integration, JWT tokens, role-based access and transactional email flows.

## Features

- JWT authentication with role-based expirations (admin, employee, agent, service)
- User registration with email verification and password reset flows
- Optional LDAP integration (bind + search) for employee auth
- Redis-backed rate limiting, token storage and user cache
- Admin dashboard (Jinja2 + Tailwind + vanilla JS, self-hosted)
- Reverse-proxy agnostic: runs the same behind Traefik, Nginx or direct
- Production-ready Docker setup: multi-stage build, gunicorn, non-root user

## Quickstart

### Requirements

- Python 3.12+ (3.14 recommended)
- PostgreSQL 15+
- Redis 7+
- (Optional) Docker + Docker Compose for the full stack
- (Optional) An LDAP server if you want LDAP auth
- (Optional) A mailsender-compatible HTTP endpoint for transactional emails

### Install

```bash
git clone https://github.com/GDelpo/fastapi-identity-service.git
cd fastapi-identity-service
python -m venv env
source env/bin/activate          # Linux/Mac
# .\env\Scripts\Activate.ps1     # Windows
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

### Configure

```bash
cp .env.example .env
# Generate a secure JWT key:
python -c "import secrets; print(secrets.token_urlsafe(32))"
# Paste the output as SECRET_KEY and fill in DB_*/REDIS_* credentials
```

See [Configuration](#configuration) for the full variable list.

### Run

```bash
# Apply migrations
alembic upgrade head

# Start the API (dev)
uvicorn app.main:app --reload --port 8080
```

Or with Docker:

```bash
# Development (hot reload)
docker compose -f docker/docker-compose.dev.yml up -d

# Production (standalone)
docker compose --env-file .env -f docker/docker-compose.prod.standalone.yml up -d --build

# Production (behind Traefik)
docker compose --env-file .env -f docker/docker-compose.prod.traefik.yml up -d --build
```

Access:

- API: `http://localhost:8080/api/v1/...`
- Health: `http://localhost:8080/health`
- Swagger UI: `http://localhost:8080/docs`
- Dashboard: `http://localhost:8080/dashboard/`

## Configuration

Variables are defined in `.env` (copy from `.env.example`):

| Variable | Description | Default |
|----------|-------------|---------|
| `DEBUG` | `true` enables dev mode (DEBUG logs, hot reload, security warnings off) | `false` |
| `SECRET_KEY` | JWT signing key. Generate with `secrets.token_urlsafe(32)` | — (required) |
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | PostgreSQL connection | `localhost`/`5432`/... |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` / `REDIS_PASSWORD` | Redis connection | `localhost`/`6379`/`0`/— |
| `ADMIN_USER` / `ADMIN_PASSWORD` / `ADMIN_EMAIL` | Seeded on first migration | `admin` / `admin` / `admin@example.com` |
| `CORS_ORIGINS` | JSON list of allowed origins | `["*"]` (change in prod) |
| `LOG_FORMAT` | `json` or `text` | `json` |
| `LDAP_URL` | LDAP server URL. Leave empty to disable LDAP | — |
| `LDAP_ADMIN_USERNAME` / `LDAP_ADMIN_PASSWORD` | Bind credentials | — |
| `LDAP_SEARCH_BASES` | JSON list of DN search bases | `[]` |
| `MAILSENDER_URL` | HTTP endpoint for transactional emails | `http://localhost:8081/api/v1/emails` |
| `MAX_LOGIN_ATTEMPTS` / `LOCKOUT_DURATION_MINUTES` | Account lockout policy | `5` / `15` |
| `RATE_LIMIT_*` | Internal rate-limit knobs | `100` req / `60` s |
| `DATA_DIR` | Host path used by `docker-compose.prod.traefik.yml` for Postgres + Redis data | `./data` |

## Endpoints

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `POST` | `/api/v1/login` | Obtain JWT | public |
| `GET`  | `/api/v1/me` | Current user | JWT |
| `POST` | `/api/v1/register` | Register agent | SERVICE token |
| `POST` | `/api/v1/register/confirm` | Confirm email | public (token) |
| `POST` | `/api/v1/password-reset/request` | Request reset | public |
| `POST` | `/api/v1/password-reset/confirm` | Confirm reset | public (token) |
| `GET`  | `/api/v1/users` | List users | ADMIN |
| `GET`  | `/health` | Health check | public |
| `GET`  | `/docs` | Swagger UI | public |

## Architecture

```
app/
├── main.py           # App factory, lifespan, middleware, /health
├── api.py            # REST endpoints
├── service.py        # Business logic
├── repository.py     # Data access (PostgreSQL via SQLModel)
├── models.py         # SQLModel tables
├── schemas.py        # Pydantic DTOs (camelCase JSON)
├── config.py         # pydantic-settings
├── auth.py           # JWT tokens, password hashing (bcrypt)
├── cache.py          # Redis client
├── ldap.py           # Optional LDAP client
├── middleware.py     # ProxyHeaders + Request logging
├── exceptions.py     # ServiceException + handlers
├── email_client.py   # HTTP client for mailsender
└── dashboard/        # Admin UI (Jinja2 + Tailwind + vanilla JS)
```

Token expirations by role:

| Role | TTL | Use case |
|------|-----|----------|
| ADMIN | 1 hour | Administrative users |
| EMPLOYEE | 30 min | Internal staff (LDAP) |
| AGENT | 15 min | External users |
| SERVICE | 365 days | Service-to-service calls |

## Tests

```bash
pytest                                    # all tests
pytest -m unit                            # by marker (unit/integration/e2e/slow/ldap)
pytest --cov=app --cov-report=html        # with coverage
```

## Notes

- The app is designed to sit behind a reverse proxy that forwards `X-Forwarded-*` headers. `ProxyHeadersMiddleware` reads `X-Forwarded-Prefix` and sets `root_path` so Swagger UI and generated URLs stay correct.
- Production Docker image uses **gunicorn** with `UvicornWorker` (not `uvicorn --workers`) to avoid Python 3.14 `forkserver` worker-death issues.
- Logs go to **stdout** (12-factor). Plug into Loki/ELK at the infra layer, not the app.

## License

[MIT](LICENSE) (c) 2026 Guido Delponte
