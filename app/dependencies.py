"""FastAPI dependencies for the application."""

from typing import Annotated

from fastapi import Depends, Query, Request
from pydantic import BaseModel, Field
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import decode_token
from app.cache import CacheManager
from app.config import settings
from app.database import get_session
from app.email_client import EmailClient
from app.exceptions import AuthorizationError
from app.ldap import LDAPClient
from app.models import UserRole
from app.repository import UserRepository
from app.schemas import TokenData
from app.service import UserService

# ============================================================================
# Authentication Dependencies
# ============================================================================


async def get_current_user(token_data: TokenData = Depends(decode_token)) -> TokenData:
    """Get authenticated user from JWT token."""
    return token_data


async def admin_role_required(
    current_user: TokenData = Depends(get_current_user),
) -> TokenData:
    """Require admin privileges."""
    if current_user.role != UserRole.ADMIN:
        raise AuthorizationError("Admin privileges required")
    return current_user


async def service_role_required(
    current_user: TokenData = Depends(get_current_user),
) -> TokenData:
    """Require service account privileges."""
    if current_user.role != UserRole.SERVICE:
        raise AuthorizationError("Service account required")
    return current_user


async def admin_or_service_required(
    current_user: TokenData = Depends(get_current_user),
) -> TokenData:
    """Require admin or service account privileges."""
    if current_user.role not in (UserRole.ADMIN, UserRole.SERVICE):
        raise AuthorizationError("Admin or service account required")
    return current_user


# ============================================================================
# Service Dependencies
# ============================================================================


# Singleton LDAP client (from app.state — created once in lifespan)


def get_ldap_client(request: Request) -> LDAPClient | None:
    """Get LDAP client from app state. Returns None if LDAP is not configured."""
    return getattr(request.app.state, "ldap_client", None)


def get_cache_manager(request: Request) -> CacheManager:
    """Get CacheManager from app state.

    Returns the initialized CacheManager from app.state.
    If not available (shouldn't happen in normal flow), raises an error.
    """
    cache = getattr(request.app.state, "cache_manager", None)
    if cache is None:
        # This should not happen if lifespan initialized correctly
        # Create a fallback but log a warning
        from app.logger import get_logger

        logger = get_logger(__name__)
        logger.warning("CacheManager not found in app.state, creating new instance")
        from app.cache import CacheManager as CacheMgr

        cache = CacheMgr(redis_url=settings.redis_url)
        # Note: this instance won't be connected, operations will gracefully fail
    return cache


async def get_repository(
    session: AsyncSession = Depends(get_session),
) -> UserRepository:
    """Get UserRepository with database session."""
    return UserRepository(session=session)


async def get_user_service(
    ldap: LDAPClient | None = Depends(get_ldap_client),
    repo: UserRepository = Depends(get_repository),
    cache: CacheManager = Depends(get_cache_manager),
) -> UserService:
    """Get UserService with all dependencies."""
    return UserService(ldap=ldap, repo=repo, cache=cache)


def get_email_client(
    request: Request,
) -> EmailClient:
    """Get EmailClient for sending emails via the email service.

    Propagates the caller's auth token to maintain traceability.
    The email service records the original caller as the source.
    """
    # Extract token from Authorization header
    auth_header = request.headers.get("Authorization", "")
    auth_token = None
    if auth_header.startswith("Bearer "):
        auth_token = auth_header[7:]  # Remove "Bearer " prefix

    return EmailClient(
        url=settings.email_service_url,
        client=request.app.state.http_client,
        auth_token=auth_token,
    )


# ============================================================================
# Pagination Parameters
# ============================================================================


class PaginationParams(BaseModel):
    """Pagination parameters."""

    skip: int = Field(default=0, ge=0, description="Offset")
    limit: int = Field(default=20, ge=1, le=100, description="Page size")

    @property
    def page(self) -> int:
        """Calculate page number (1-indexed)."""
        return (self.skip // self.limit) + 1 if self.limit > 0 else 1


def get_pagination_params(
    skip: Annotated[int, Query(ge=0, description="Offset", examples=[0])] = 0,
    limit: Annotated[int, Query(ge=1, le=100, description="Limit", examples=[20])] = 20,
) -> PaginationParams:
    """Dependency for pagination parameters."""
    return PaginationParams(skip=skip, limit=limit)


# ============================================================================
# User Filter Parameters
# ============================================================================


class UserFilterParams(BaseModel):
    """User-specific filter parameters."""

    role: str | None = None
    is_active: bool | None = None
    search: str | None = None
    sort_by: str | None = "user_name:asc"

    def get_sort_order(self) -> str | None:
        """Parse sort_by into SQL-friendly format."""
        if not self.sort_by:
            return None
        field, _, order = self.sort_by.partition(":")
        order = order or "asc"
        return f"{field} {order.upper()}"


def get_user_filters(
    role: Annotated[str | None, Query(description="Filter by role")] = None,
    is_active: Annotated[bool | None, Query(description="Filter by active status")] = None,
    search: Annotated[
        str | None,
        Query(min_length=3, max_length=100, description="Search in username or email"),
    ] = None,
    sort_by: Annotated[
        str | None,
        Query(
            description="field:order (e.g., user_name:asc)",
            pattern=r"^(user_name|mail|role|created_at|updated_at)(:asc|:desc)?$",
        ),
    ] = "user_name:asc",
) -> UserFilterParams:
    """Dependency for user filter parameters."""
    return UserFilterParams(role=role, is_active=is_active, search=search, sort_by=sort_by)
