"""Custom exceptions and handlers."""

from typing import Any

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.logger import get_logger
from app.schemas import ErrorResponse

logger = get_logger(__name__)


# ============================================================================
# Custom Exceptions
# ============================================================================


class ServiceException(Exception):
    """Base service exception."""

    def __init__(
        self,
        message: str,
        status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
        details: dict[str, Any] | None = None,
    ):
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(self.message)


class EntityNotFoundError(ServiceException):
    """Entity not found."""

    def __init__(self, entity: str, identifier: str | int):
        super().__init__(
            message=f"{entity} '{identifier}' not found",
            status_code=status.HTTP_404_NOT_FOUND,
            details={"entity": entity, "identifier": str(identifier)},
        )


class EntityAlreadyExistsError(ServiceException):
    """Entity already exists."""

    def __init__(self, entity: str, identifier: str | int):
        super().__init__(
            message=f"{entity} '{identifier}' already exists",
            status_code=status.HTTP_409_CONFLICT,
            details={"entity": entity, "identifier": str(identifier)},
        )


class EntityInactiveError(ServiceException):
    """Entity is inactive."""

    def __init__(self, entity: str, identifier: str | int):
        super().__init__(
            message=f"{entity} '{identifier}' is inactive",
            status_code=status.HTTP_403_FORBIDDEN,
            details={"entity": entity, "identifier": str(identifier)},
        )


class AuthenticationError(ServiceException):
    """Authentication failed."""

    def __init__(self, message: str = "Authentication failed"):
        super().__init__(
            message=message,
            status_code=status.HTTP_401_UNAUTHORIZED,
            details={"www_authenticate": "Bearer"},
        )


class AuthorizationError(ServiceException):
    """Authorization failed."""

    def __init__(self, message: str = "Insufficient permissions"):
        super().__init__(message=message, status_code=status.HTTP_403_FORBIDDEN)


class DatabaseError(ServiceException):
    """Database operation failed."""

    def __init__(self, operation: str, message: str):
        super().__init__(
            message=f"Database {operation} failed: {message}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            details={"operation": operation},
        )


class ExternalServiceError(ServiceException):
    """External service call failed."""

    def __init__(self, service: str, message: str):
        super().__init__(
            message=f"{service} error: {message}",
            status_code=status.HTTP_502_BAD_GATEWAY,
            details={"service": service},
        )


class ValidationError(ServiceException):
    """Validation failed."""

    def __init__(self, message: str):
        super().__init__(
            message=message,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )


class TokenExpiredError(ServiceException):
    """Token has expired."""

    def __init__(self, token_type: str = "Token"):
        super().__init__(
            message=f"{token_type} has expired",
            status_code=status.HTTP_400_BAD_REQUEST,
            details={"token_type": token_type},
        )


class InvalidTokenError(ServiceException):
    """Token is invalid."""

    def __init__(self, token_type: str = "Token"):
        super().__init__(
            message=f"Invalid {token_type.lower()}",
            status_code=status.HTTP_400_BAD_REQUEST,
            details={"token_type": token_type},
        )


# ============================================================================
# Exception Handlers
# ============================================================================


async def service_exception_handler(_request: Request, exc: ServiceException) -> JSONResponse:
    """Handle service exceptions."""
    logger.warning(
        f"Service exception: {exc.message}",
        extra={
            "extra_fields": {
                "status_code": exc.status_code,
                "message": exc.message,
                "details": exc.details,
            }
        },
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(message=exc.message, details=exc.details).model_dump(),
    )


async def validation_exception_handler(
    _request: Request, exc: RequestValidationError | PydanticValidationError
) -> JSONResponse:
    """Handle validation errors."""
    errors = exc.errors() if hasattr(exc, "errors") else []
    cleaned_errors = [
        {"loc": e.get("loc", []), "msg": e.get("msg", ""), "type": e.get("type", "")}
        for e in errors
    ]
    logger.warning(
        "Validation error",
        extra={
            "extra_fields": {
                "error_count": len(cleaned_errors),
                "errors": cleaned_errors[:3],  # Log only first 3 errors
            }
        },
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=ErrorResponse(
            message="Validation error", details={"errors": cleaned_errors}
        ).model_dump(),
    )


async def sqlalchemy_exception_handler(_request: Request, exc: SQLAlchemyError) -> JSONResponse:
    """Handle database errors."""
    logger.error(
        "Database error",
        extra={
            "extra_fields": {
                "error_type": exc.__class__.__name__,
                "error_message": str(exc),
            }
        },
        exc_info=True,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorResponse(
            message="Database error", details={"type": exc.__class__.__name__}
        ).model_dump(),
    )


async def general_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Handle unexpected errors."""
    logger.error(
        "Unexpected error",
        extra={
            "extra_fields": {
                "error_type": exc.__class__.__name__,
                "error_message": str(exc),
            }
        },
        exc_info=True,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorResponse(
            message="Internal server error", details={"type": exc.__class__.__name__}
        ).model_dump(),
    )
