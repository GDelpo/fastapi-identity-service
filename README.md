# Servicio de Identidad

Microservicio de autenticación y gestión de usuarios con FastAPI.

## Repositorio

```bash
git clone https://git.example.com/example-org/identity.git
git pull origin main   # actualizar
```

> Primera vez en una máquina nueva: ver [SETUP.md](https://git.example.com/example-org/workspace/raw/branch/main/SETUP.md) para configurar credenciales Git.

## Tech Stack

- **Python 3.14** + FastAPI 0.128
- **PostgreSQL 16** + SQLModel
- **Redis 7** (cache, rate limiting, tokens)
- **Docker** + Docker Compose
- **Traefik** (reverse proxy en producción)

## Características

- ✅ Autenticación JWT (access + refresh tokens)
- ✅ Registro de usuarios con verificación por email
- ✅ Reset de contraseña por email
- ✅ Integración LDAP (opcional)
- ✅ Rate limiting por endpoint
- ✅ Integración con Mailsender para envío de emails

## Integración con Mailsender

Este servicio se comunica directamente con **Mailsender** para:
- Envío de emails de verificación de cuenta
- Envío de emails de reset de contraseña
- Emails de bienvenida

La comunicación es interna vía red Docker (`traefik-public`).

## Escenarios de Despliegue

### 1. Desarrollo Local (con Docker)

```bash
# Levantar servicios
docker compose -f docker/docker-compose.dev.yml up -d

# Ver logs
docker compose -f docker/docker-compose.dev.yml logs -f

# Parar
docker compose -f docker/docker-compose.dev.yml down
```

**Acceso:** `http://localhost:8080`

- Hot reload habilitado
- Debug mode activo
- Puertos expuestos directamente

---

### 2. Desarrollo Local (sin Docker)

```bash
# Crear entorno virtual
python -m venv env
source env/bin/activate       # Linux/Mac
.\env\Scripts\Activate.ps1    # Windows

# Instalar dependencias
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Configurar
cp .env.example .env
# Editar .env: DB_HOST=localhost, REDIS_HOST=localhost

# Ejecutar migraciones y servidor
alembic upgrade head
uvicorn app.main:app --reload --port 8080
```

**Requisitos:** PostgreSQL y Redis corriendo localmente.

---

### 3. Producción (Standalone - sin proxy)

```bash
docker compose --env-file .env -f docker/docker-compose.prod.standalone.yml up -d --build
```

**Acceso directo:** `http://<server>:8080`

- Sin reverse proxy
- Ideal para testing o redes internas
- Sin rate limiting global (solo el interno por endpoint)

---

### 4. Producción (con Traefik) ✅ Recomendado

```bash
# Prerequisitos
docker network create traefik-public
# Traefik corriendo: docker compose -f docker-compose.traefik.yml up -d

# Levantar servicio
docker compose --env-file .env -f docker/docker-compose.prod.traefik.yml up -d --build

# Ver logs
docker logs -f identity_api
```

**Acceso via proxy:** `http://<server>/identity/api/v1/...`

Características:
- Rate limiting global (100 req/s por IP)
- Path prefix `/identity` manejado automáticamente
- Health checks configurados
- Listo para SSL/TLS

## Logs

Los logs van a **stdout** y Docker los captura automáticamente.

```bash
# Ver logs en tiempo real
docker logs -f identity_api

# Ver últimas 100 líneas
docker logs --tail 100 identity_api

# Buscar errores
docker logs identity_api 2>&1 | grep ERROR
```

**Formato:** JSON en producción (`LOG_FORMAT=json`), texto en desarrollo.

> **Tip:** Para logging centralizado, agregar Loki + Grafana o ELK stack. 
> Leen logs de Docker automáticamente sin cambios en el servicio.

## Tests

```bash
pytest                              # Todos los tests
pytest --cov=app --cov-report=html  # Con coverage
pytest tests/test_e2e.py -v         # Test específico
```

## Estructura

```
app/
├── main.py          # Entry point, lifespan, middlewares
├── api.py           # Endpoints REST
├── service.py       # Lógica de negocio
├── repository.py    # Acceso a datos (PostgreSQL)
├── models.py        # Modelos SQLModel
├── schemas.py       # DTOs Pydantic
├── config.py        # Settings desde env vars
├── auth.py          # JWT tokens, password hashing
├── cache.py         # Redis client
├── ldap.py          # Cliente LDAP (opcional)
├── middleware.py    # Logging, proxy headers
├── exceptions.py    # Errores custom + handlers
└── email_client.py  # Cliente HTTP para Mailsender
```

## Configuración

Ver `.env.example` para todas las variables.

### Variables Principales

| Variable | Descripción | Default |
|----------|-------------|---------|
| `DEBUG` | Modo debug (afecta log_level y environment) | `false` |
| `LOG_FORMAT` | Formato de logs: `json` o `text` | `json` |
| `DB_HOST` | Host PostgreSQL | `localhost` |
| `REDIS_HOST` | Host Redis | `localhost` |
| `SECRET_KEY` | Clave JWT (**cambiar en prod**) | - |
| `LDAP_URL` | URL servidor LDAP (opcional) | - |

### Variables que NO necesitás en producción

| Variable | Razón |
|----------|-------|
| `LOG_FILE` | Logs van a stdout → Docker los captura |
| `DB_PORT`, `REDIS_PORT` | Usar defaults (5432, 6379) |
| `ENVIRONMENT`, `LOG_LEVEL` | Se derivan de `DEBUG` |

## Seguridad

### Passwords

- **Hashing:** bcrypt
- **Requisitos:** mínimo 8 caracteres + mayúscula + minúscula + número + caracter especial

### Tokens JWT

| Rol | Expiración | Caso de uso |
|-----|------------|-------------|
| ADMIN | 1 hora | Usuarios administrativos |
| EMPLOYEE | 30 minutos | Empleados internos (LDAP) |
| AGENT | 15 minutos | Usuarios externos |
| SERVICE | 365 días | Comunicación entre microservicios |

### Rate Limiting

| Nivel | Implementación | Límite |
|-------|---------------|--------|
| Global | Traefik middleware | 100 req/s por IP |
| Password reset | Redis (por usuario) | 1 cada 2 min |
| Email verification | Redis (por usuario) | 1 cada 2 min |

### Bloqueo de cuenta

- **5 intentos fallidos** → cuenta bloqueada 15 minutos
- Configurable via `MAX_LOGIN_ATTEMPTS` y `LOCKOUT_DURATION_MINUTES`

## Endpoints Principales

| Método | Endpoint | Descripción | Auth |
|--------|----------|-------------|------|
| `POST` | `/api/v1/login` | Obtener token | No |
| `GET` | `/api/v1/me` | Usuario actual | JWT |
| `POST` | `/api/v1/register` | Registrar agente | SERVICE |
| `POST` | `/api/v1/register/confirm` | Confirmar email | No |
| `POST` | `/api/v1/password-reset/request` | Solicitar reset | No |
| `POST` | `/api/v1/password-reset/confirm` | Confirmar reset | No |
| `GET` | `/api/v1/users` | Listar usuarios | ADMIN |
| `GET` | `/health` | Health check | No |
| `GET` | `/docs` | Swagger UI | No |

## Integración con Mailsender

El servicio se integra con `mailsender` para emails transaccionales.

### Flujo de Registro

```
Frontend                    Identidad                   Mailsender
   │                           │                           │
   │  POST /register           │                           │
   │  + callback_url ─────────►│                           │
   │                           │  POST /emails ───────────►│
   │                           │  (verification email)     │
   │                           │                           │
   │                           │◄──────────────────────────│
   │◄──────────────────────────│                           │
   │  {user, email_sent_to}    │                           │
```

Usuario recibe email → click en link → `callback_url?token=xxx`

```
Frontend                    Identidad                   Mailsender
   │                           │                           │
   │  POST /register/confirm   │                           │
   │  {token} ────────────────►│                           │
   │                           │  POST /emails ───────────►│
   │                           │  (welcome email)          │
   │                           │                           │
   │◄──────────────────────────│                           │
   │  ✅ Usuario verificado    │                           │
```

### Flujo de Password Reset

Similar: `POST /password-reset/request` → email con link → `POST /password-reset/confirm`

## Decisiones de Diseño

### ¿Por qué no refresh tokens?

Para aplicación interna con tokens de 15-30 min, el re-login es UX aceptable. 
Menos complejidad, menos superficie de ataque.

### ¿Por qué no revocación inmediata de tokens?

JWT stateless + tokens cortos = ventana de riesgo mínima (15 min máx para AGENT).
Implementar revocación requeriría consulta a Redis/DB en cada request.

### ¿Por qué logs a stdout?

Patrón 12-factor apps. Docker captura logs, y herramientas como Loki/ELK 
los recolectan automáticamente sin cambios en el servicio.
