"""Pydantic schemas (DTOs) for API requests and responses."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from app.models import UserRole

# ============================================================================
# Base Schema with camelCase serialization
# ============================================================================


def to_camel(string: str) -> str:
    """Convert snake_case to camelCase.

    Example: user_name -> userName, created_at -> createdAt
    """
    components = string.split("_")
    return components[0] + "".join(word.capitalize() for word in components[1:])


class CamelModel(BaseModel):
    """Base model that serializes to camelCase for API responses.

    - Internal code uses snake_case (Pythonic)
    - API responses use camelCase (JavaScript convention)
    - API requests accept both snake_case and camelCase
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,  # Accept both snake_case and camelCase in requests
        from_attributes=True,  # Allow creating from ORM models
    )


# ============================================================================
# Helpers
# ============================================================================


def mask_email(email: str) -> str:
    """Mask email address for display: show first 2 chars + *** + domain.

    Example: 'john.doe@example.com' -> 'jo***@example.com'
    """
    if not email or "@" not in email:
        return email

    local, domain = email.split("@", 1)
    if len(local) > 2:
        masked_local = local[:2] + "***"
    elif len(local) > 0:
        masked_local = local[0] + "***"
    else:
        masked_local = "***"

    return f"{masked_local}@{domain}"


def validate_password_strength(password: str) -> str:
    """Validate password meets security requirements.

    Requirements:
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one digit
    - At least one special character

    Raises:
        ValueError: If password doesn't meet requirements
    """
    special_chars = "!@#$%^&*()_+-=[]{}|;:',.<>?/`~\"\\"

    if not any(c.isupper() for c in password):
        raise ValueError("Password must contain at least one uppercase letter")
    if not any(c.islower() for c in password):
        raise ValueError("Password must contain at least one lowercase letter")
    if not any(c.isdigit() for c in password):
        raise ValueError("Password must contain at least one digit")
    if not any(c in special_chars for c in password):
        raise ValueError("Password must contain at least one special character (!@#$%^&*...)")
    return password


# ============================================================================
# Pagination
# ============================================================================


class PaginationMeta(CamelModel):
    """Pagination metadata."""

    total: int
    page: int
    page_size: int
    total_pages: int
    has_next: bool
    has_previous: bool

    @classmethod
    def from_params(cls, total: int, skip: int, limit: int) -> PaginationMeta:  # noqa: F821
        page = (skip // limit) + 1 if limit > 0 else 1
        total_pages = (total + limit - 1) // limit if limit > 0 else 1
        return cls(
            total=total,
            page=page,
            page_size=limit,
            total_pages=total_pages,
            has_next=skip + limit < total,
            has_previous=skip > 0,
        )


class PaginatedResponse[T](CamelModel):
    """Paginated response wrapper."""

    data: list[T]
    meta: PaginationMeta


# ============================================================================
# Health Check
# ============================================================================


class ServiceStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class DependencyStatus(CamelModel):
    """Status of a single dependency."""

    status: ServiceStatus
    latency_ms: float | None = None
    error: str | None = None


class HealthResponse(CamelModel):
    """Health check response."""

    status: ServiceStatus = ServiceStatus.HEALTHY
    version: str
    environment: str
    dependencies: dict[str, DependencyStatus] = {}


# ============================================================================
# Error Response
# ============================================================================


class ErrorResponse(CamelModel):
    """Standard error response."""

    success: bool = False
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# User Schemas
# ============================================================================


class UserRegister(BaseModel):
    """Self-registration request (public endpoint).

    Only agents can self-register. Email is required.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    user_name: str = Field(min_length=1, max_length=50, pattern="^[a-zA-Z0-9_.-]+$")
    mail: EmailStr = Field(..., description="Email is required for registration")
    password: str = Field(min_length=8, max_length=100)
    callback_url: str = Field(
        ...,
        description="URL where user will be redirected to verify email (e.g., https://myapp.com/verify-email)",
    )

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        return validate_password_strength(v)


class UserRegisterResponse(CamelModel):
    """Registration response."""

    message: str
    success: bool = True
    user: UserResponse | None = None
    is_resend: bool = False  # True if verification email was resent for existing unverified user
    email_sent_to: str | None = None  # Masked email when verification was sent


class UserCreate(BaseModel):
    """Create user request."""

    model_config = ConfigDict(str_strip_whitespace=True)

    user_name: str = Field(min_length=1, max_length=50, pattern="^[a-zA-Z0-9_.-]+$")
    mail: EmailStr | None = Field(default=None)
    password: str = Field(min_length=8, max_length=100)
    role: UserRole = UserRole.EMPLOYEE

    @model_validator(mode="after")
    def validate_mail_required(self):
        """Require mail for non-service users."""
        if self.role != UserRole.SERVICE and not self.mail:
            raise ValueError("Mail is required for non-service users")
        return self

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        return validate_password_strength(v)


class UserUpdate(BaseModel):
    """Update user request (partial)."""

    user_name: str | None = Field(None, min_length=1, max_length=50)
    mail: EmailStr | None = None
    role: UserRole | None = None
    is_active: bool | None = None


class UserResponse(CamelModel):
    """User response."""

    id: uuid.UUID
    user_name: str
    mail: str | None = None
    role: UserRole
    is_active: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ============================================================================
# Auth Schemas
# ============================================================================


class Token(BaseModel):
    """JWT token response.

    NOTE: This intentionally does NOT inherit from CamelModel.
    OAuth2 RFC 6749 requires snake_case field names:
    - access_token (not accessToken)
    - token_type (not tokenType)

    Swagger UI and OAuth2 clients expect this exact format.
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int


class TokenData(CamelModel):
    """Data extracted from JWT token."""

    id: uuid.UUID
    user_name: str
    mail: str | None = None
    role: UserRole


# ============================================================================
# Password Reset Schemas
# ============================================================================


class PasswordResetRequest(BaseModel):
    """Request password reset."""

    email: EmailStr
    callback_url: str = Field(
        ...,
        description="URL where user will be redirected to reset password (e.g., https://myapp.com/reset-password)",
    )


class PasswordResetConfirm(BaseModel):
    """Confirm password reset with token."""

    token: str
    new_password: str = Field(min_length=8, max_length=100)


class PasswordResetResponse(CamelModel):
    """Password reset response."""

    message: str
    success: bool = True
    email_sent_to: str | None = None  # Masked email when sent


# ============================================================================
# Email Verification Schemas
# ============================================================================


class EmailVerificationRequest(BaseModel):
    """Request email verification."""

    email: EmailStr


class EmailVerificationConfirm(BaseModel):
    """Confirm email verification with token."""

    token: str


class EmailVerificationResponse(CamelModel):
    """Email verification response."""

    message: str
    success: bool = True
    verified: bool = False
