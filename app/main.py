"""Identity Service - Main application entry point.

This application is infrastructure-agnostic. It runs the same way:
- Locally on port 8080
- Behind Traefik with any path prefix
- Behind Nginx or any other reverse proxy

The proxy is responsible for:
- Path prefix stripping (via StripPrefix middleware)
- Setting X-Forwarded-* headers
- TLS termination

FastAPI automatically handles X-Forwarded-* headers for proper URL generation.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import select

from app.api import router as users_router
from app.cache import CacheManager
from app.config import settings
from app.dashboard.routes import router as dashboard_router
from app.database import async_session_maker, engine
from app.exceptions import (
    ServiceException,
    general_exception_handler,
    service_exception_handler,
    sqlalchemy_exception_handler,
    validation_exception_handler,
)
from app.ldap import LDAPClient
from app.logger import configure_logging, get_logger
from app.middleware import ProxyHeadersMiddleware, RequestLoggingMiddleware
from app.schemas import DependencyStatus, HealthResponse, ServiceStatus

# Configure logging before anything else
configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
    """Manage application lifespan: startup and shutdown events."""
    logger.info(
        "Starting Identity Service",
        extra={
            "version": settings.project_version,
            "environment": settings.environment,
            "debug": settings.debug,
        },
    )

    # Initialize cache
    cache_manager = CacheManager(redis_url=settings.redis_url)
    try:
        await cache_manager.connect()
        app_instance.state.cache_manager = cache_manager
        logger.info("Cache manager initialized")
    except Exception as e:
        logger.warning("Cache initialization failed: %s", e)
        app_instance.state.cache_manager = cache_manager  # Store even if not connected

    # Initialize LDAP if configured
    app_instance.state.ldap_client = None
    if settings.ldap_enabled and settings.ldap_url:
        ldap_client = LDAPClient(
            ldap_url=settings.ldap_url,
            admin_username=settings.ldap_admin_username.get_secret_value()
            if settings.ldap_admin_username
            else "",
            admin_password=settings.ldap_admin_password.get_secret_value()
            if settings.ldap_admin_password
            else "",
            search_bases=settings.ldap_search_bases or [],
            ou_to_avoid=settings.ldap_ou_to_avoid,
            attributes=settings.ldap_attr_to_retrieve,
        )
        app_instance.state.ldap_client = ldap_client
        logger.info(
            "Testing LDAP connection",
            extra={
                "extra_fields": {
                    "ldap_url": settings.ldap_url,
                    "search_bases": settings.ldap_search_bases,
                }
            },
        )
        try:
            is_connected = await ldap_client.test_connection()
            if is_connected:
                logger.info("✅ LDAP connection successful")
            else:
                _, error = ldap_client.get_status()
                logger.warning(
                    "⚠️ LDAP connection failed: %s",
                    error,
                    extra={"extra_fields": {"ldap_url": settings.ldap_url}},
                )
        except Exception as e:
            logger.warning(
                "⚠️ LDAP initialization error: %s",
                e,
                extra={
                    "extra_fields": {
                        "ldap_url": settings.ldap_url,
                        "error_type": type(e).__name__,
                    }
                },
            )
    else:
        logger.info("LDAP disabled, skipping initialization")

    # Shared HTTP client for the email service (used by dependencies.get_email_client)
    app_instance.state.http_client = httpx.AsyncClient(timeout=settings.email_service_timeout)

    yield

    # Cleanup
    logger.info("Shutting down Identity Service")
    await app_instance.state.http_client.aclose()
    if hasattr(app_instance.state, "cache_manager"):
        await app_instance.state.cache_manager.disconnect()
    # Close pooled DB connections; they belong to this event loop.
    await engine.dispose()


# =============================================================================
# Application Factory
# =============================================================================

app = FastAPI(
    title=settings.project_name,
    version=settings.project_version,
    description=settings.project_description,
    # No root_path hardcoded - ProxyHeadersMiddleware reads X-Forwarded-Prefix
    docs_url="/docs",
    redoc_url=None,  # Disabled - we only use Swagger UI
    openapi_url="/openapi.json",
    debug=settings.debug,
    lifespan=lifespan,
)

# =============================================================================
# Middleware (order matters - first added = last executed, outermost first)
# =============================================================================

# Proxy headers middleware (outermost - handles X-Forwarded-Prefix for root_path)
# Must be first so it runs before all other middleware
app.add_middleware(ProxyHeadersMiddleware)

# Logging middleware
app.add_middleware(RequestLoggingMiddleware)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# =============================================================================
# Exception Handlers
# =============================================================================

app.add_exception_handler(ServiceException, service_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(PydanticValidationError, validation_exception_handler)
app.add_exception_handler(SQLAlchemyError, sqlalchemy_exception_handler)
app.add_exception_handler(Exception, general_exception_handler)

# =============================================================================
# Routers
# =============================================================================

# Mount API router with internal prefix
app.include_router(users_router, prefix=settings.api_prefix)

# Mount Dashboard (admin UI)
app.include_router(dashboard_router, prefix="/dashboard")


# =============================================================================
# Health Check (at root level, not under api_prefix)
# =============================================================================


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["System"],
    summary="Health Check",
    responses={
        200: {"description": "Service is healthy or degraded"},
        503: {"description": "Service is unhealthy - critical dependencies failed"},
    },
)
async def health_check(request: Request) -> HealthResponse:
    """Check health of the application and its dependencies.

    This endpoint is at the root level for easy access by:
    - Load balancers
    - Kubernetes probes
    - Monitoring systems

    Returns:
        - 200 OK: Service is healthy or degraded (can still serve requests)
        - 503 Service Unavailable: Critical dependencies failed (PostgreSQL or Redis)

    LDAP is optional - if unavailable, service is degraded but not unhealthy.
    """
    from fastapi.responses import JSONResponse

    dependencies: dict[str, DependencyStatus] = {}

    # ==========================================================================
    # Critical dependencies (if any fails -> UNHEALTHY -> 503)
    # ==========================================================================

    # Check PostgreSQL (CRITICAL)
    try:
        async with async_session_maker() as session:
            await session.execute(select(1))
        dependencies["postgresql"] = DependencyStatus(
            status=ServiceStatus.HEALTHY,
            latency_ms=0,
        )
    except Exception as e:
        dependencies["postgresql"] = DependencyStatus(
            status=ServiceStatus.UNHEALTHY,
            error=str(e),
        )

    # Check Redis (CRITICAL - needed for tokens, cache, rate limiting)
    try:
        cache_manager: CacheManager | None = getattr(request.app.state, "cache_manager", None)
        if cache_manager and await cache_manager.ping():
            dependencies["redis"] = DependencyStatus(
                status=ServiceStatus.HEALTHY,
                latency_ms=0,
            )
        else:
            # Redis not connected is UNHEALTHY because it's needed for:
            # - Password reset tokens
            # - Email verification tokens
            # - User cache
            # - Rate limiting
            dependencies["redis"] = DependencyStatus(
                status=ServiceStatus.UNHEALTHY,
                error="Redis not connected",
            )
    except Exception as e:
        dependencies["redis"] = DependencyStatus(
            status=ServiceStatus.UNHEALTHY,
            error=str(e),
        )

    # ==========================================================================
    # Optional dependencies (if unavailable -> DEGRADED, service still works)
    # ==========================================================================

    # Check LDAP if configured (OPTIONAL - service works without it)
    if settings.ldap_enabled:
        try:
            ldap_client = getattr(request.app.state, "ldap_client", None)
            if ldap_client:
                is_available, error = ldap_client.get_status()
                if is_available:
                    dependencies["ldap"] = DependencyStatus(
                        status=ServiceStatus.HEALTHY,
                        latency_ms=0,
                    )
                else:
                    # LDAP unavailable = DEGRADED (can still auth against DB)
                    dependencies["ldap"] = DependencyStatus(
                        status=ServiceStatus.DEGRADED,
                        error=error or "LDAP not connected",
                    )
            else:
                dependencies["ldap"] = DependencyStatus(
                    status=ServiceStatus.DEGRADED,
                    error="LDAP client not initialized",
                )
        except Exception as e:
            # LDAP errors are DEGRADED, not UNHEALTHY
            dependencies["ldap"] = DependencyStatus(
                status=ServiceStatus.DEGRADED,
                error=str(e),
            )

    # ==========================================================================
    # Determine overall status
    # ==========================================================================

    # Critical dependencies that must be healthy
    critical_deps = ["postgresql", "redis"]
    critical_statuses = [dependencies[dep].status for dep in critical_deps if dep in dependencies]

    # If any critical dependency is unhealthy -> service is unhealthy
    if ServiceStatus.UNHEALTHY in critical_statuses:
        overall_status = ServiceStatus.UNHEALTHY
    # If all critical are healthy but optional are degraded -> degraded
    elif ServiceStatus.DEGRADED in [dep.status for dep in dependencies.values()]:
        overall_status = ServiceStatus.DEGRADED
    # All healthy
    else:
        overall_status = ServiceStatus.HEALTHY

    response_data = HealthResponse(
        status=overall_status,
        version=settings.project_version,
        environment=settings.environment,
        dependencies=dependencies,
    )

    # Return 503 if unhealthy (for Docker/K8s health checks)
    if overall_status == ServiceStatus.UNHEALTHY:
        return JSONResponse(
            status_code=503,
            content=response_data.model_dump(by_alias=True),
        )

    return response_data


@app.get("/", tags=["System"], summary="Root")
async def root():
    """Root endpoint - basic service info."""
    return {
        "service": settings.project_name,
        "version": settings.project_version,
        "docs": "/docs",
        "health": "/health",
    }


# =============================================================================
# Development Server
# =============================================================================

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8080,
        reload=settings.debug,
    )
