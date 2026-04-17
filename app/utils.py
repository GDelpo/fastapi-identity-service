"""Utility functions and helpers for the identity service."""

from enum import Enum


class IdentifierType(str, Enum):
    """Type of user identifier for authentication."""

    EMAIL = "email"
    USERNAME = "username"


def detect_identifier_type(identifier: str) -> IdentifierType:
    """Detect if identifier is an email or username.

    Simple heuristic: if it contains @ it's treated as email.
    Full email validation is handled by Pydantic's EmailStr in schemas.

    Args:
        identifier: User identifier (email or username)

    Returns:
        IdentifierType.EMAIL or IdentifierType.USERNAME
    """
    return IdentifierType.EMAIL if "@" in identifier else IdentifierType.USERNAME


def normalize_email(email: str) -> str:
    """Normalize email address for comparison.

    Args:
        email: Email address to normalize

    Returns:
        Lowercase, stripped email
    """
    return email.strip().lower()
