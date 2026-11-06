# fastapi-identity-service

<p>
  <img alt="Python" src="https://img.shields.io/badge/python-3.14-blue?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.128-009688?logo=fastapi&logoColor=white">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green">
</p>

> FastAPI microservice for authentication and user management: JWT tokens with per-role lifetimes, optional LDAP / Active Directory login, account lockout, email verification and password reset through a separate email service.

Other services delegate their auth to it: they forward the caller's bearer token to `GET /api/v1/me` and get the user and role back.

## Features

- JWT login (`POST /api/v1/login`) with an email address or a username; token lifetime depends on the role.
- Optional LDAP / Active Directory: users are looked up and bound against the directory first, then against the local PostgreSQL users. Empty passwords are rejected before any bind.
- Account lockout after repeated failed logins (`MAX_LOGIN_ATTEMPTS`, `LOCKOUT_DURATION_MINUTES`).
- Agent registration with email verification, and password reset, both started by a service account and delivered through an external email service.
- Redis for the user cache, reset and verification tokens, and the cooldown between reset or verification emails.
- Admin dashboard (Jinja2 + Tailwind + vanilla JS).
- Reverse-proxy agnostic: the same image runs direct, behind Nginx or behind Traefik.
- Docker setup: multi-stage build, gunicorn with Uvicorn workers, non-root user.

## Requirements

