"""Authentication: JWT tokens and password utilities.

The tokenUrl uses the api_prefix from settings, making this module
infrastructure-agnostic. The proxy's root_path is prepended automatically.
"""

from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.security import OAuth2PasswordBearer
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError
from passlib.context import CryptContext

from app.config import settings
from app.exceptions import AuthenticationError
from app.models import UserRole
from app.schemas import Token, TokenData, UserResponse

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# OAuth2 scheme with tokenUrl relative to root_path
# The tokenUrl must include the api_prefix since the login endpoint is mounted there.
# FastAPI prepends root_path (set by ProxyHeadersMiddleware from X-Forwarded-Prefix).
#
# Examples:
# - Direct access: root_path="" → tokenUrl="/api/v1/login"
# - Via Traefik:   root_path="/identity" → tokenUrl="/identity/api/v1/login"
#
# We use f-string without leading slash so it's relative to root_path
oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.api_prefix.lstrip('/')}/login")

# Token expiration by role
TOKEN_EXPIRATION = {
    UserRole.ADMIN: timedelta(hours=1),
    UserRole.AGENT: timedelta(minutes=15),
    UserRole.EMPLOYEE: timedelta(minutes=30),
    UserRole.SERVICE: timedelta(days=365),
}


async def get_password_hash(password: str) -> str:
    """Hash a password using bcrypt.

    Uses threadpool to avoid blocking the event loop since bcrypt is CPU-bound.
    """
    return await run_in_threadpool(pwd_context.hash, password)


async def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash.

    Uses threadpool to avoid blocking the event loop since bcrypt is CPU-bound.
    """
    return await run_in_threadpool(pwd_context.verify, plain_password, hashed_password)


async def create_access_token(user: UserResponse) -> Token:
    """Create JWT access token for a user.

    Args:
        user: UserResponse with user data

    Returns:
        Token with access_token and metadata
    """
    # Get expiration based on role
    expires_delta = TOKEN_EXPIRATION.get(
        user.role, timedelta(minutes=settings.access_token_expire_minutes)
    )
    expire = datetime.now(UTC) + expires_delta

    # Token payload
    payload = {
        "sub": str(user.id),
        "name": user.user_name,
        "mail": user.mail,
        "role": user.role.value,
        "exp": expire,
        "iat": datetime.now(UTC),
    }

    # Encode token
    access_token = jwt.encode(
        payload,
        settings.secret_key.get_secret_value(),
        algorithm=settings.algorithm,
    )

    return Token(
        access_token=access_token,
        token_type="bearer",
        expires_in=int(expires_delta.total_seconds()),
    )


async def decode_token(token: str = Depends(oauth2_scheme)) -> TokenData:
    """Decode and validate JWT token.

    Args:
        token: JWT token from Authorization header

    Returns:
        TokenData with user information

    Raises:
        AuthenticationError: If token is invalid or expired
    """
    try:
        payload = jwt.decode(
            token,
            settings.secret_key.get_secret_value(),
            algorithms=[settings.algorithm],
        )
        user_id = payload.get("sub")
        user_name = payload.get("name")
        mail = payload.get("mail")  # Can be None for SERVICE users
        role = payload.get("role")

        # mail is optional (can be None for SERVICE users)
        if not all([user_id, user_name, role]):
            raise AuthenticationError("Invalid token claims")

        return TokenData(id=user_id, user_name=user_name, mail=mail, role=role)

    except ExpiredSignatureError:
        raise AuthenticationError("Token expired") from None
    except InvalidTokenError:
        raise AuthenticationError("Invalid token") from None


async def get_current_user(token_data: TokenData = Depends(decode_token)) -> TokenData:
    """Get current authenticated user from token.

    This is a convenience dependency that can be used in route handlers.
    """
    return token_data


def require_roles(*roles: UserRole):
    """Dependency factory to require specific roles.

    Usage:
        @router.get("/admin-only")
        async def admin_endpoint(user: TokenData = Depends(require_roles(UserRole.ADMIN))):
            ...
    """

    async def role_checker(current_user: TokenData = Depends(get_current_user)) -> TokenData:
        if current_user.role not in [r.value for r in roles]:
            raise AuthenticationError(f"Required roles: {[r.value for r in roles]}")
        return current_user

    return role_checker
