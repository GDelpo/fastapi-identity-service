"""Database models (SQLModel ORM)."""

from datetime import UTC, datetime
from enum import Enum
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


# Helper function for timezone-aware datetime (must be before User class)
def utc_now() -> datetime:
    """Get current UTC time with timezone info."""
    return datetime.now(UTC)


class UserRole(str, Enum):
    """User roles enumeration."""

    EMPLOYEE = "employee"
    ADMIN = "admin"
    AGENT = "agent"
    SERVICE = "service"


class User(SQLModel, table=True):
    """User database model."""

    __tablename__ = "users"

    # Identification
    id: UUID | None = Field(default_factory=uuid4, primary_key=True)
    user_name: str = Field(unique=True, index=True, min_length=3, max_length=50)
    mail: str | None = Field(default=None, unique=True, index=True)  # Optional for SERVICE users
    hashed_password: str = Field(exclude=True)

    # Status
    is_active: bool = Field(default=True)
    is_verified: bool = Field(default=False)
    role: UserRole = Field(default=UserRole.EMPLOYEE)

    # Security
    failed_login_attempts: int = Field(default=0)
    locked_until: datetime | None = Field(
        default=None, sa_column=sa.Column(sa.DateTime(timezone=True), nullable=True)
    )
    last_login: datetime | None = Field(
        default=None, sa_column=sa.Column(sa.DateTime(timezone=True), nullable=True)
    )
    last_password_change: datetime | None = Field(
        default=None, sa_column=sa.Column(sa.DateTime(timezone=True), nullable=True)
    )

    # Password reset
    password_reset_token: str | None = Field(default=None)
    password_reset_expires: datetime | None = Field(
        default=None, sa_column=sa.Column(sa.DateTime(timezone=True), nullable=True)
    )

    # Email verification
    verification_token: str | None = Field(default=None)
    verification_token_expires: datetime | None = Field(
        default=None, sa_column=sa.Column(sa.DateTime(timezone=True), nullable=True)
    )

    # Timestamps
    created_at: datetime = Field(
        default_factory=utc_now,
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime | None = Field(
        default=None, sa_column=sa.Column(sa.DateTime(timezone=True), nullable=True)
    )