- Python 3.14
- PostgreSQL 15+
- Redis 7+
- Optional: Docker + Docker Compose, an LDAP / AD server, and an HTTP email service that accepts `POST` on `EMAIL_SERVICE_URL` (for example [`fastapi-email-service`](https://github.com/GDelpo/fastapi-email-service)).

## Quickstart

### Install

```bash
git clone https://github.com/GDelpo/fastapi-identity-service.git
cd fastapi-identity-service
python -m venv env
source env/bin/activate          # Windows: .\env\Scripts\Activate.ps1
pip install -r requirements-dev.txt
```

### Configure

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"   # paste as SECRET_KEY
# fill in DB_*, REDIS_* and ADMIN_USER / ADMIN_PASSWORD / ADMIN_EMAIL
```

### Run

```bash
alembic upgrade head             # creates the tables and seeds the admin
uvicorn app.main:app --reload --port 8080
```

The `seed_admin_user` migration creates the first admin from `ADMIN_USER`, `ADMIN_PASSWORD` and `ADMIN_EMAIL`. There are no defaults: if any of the three is missing, the migration skips the seed and you have no admin.

With Docker:

```bash
docker compose -f docker/docker-compose.dev.yml up -d                                        # development
docker compose --env-file .env -f docker/docker-compose.prod.standalone.yml up -d --build    # production
docker compose --env-file .env -f docker/docker-compose.prod.traefik.yml up -d --build       # behind Traefik
```

- API: `http://localhost:8080/api/v1/...`
- Health: `http://localhost:8080/health` (200 when PostgreSQL and Redis answer, 503 otherwise)
- Swagger UI: `http://localhost:8080/docs` (ReDoc is turned off)
- Dashboard: `http://localhost:8080/dashboard/`

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `DEBUG` | Development mode: DEBUG logs, no production security warnings | `false` |
| `SECRET_KEY` | JWT signing key: `secrets.token_urlsafe(32)` | required |
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | PostgreSQL connection | `localhost` / `5432` / … |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` / `REDIS_PASSWORD` | Redis connection | `localhost` / `6379` / `0` / — |
| `ADMIN_USER` / `ADMIN_PASSWORD` / `ADMIN_EMAIL` | First admin, read only by the seed migration | none (seed skipped) |
| `CORS_ORIGINS` | JSON list of allowed origins | `["*"]`, which logs a warning outside debug |
| `LOG_FORMAT` / `LOG_FILE` | `json` or `text`; optional log file besides stdout | `json` / — |
| `LDAP_URL` | LDAP server URL; empty disables LDAP | — |
| `LDAP_ADMIN_USERNAME` / `LDAP_ADMIN_PASSWORD` | Service account used to search the directory | — |
| `LDAP_SEARCH_BASES` / `LDAP_OU_TO_AVOID` / `LDAP_ATTR_TO_RETRIEVE` | JSON lists: where to search, OUs to skip, attributes to read | `[]` |
| `EMAIL_SERVICE_URL` | Endpoint of the email service (`MAILSENDER_URL` is still read) | `http://localhost:8081/api/v1/emails` |
| `EMAIL_SERVICE_TIMEOUT` | Seconds to wait for the email service | `30` |
| `MAX_LOGIN_ATTEMPTS` / `LOCKOUT_DURATION_MINUTES` | Lockout policy | `5` / `15` |
| `DATA_DIR` | Host path for Postgres + Redis data in `docker-compose.prod.traefik.yml` | `./data` |

The service has no request rate limiter; brute force is handled by the lockout policy, and a proxy in front can add per-IP limits.

## Endpoints

All under `/api/v1`.

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `POST` | `/login` | Get a JWT (OAuth2 password form) | public |
| `GET` | `/me` | Current user | any valid JWT |
| `POST` | `/register` | Register an agent and send the verification email | SERVICE |
| `POST` | `/register/confirm` | Confirm the email with the token | public (token) |
| `POST` | `/password-reset/request` | Send a reset email | SERVICE |
| `POST` | `/password-reset/confirm` | Set a new password with the token | public (token) |
| `GET` | `` (the prefix itself) | List users, paginated and filtered | ADMIN or SERVICE |
| `POST` | `` | Create a user | ADMIN |
| `GET` / `PATCH` / `DELETE` | `/{user_id}` | Read, update or delete a user | ADMIN |

`GET /health` and `GET /docs` sit at the root, outside the prefix.

Token lifetimes by role (`app/auth.py`):

| Role | TTL | Use case |
|------|-----|----------|
| ADMIN | 1 hour | Administrators |
| EMPLOYEE | 30 min | Internal staff (LDAP) |
| AGENT | 15 min | External users |
| SERVICE | 365 days | Service-to-service calls |

## Tests

```bash
pip install -r requirements-dev.txt
ruff check . && ruff format --check .
pytest
```

- `tests/test_auth_unit.py` needs no infrastructure. It covers empty-password rejection in the LDAP client and the login service, binding as the user and never as the service account, LDAP filter escaping, the email-service settings and the HTTP client the lifespan creates.
- `tests/test_e2e.py` runs against the app with real PostgreSQL and Redis. With the seeded admin (`ADMIN_*` set and `alembic upgrade head` run), it also covers login, `/me` and health. Without that infrastructure, the tests that need it skip.

CI starts PostgreSQL and Redis as services, applies the migrations and runs both files.

## Architecture

```
app/
├── main.py           # app, lifespan (Redis, LDAP, HTTP client), middleware, /health
├── api.py            # REST endpoints
├── service.py        # business logic (LDAP first, then database)
├── repository.py     # data access (PostgreSQL via SQLModel)
├── models.py         # SQLModel tables
├── schemas.py        # Pydantic DTOs (camelCase JSON)
├── config.py         # pydantic-settings
├── auth.py           # JWT tokens, password hashing (bcrypt)
├── cache.py          # Redis client
├── ldap.py           # optional LDAP client
├── middleware.py     # ProxyHeaders + request logging
├── exceptions.py     # ServiceException + handlers
├── email_client.py   # HTTP client for the email service
└── dashboard/        # admin UI (Jinja2 + Tailwind + vanilla JS)
```

## Notes

- Behind a reverse proxy, `ProxyHeadersMiddleware` reads `X-Forwarded-Prefix` and sets `root_path`, so Swagger UI and generated URLs stay correct.
- The production image runs **gunicorn** with `UvicornWorker` (not `uvicorn --workers`), to avoid worker deaths under Python 3.14's `forkserver` start method.
- Logs go to stdout (12-factor); collect them at the infrastructure layer.

## License

[MIT](LICENSE) © 2026 Guido Delponte
