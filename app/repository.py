"""Repository: Database operations for users."""

import contextlib
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import get_password_hash
from app.config import settings
from app.exceptions import DatabaseError, EntityInactiveError, EntityNotFoundError
from app.logger import get_logger
from app.models import User, UserRole, utc_now
from app.schemas import UserCreate, UserUpdate

logger = get_logger(__name__)


class UserRepository:
    """Database operations for User entity."""

    def __init__(self, session: AsyncSession):
        self.session = session

    # --- Create ---

    async def create(self, user_data: UserCreate | User) -> User:
        """Create new user."""
        if isinstance(user_data, UserCreate):
            user = User(
                user_name=user_data.user_name,
                hashed_password=await get_password_hash(user_data.password),
                mail=user_data.mail,
                role=user_data.role,
                is_active=True,
            )
        else:
            user = user_data

        try:
            self.session.add(user)
            await self.session.commit()
            await self.session.refresh(user)
            logger.info("User created: %s", user.user_name)
            return user
        except SQLAlchemyError as e:
            logger.error("Error creating user: %s", e)
            await self.session.rollback()
            raise DatabaseError("create", str(e)) from None

    # --- Read ---

    async def get(self, user_name: str) -> User:
        """Get user by username. Raises if not found or inactive."""
        stmt = select(User).where(User.user_name == user_name)
        result = await self.session.execute(stmt)
        user = result.scalars().first()

        if not user:
            raise EntityNotFoundError("User", user_name)
        if not user.is_active:
            raise EntityInactiveError("User", user_name)
        return user

    async def get_by_id(self, user_id: str) -> User:
        """Get user by ID (UUID). Raises if not found or inactive."""
        stmt = select(User).where(User.id == user_id)
        result = await self.session.execute(stmt)
        user = result.scalars().first()

        if not user:
            raise EntityNotFoundError("User", user_id)
        if not user.is_active:
            raise EntityInactiveError("User", user_id)
        return user

    async def get_all(
        self,
        user_role: str | None = None,
        is_active: bool | None = None,
        search: str | None = None,
        skip: int = 0,
        limit: int | None = None,
    ) -> list[User]:
        """Get users with optional filters and pagination."""
        stmt = select(User)
        has_filters = any([user_role, is_active is not None, search, skip > 0, limit])

        if user_role:
            try:
                stmt = stmt.where(User.role == UserRole[user_role.upper()])
            except KeyError:
                logger.warning("Invalid role filter: %s", user_role)

        if is_active is not None:
            stmt = stmt.where(User.is_active == is_active)

        if search:
            pattern = f"%{search}%"
            stmt = stmt.where(
                (User.user_name.ilike(pattern)) | (User.mail.ilike(pattern))
            )

        stmt = stmt.offset(skip)
        if limit:
            stmt = stmt.limit(limit)

        result = await self.session.execute(stmt)
        users = list(result.scalars().all())

        if not users and not has_filters:
            raise EntityNotFoundError("Users", "any")

        return users

    async def count(
        self,
        user_role: str | None = None,
        is_active: bool | None = None,
        search: str | None = None,
    ) -> int:
        """Count users matching filters."""
        stmt = select(func.count()).select_from(User)

        if user_role:
            with contextlib.suppress(KeyError):
                stmt = stmt.where(User.role == UserRole[user_role.upper()])

        if is_active is not None:
            stmt = stmt.where(User.is_active == is_active)

        if search:
            pattern = f"%{search}%"
            stmt = stmt.where(
                (User.user_name.ilike(pattern)) | (User.mail.ilike(pattern))
            )

        result = await self.session.execute(stmt)
        return result.scalar_one()

    # --- Update ---

    async def update(self, user_name: str, update_data: UserUpdate) -> User:
        """Update user by username."""
        user = await self.get(user_name)
        return await self._apply_update(user, update_data)

    async def update_by_id(self, user_id: str, update_data: UserUpdate) -> User:
        """Update user by ID."""
        user = await self.get_by_id(user_id)
        return await self._apply_update(user, update_data)

    async def _apply_update(self, user: User, data: UserUpdate) -> User:
        """Apply update data to user."""
        try:
            if data.user_name is not None:
                user.user_name = data.user_name
            if data.mail is not None:
                user.mail = data.mail
            if data.role is not None:
                user.role = data.role
            if data.is_active is not None:
                user.is_active = data.is_active
            user.updated_at = utc_now()

            await self.session.commit()
            await self.session.refresh(user)
            logger.info("User updated: %s", user.user_name)
            return user
        except SQLAlchemyError as e:
            logger.error("Error updating user: %s", e)
            await self.session.rollback()
            raise DatabaseError("update", str(e)) from None

    async def update_password(self, user_name: str, new_password: str) -> User:
        """Update user password."""
        user = await self.get(user_name)
        try:
            user.hashed_password = await get_password_hash(new_password)
            user.updated_at = utc_now()
            await self.session.commit()
            await self.session.refresh(user)
            logger.info("Password updated: %s", user.user_name)
            return user
        except SQLAlchemyError as e:
            logger.error("Error updating password: %s", e)
            await self.session.rollback()
            raise DatabaseError("update_password", str(e)) from None

    # --- Delete ---

    async def delete(self, user_name: str) -> None:
        """Delete user by username."""
        user = await self.get(user_name)
        await self._delete(user)

    async def delete_by_id(self, user_id: str) -> None:
        """Delete user by ID."""
        user = await self.get_by_id(user_id)
        await self._delete(user)

    async def _delete(self, user: User) -> None:
        """Delete user from database."""
        try:
            await self.session.delete(user)
            await self.session.commit()
            logger.info("User deleted: %s", user.user_name)
        except SQLAlchemyError as e:
            logger.error("Error deleting user: %s", e)
            await self.session.rollback()
            raise DatabaseError("delete", str(e)) from None

    # --- Security helpers ---

    async def increment_failed_attempts(self, user_name: str) -> None:
        """Increment failed login attempts, lock if threshold exceeded."""
        try:
            user = await self.get(user_name)
            user.failed_login_attempts += 1
            user.updated_at = utc_now()

            if user.failed_login_attempts >= settings.max_login_attempts:
                user.locked_until = utc_now() + timedelta(
                    minutes=settings.lockout_duration_minutes
                )
                logger.warning("Account locked: %s", user_name)

            await self.session.commit()
        except SQLAlchemyError as e:
            await self.session.rollback()
            raise DatabaseError("increment_failed_attempts", str(e)) from None

    async def reset_login_counters(self, user_name: str) -> None:
        """Reset security counters after successful login."""
        try:
            user = await self.get(user_name)
            user.failed_login_attempts = 0
            user.locked_until = None
            user.last_login = utc_now()
            user.updated_at = utc_now()

            await self.session.commit()
            logger.info("Login successful: %s", user_name)
        except SQLAlchemyError as e:
            await self.session.rollback()
            raise DatabaseError("reset_login_counters", str(e)) from None

    async def find_by_email(self, email: str) -> User | None:
        """Find user by email address. Returns None if not found."""
        stmt = select(User).where(func.lower(User.mail) == email.lower())
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def mark_email_verified(self, user_id: str) -> None:
        """Mark user's email as verified."""
        try:
            user = await self.get_by_id(user_id)
            user.is_verified = True
            user.updated_at = utc_now()
            await self.session.commit()
            logger.info("Email verified for user: %s", user_id)
        except SQLAlchemyError as e:
            await self.session.rollback()
            raise DatabaseError("mark_email_verified", str(e)) from None
