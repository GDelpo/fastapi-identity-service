"""Service: Business logic and orchestration for users."""

import secrets
import uuid

from app.auth import verify_password
from app.cache import CacheManager, cache_key
from app.config import settings
from app.exceptions import (
    AuthenticationError,
    EntityAlreadyExistsError,
    EntityNotFoundError,
    InvalidTokenError,
)
from app.ldap import LDAPClient
from app.logger import get_logger
from app.models import UserRole, utc_now
from app.repository import UserRepository
from app.schemas import UserCreate, UserRegister, UserResponse, UserUpdate
from app.utils import IdentifierType, detect_identifier_type, normalize_email

logger = get_logger(__name__)


class UserService:
    """User domain service - orchestrates LDAP, database, and cache."""

    def __init__(self, ldap: LDAPClient | None, repo: UserRepository, cache: CacheManager):
        self.ldap = ldap
        self.repo = repo
        self.cache = cache

    # ========================================================================
    # Cache Helpers
    # ========================================================================

    async def _invalidate_user_cache(
        self, user_name: str, user_id: str | None = None
    ) -> None:
        """Invalidate all cache entries for a user.

        Args:
            user_name: Username to invalidate
            user_id: User ID to invalidate (optional, will be looked up if needed)
        """
        await self.cache.delete(cache_key("user", user_name, prefix="ldap"))
        if user_id:
            await self.cache.delete(cache_key("user_id", user_id))

    # ========================================================================
    # User Lookup Methods
    # ========================================================================

    async def find_user(
        self, username: str, use_cache: bool = True
    ) -> UserResponse | None:
        """Find user across LDAP → Database with optional cache. Returns None if not found."""
        key = cache_key("user", username, prefix="ldap")

        if use_cache:
            cached = await self.cache.get(key)
            if cached:
                logger.debug("Cache hit: %s", username)
                return UserResponse.model_validate(cached)

        # Try LDAP
        if self.ldap and self.ldap.is_available:
            try:
                user = await self.ldap.get(username)
                if user:
                    if use_cache:
                        await self.cache.set(key, user.model_dump(), ttl=60)
                    return user
            except Exception as e:
                logger.warning("LDAP query failed: %s", e)

        # Try database
        try:
            db_user = await self.repo.get(username)
            response = UserResponse.model_validate(db_user)
            if use_cache:
                await self.cache.set(key, response.model_dump(), ttl=60)
            return response
        except EntityNotFoundError:
            return None

    async def get_user(self, username: str, use_cache: bool = True) -> UserResponse:
        """Get user by username (must exist)."""
        user = await self.find_user(username, use_cache=use_cache)
        if not user:
            raise EntityNotFoundError("User", username)
        return user

    async def get_user_by_id(
        self, user_id: str, use_cache: bool = True
    ) -> UserResponse:
        """Get user by ID/UUID (must exist).

        Checks local DB first, then falls back to LDAP for employee users
        whose UUIDs are derived as uuid5(NAMESPACE_DNS, sAMAccountName).
        """
        if use_cache:
            cached = await self.cache.get(cache_key("user_id", user_id))
            if cached:
                return UserResponse.model_validate(cached)

        # Try local DB first
        try:
            db_user = await self.repo.get_by_id(user_id)
            response = UserResponse.model_validate(db_user)
            await self.cache.set(
                cache_key("user_id", user_id), response.model_dump(), ttl=60
            )
            return response
        except EntityNotFoundError:
            pass

        # LDAP fallback: employee UUIDs are uuid5(NAMESPACE_DNS, sAMAccountName)
        if self.ldap and self.ldap.is_available:
            try:
                ldap_users = await self.ldap.get_all()
                for u in ldap_users:
                    if str(u.id) == str(user_id):
                        await self.cache.set(
                            cache_key("user_id", user_id), u.model_dump(), ttl=60
                        )
                        return u
            except Exception as e:
                logger.warning("LDAP get_by_id fallback failed: %s", e)

        raise EntityNotFoundError("User", user_id)

    # ========================================================================
    # User Registration (Public)
    # ========================================================================

    async def register_user(
        self,
        user_data: UserRegister,
    ) -> tuple[UserResponse, bool]:
        """Register new user or resend verification for unverified user.

        Only allows registration as AGENT role.
        If user exists but is not verified, resends verification email.

        Args:
            user_data: Registration data

        Returns:
            Tuple of (user_response, is_resend)
            - is_resend=False: New user created
            - is_resend=True: Existing unverified user, verification will be resent

        Raises:
            EntityAlreadyExistsError: If username/email already exists and is verified
        """
        logger.debug("Registering new agent: %s", user_data.user_name)

        # Check if username exists in database
        existing_user = None
        try:
            existing_user = await self.repo.get(user_data.user_name)
        except EntityNotFoundError:
            pass

        # If user exists, check if we can resend verification
        if existing_user:
            if existing_user.is_verified:
                logger.warning(
                    f"Username already exists and verified: {user_data.user_name}"
                )
                raise EntityAlreadyExistsError("User", user_data.user_name)
            else:
                # User exists but not verified - will resend verification
                logger.info(
                    f"Resend verification for unverified user: {user_data.user_name}",
                    extra={"extra_fields": {"user_id": str(existing_user.id)}},
                )
                return (UserResponse.model_validate(existing_user), True)

        # Check if email exists in database
        existing_by_email = await self.repo.find_by_email(user_data.mail)
        if existing_by_email:
            if existing_by_email.is_verified:
                logger.warning("Email already exists and verified: %s", user_data.mail)
                raise EntityAlreadyExistsError("Email", user_data.mail)
            else:
                # Email exists but not verified - will resend verification
                logger.info(
                    f"Resend verification for unverified email: {user_data.mail}",
                    extra={"extra_fields": {"user_id": str(existing_by_email.id)}},
                )
                return (UserResponse.model_validate(existing_by_email), True)

        # Check LDAP for existing username (conflict)
        if self.ldap and self.ldap.is_available:
            try:
                ldap_user = await self.ldap.get(user_data.user_name)
                if ldap_user:
                    logger.warning("Username exists in LDAP: %s", user_data.user_name)
                    raise EntityAlreadyExistsError("User", user_data.user_name)
            except EntityAlreadyExistsError:
                raise  # Re-raise our own exception
            except Exception as e:
                logger.warning("LDAP check failed: %s", e)

        # Create user with AGENT role (forced)
        create_data = UserCreate(
            user_name=user_data.user_name,
            mail=user_data.mail,
            password=user_data.password,
            role=UserRole.AGENT,  # Force AGENT role for self-registration
        )

        db_user = await self.repo.create(create_data)
        logger.info(
            f"Agent registered successfully: {user_data.user_name} (ID: {db_user.id})",
            extra={
                "extra_fields": {"user_id": str(db_user.id), "email": user_data.mail}
            },
        )
        return (UserResponse.model_validate(db_user), False)

    # ========================================================================
    # User CRUD Operations (Admin)
    # ========================================================================

    async def create_user(self, user_data: UserCreate) -> UserResponse:
        """Create new user in database."""
        ldap_uuid: uuid.UUID | None = None

        logger.debug("Creating user: %s", user_data.user_name)

        # Check LDAP for existing user (reuse UUID5 if found)
        if self.ldap and self.ldap.is_available:
            try:
                ldap_user = await self.ldap.get(user_data.user_name)
                if ldap_user:
                    ldap_uuid = ldap_user.id
                    logger.debug("LDAP user found, reusing UUID: %s", ldap_uuid)
            except Exception as e:
                logger.warning("LDAP check failed: %s", e)

        # Check if user exists in database
        try:
            await self.repo.get(user_data.user_name)
            logger.warning("User already exists: %s", user_data.user_name)
            raise EntityAlreadyExistsError("User", user_data.user_name)
        except EntityNotFoundError:
            pass

        # Use LDAP UUID if available
        if ldap_uuid and user_data.id is None:
            user_data = user_data.model_copy(update={"id": ldap_uuid})

        db_user = await self.repo.create(user_data)
        logger.info(
            f"User created successfully: {user_data.user_name} (ID: {db_user.id})",
            extra={"extra_fields": {"user_id": str(db_user.id)}},
        )
        return UserResponse.model_validate(db_user)

    async def update_user(
        self, user_name: str, update_data: UserUpdate
    ) -> UserResponse:
        """Update user by username."""
        updated = await self.repo.update(user_name, update_data)
        await self._invalidate_user_cache(user_name, str(updated.id))
        return UserResponse.model_validate(updated)

    async def update_user_by_id(
        self, user_id: str, update_data: UserUpdate
    ) -> UserResponse:
        """Update user by ID."""
        logger.debug("Updating user: %s", user_id)
        updated = await self.repo.update_by_id(user_id, update_data)
        await self._invalidate_user_cache(updated.user_name, user_id)
        logger.debug("User updated successfully: %s", user_id)
        return UserResponse.model_validate(updated)

    async def delete_user(self, username: str) -> None:
        """Delete user by username."""
        logger.info("Deleting user: %s", username)
        user = await self.repo.get(username)
        user_id = str(user.id)
        await self.repo.delete(username)
        await self._invalidate_user_cache(username, user_id)
        logger.info("User deleted successfully: %s (ID: %s)", username, user_id)

    async def delete_user_by_id(self, user_id: str) -> None:
        """Delete user by ID."""
        logger.info("Deleting user by ID: %s", user_id)
        user = await self.repo.get_by_id(user_id)
        username = user.user_name
        await self.repo.delete_by_id(user_id)
        await self._invalidate_user_cache(username, user_id)
        logger.info("User deleted successfully: %s (ID: %s)", username, user_id)

    # ========================================================================
    # User Listing with Pagination
    # ========================================================================

    async def list_users(
        self,
        role: str | None = None,
        is_active: bool | None = None,
        search: str | None = None,
        skip: int = 0,
        limit: int = 50,
        sort_order: str | None = None,
    ) -> list[UserResponse]:
        """List users with filtering and pagination."""

        # EMPLOYEE role = LDAP only
        if role == UserRole.EMPLOYEE.value:
            if not self.ldap or not self.ldap.is_available:
                return []
            try:
                users = await self.ldap.get_all(search=search, skip=skip, limit=limit)
                return self._sort_users(users, sort_order)
            except Exception as e:
                logger.error("LDAP list failed: %s", e)
                return []

        # Specific role (not EMPLOYEE) = database only
        if role:
            db_users = await self.repo.get_all(
                user_role=role,
                is_active=is_active,
                search=search,
                skip=skip,
                limit=limit,
            )
            return [UserResponse.model_validate(u) for u in db_users]

        # No role = combine LDAP + database
        ldap_users: list[UserResponse] = []
        if self.ldap and self.ldap.is_available:
            try:
                ldap_users = await self.ldap.get_all(
                    search=search, skip=skip, limit=limit
                )
            except Exception as e:
                logger.warning("LDAP list failed: %s", e)

        db_limit = limit - len(ldap_users) if ldap_users else limit
        db_skip = max(0, skip - len(ldap_users))

        db_users: list[UserResponse] = []
        if db_limit > 0:
            results = await self.repo.get_all(
                is_active=is_active, search=search, skip=db_skip, limit=db_limit
            )
            db_users = [UserResponse.model_validate(u) for u in results]

        return self._sort_users(ldap_users + db_users, sort_order)

    async def count_users(
        self,
        role: str | None = None,
        is_active: bool | None = None,
        search: str | None = None,
    ) -> int:
        """Count users matching filters (for pagination metadata)."""

        if role == UserRole.EMPLOYEE.value:
            if not self.ldap or not self.ldap.is_available:
                return 0
            try:
                users = await self.ldap.get_all(search=search, skip=0, limit=10000)
                return len(users)
            except Exception:
                return 0

        if role:
            return await self.repo.count(
                user_role=role, is_active=is_active, search=search
            )

        # Combined count
        total = 0
        if self.ldap and self.ldap.is_available:
            try:
                ldap_users = await self.ldap.get_all(search=search, skip=0, limit=10000)
                total += len(ldap_users)
            except Exception:
                pass

        total += await self.repo.count(is_active=is_active, search=search)
        return total

    def _sort_users(
        self, users: list[UserResponse], sort_order: str | None
    ) -> list[UserResponse]:
        """Sort users by field."""
        if not sort_order or not users:
            return users

        try:
            parts = sort_order.split()
            field = parts[0]
            reverse = len(parts) > 1 and parts[1].upper() == "DESC"

            if not hasattr(users[0], field):
                return users

            return sorted(users, key=lambda u: getattr(u, field), reverse=reverse)
        except Exception:
            return users

    # ========================================================================
    # Authentication
    # ========================================================================

    async def authenticate(self, identifier: str, password: str) -> UserResponse:
        """Authenticate user credentials by email or username.

        Supports login via:
        - Email address (user@example.com) - searches by mail field
        - Username (sAMAccountName for LDAP, user_name for local)

        Flow: LDAP first, then database.

        Args:
            identifier: Email address or username
            password: User password

        Returns:
            UserResponse on successful authentication

        Raises:
            AuthenticationError: Account locked, inactive, or unverified
            EntityNotFoundError: User not found or invalid password
        """
        id_type = detect_identifier_type(identifier)
        logger.debug(
            f"Authenticating user: {identifier} (type: {id_type.value})"
        )

        # Try LDAP first
        if self.ldap and self.ldap.is_available:
            ldap_result = await self._authenticate_ldap(identifier, password, id_type)
            if ldap_result:
                return ldap_result

        # Try database
        db_result = await self._authenticate_database(identifier, password, id_type)
        if db_result:
            return db_result

        logger.error("Authentication failed: %s - user not found", identifier)
        raise EntityNotFoundError("User", identifier)

    async def _authenticate_ldap(
        self, identifier: str, password: str, id_type: IdentifierType
    ) -> UserResponse | None:
        """Authenticate against LDAP by email or username."""
        try:
            # Get user from LDAP based on identifier type
            if id_type == IdentifierType.EMAIL:
                ldap_user = await self.ldap.get_by_email(normalize_email(identifier))
            else:
                ldap_user = await self.ldap.get(identifier)

            if not ldap_user:
                return None

            # Authenticate with LDAP using the username (sAMAccountName)
            if await self.ldap.authenticate(ldap_user.user_name, password):
                logger.info(
                    f"LDAP authentication successful: {ldap_user.user_name} (ID: {ldap_user.id})",
                    extra={
                        "extra_fields": {
                            "auth_method": "ldap",
                            "identifier_type": id_type.value,
                            "user_id": str(ldap_user.id),
                        }
                    },
                )
                return ldap_user
            else:
                logger.warning(
                    f"LDAP authentication failed: {identifier} - invalid password"
                )
                raise EntityNotFoundError("User", identifier)

        except EntityNotFoundError:
            raise
        except Exception as e:
            logger.warning("LDAP authentication error: %s - %s", identifier, str(e))
            return None

    async def _authenticate_database(
        self, identifier: str, password: str, id_type: IdentifierType
    ) -> UserResponse | None:
        """Authenticate against database by email or username."""
        try:
            # Get user from database based on identifier type
            if id_type == IdentifierType.EMAIL:
                db_user = await self.repo.find_by_email(normalize_email(identifier))
                if not db_user:
                    return None
                # Check if user is active (find_by_email doesn't check this)
                if not db_user.is_active:
                    logger.warning("Account inactive: %s", identifier)
                    raise AuthenticationError(
                        "Account is inactive. Contact administrator."
                    )
            else:
                db_user = await self.repo.get(identifier)

            # Check lockout
            if db_user.locked_until and db_user.locked_until > utc_now():
                remaining = int(
                    (db_user.locked_until - utc_now()).total_seconds() / 60
                )
                logger.warning(
                    f"Account locked: {identifier} - {remaining} minutes remaining"
                )
                raise AuthenticationError(
                    f"Account locked. Try again in {remaining} minutes."
                )

            # Verify password
            if not await verify_password(password, db_user.hashed_password):
                await self.repo.increment_failed_attempts(db_user.user_name)
                logger.warning(
                    f"Database authentication failed: {identifier} - invalid password"
                )
                raise EntityNotFoundError("User", identifier)

            # Check email verification for non-service/admin users
            if not db_user.is_verified and db_user.role not in (
                UserRole.ADMIN,
                UserRole.SERVICE,
            ):
                logger.warning("Email not verified: %s", identifier)
                raise AuthenticationError(
                    "Email not verified. Please check your inbox and verify your email."
                )

            await self.repo.reset_login_counters(db_user.user_name)
            logger.info(
                f"Database authentication successful: {db_user.user_name} (ID: {db_user.id})",
                extra={
                    "extra_fields": {
                        "auth_method": "database",
                        "identifier_type": id_type.value,
                        "user_id": str(db_user.id),
                    }
                },
            )
            return UserResponse.model_validate(db_user)

        except (EntityNotFoundError, AuthenticationError):
            raise
        except Exception:
            return None

    # ========================================================================
    # Password Management
    # ========================================================================

    async def change_password(
        self, user_id: str | uuid.UUID, current_password: str, new_password: str
    ) -> None:
        """Change password for authenticated user."""
        user = await self.repo.get_by_id(str(user_id))

        if not await verify_password(current_password, user.hashed_password):
            raise AuthenticationError("Invalid current password")

        await self.repo.update_password(user.user_name, new_password)
        await self._invalidate_user_cache(user.user_name, str(user_id))
        logger.info("Password changed: %s", user_id)

    # ========================================================================
    # Password Reset
    # ========================================================================

    async def request_password_reset(
        self, email: str, resend_cooldown_minutes: int = 2
    ) -> tuple | None:
        """Request password reset for email.

        Returns tuple of (user, token) if user found, None otherwise (for security).
        We don't reveal if email exists.

        Args:
            email: User's email
            resend_cooldown_minutes: Minutes to wait between requests (rate limit)
        """
        # Find user by email
        user = await self.repo.find_by_email(email)
        if not user:
            logger.debug("Password reset requested for unknown email: %s", email)
            return None  # Don't reveal if email exists

        if not user.is_active:
            logger.warning("Password reset for inactive user: %s", email)
            return None

        # Rate limiting: check if we recently sent a reset email
        rate_limit_key = f"password_reset_rate:{user.id}"
        last_request = await self.cache.get(rate_limit_key)
        if last_request:
            logger.info(
                f"Password reset rate limited for user {user.id}",
                extra={"extra_fields": {"user_id": str(user.id), "email": email}},
            )
            return None  # Don't reveal rate limit externally (security)

        # Generate secure token
        token = secrets.token_urlsafe(32)

        # Store in cache with expiration
        cache_key_str = f"password_reset:{token}"
        await self.cache.set(
            cache_key_str,
            {"user_id": str(user.id), "email": email},
            ttl=settings.password_reset_token_expire_minutes * 60,
        )

        # Set rate limit for this user
        await self.cache.set(
            rate_limit_key,
            {"requested_at": str(user.id)},
            ttl=resend_cooldown_minutes * 60,
        )

        logger.info(
            "Password reset token generated",
            extra={"extra_fields": {"user_id": str(user.id), "email": email}},
        )

        return (user, token)

    async def confirm_password_reset(
        self, token: str, new_password: str
    ) -> UserResponse:
        """Confirm password reset with token.

        Args:
            token: Reset token from email
            new_password: New password

        Returns:
            User whose password was reset

        Raises:
            InvalidTokenError: If token is invalid
            TokenExpiredError: If token has expired
        """
        cache_key_str = f"password_reset:{token}"
        data = await self.cache.get(cache_key_str)

        if not data:
            logger.warning("Invalid or expired password reset token")
            raise InvalidTokenError("Password reset token")

        user_id = data.get("user_id")
        email = data.get("email")

        if not user_id:
            raise InvalidTokenError("Password reset token")

        # Update password
        user = await self.repo.get_by_id(user_id)
        await self.repo.update_password(user.user_name, new_password)

        # Invalidate token (single use)
        await self.cache.delete(cache_key_str)

        # Invalidate user cache
        await self._invalidate_user_cache(user.user_name, user_id)

        logger.info(
            "Password reset completed",
            extra={"extra_fields": {"user_id": user_id, "email": email}},
        )

        return UserResponse.model_validate(user)

    # ========================================================================
    # Email Verification
    # ========================================================================

    async def request_email_verification(
        self,
        email: str,
        resend_cooldown_minutes: int = 2,
    ) -> tuple | None:
        """Request email verification.

        Can be used for initial request or resend.
        Implements rate limiting to prevent spam.

        Args:
            email: User's email address
            resend_cooldown_minutes: Minimum minutes between resends

        Returns:
            Tuple of (user, token) if successful, None otherwise.
        """
        # Find user by email
        user = await self.repo.find_by_email(email)
        if not user:
            logger.debug("Email verification requested for unknown email: %s", email)
            return None

        if user.is_verified:
            logger.debug("Email already verified: %s", email)
            return None  # Already verified

        # Check rate limit for resends
        rate_limit_key = f"email_verify_rate:{user.id}"
        last_request = await self.cache.get(rate_limit_key)
        if last_request:
            logger.warning(
                "Email verification rate limited",
                extra={"extra_fields": {"user_id": str(user.id), "email": email}},
            )
            # Return None to not reveal rate limiting externally
            return None

        # Generate secure token
        token = secrets.token_urlsafe(32)

        # Store in cache with expiration
        cache_key_str = f"email_verify:{token}"
        await self.cache.set(
            cache_key_str,
            {"user_id": str(user.id), "email": email},
            ttl=settings.email_verification_token_expire_minutes * 60,
        )

        # Set rate limit
        await self.cache.set(
            rate_limit_key,
            {"requested_at": utc_now().isoformat()},
            ttl=resend_cooldown_minutes * 60,
        )

        logger.info(
            "Email verification token generated",
            extra={"extra_fields": {"user_id": str(user.id), "email": email}},
        )

        return (user, token)

    async def confirm_email_verification(self, token: str) -> UserResponse:
        """Confirm email verification with token.

        Args:
            token: Verification token from email

        Returns:
            User whose email was verified

        Raises:
            InvalidTokenError: If token is invalid
        """
        cache_key_str = f"email_verify:{token}"
        data = await self.cache.get(cache_key_str)

        if not data:
            logger.warning("Invalid or expired email verification token")
            raise InvalidTokenError("Email verification token")

        user_id = data.get("user_id")
        email = data.get("email")

        if not user_id:
            raise InvalidTokenError("Email verification token")

        # Mark email as verified
        await self.repo.mark_email_verified(user_id)

        # Invalidate token (single use)
        await self.cache.delete(cache_key_str)

        # Invalidate user cache
        user = await self.repo.get_by_id(user_id)
        await self._invalidate_user_cache(user.user_name, user_id)

        logger.info(
            "Email verified",
            extra={"extra_fields": {"user_id": user_id, "email": email}},
        )

        return UserResponse.model_validate(user)

    async def get_user_by_email(self, email: str) -> UserResponse | None:
        """Get user by email address."""
        user = await self.repo.find_by_email(email)
        if user:
            return UserResponse.model_validate(user)
        return None
