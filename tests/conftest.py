"""
Pytest Configuration and Shared Fixtures

Provides reusable fixtures for testing the Identity Service.
Includes database fixtures, cache, and test data factories.

REQUIREMENTS:
    Start with: docker compose -f docker/docker-compose.dev.yml up -d
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from app.auth import get_password_hash
from app.main import app
from app.models import User, UserRole
from app.repository import UserRepository
from app.schemas import UserCreate, UserResponse
from app.service import UserService

# ============================================================================
# Pytest Configuration
# ============================================================================

# Use in-memory SQLite for tests (fast, isolated)
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


# ============================================================================
# Test Client Fixture
# ============================================================================


@pytest.fixture
def client():
    """Create a TestClient for FastAPI app with lifespan support."""
    # TestClient automatically handles lifespan events
    return TestClient(app)


# ============================================================================
# Database Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def async_engine():
    """Create async engine for testing."""
    engine = create_async_engine(
        TEST_DATABASE_URL,
        echo=False,
        connect_args={"check_same_thread": False},
    )

    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    yield engine

    # Drop all tables after tests
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)

    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(async_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create a fresh database session for each test."""
    async_session_maker = sessionmaker(
        async_engine, class_=AsyncSession, expire_on_commit=False
    )

    async with async_session_maker() as session:
        yield session
        await session.rollback()


# ============================================================================
# Cache Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def cache_manager():
    """Create cache manager with mock Redis (or in-memory dict)."""

    # For unit tests, use a simple dict-based cache mock
    class MockCacheManager:
        def __init__(self):
            self._cache = {}

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def get(self, key: str):
            return self._cache.get(key)

        async def set(self, key: str, value, _ttl: int = None):
            self._cache[key] = value

        async def delete(self, key: str):
            self._cache.pop(key, None)

        async def ping(self) -> bool:
            return True

    cache = MockCacheManager()
    await cache.connect()
    yield cache
    await cache.disconnect()


# ============================================================================
# LDAP Fixtures
# ============================================================================


@pytest.fixture
def mock_ldap_service():
    """Create mock LDAP service for testing."""

    class MockLdapService:
        def __init__(self):
            self.is_available = False
            self._last_error = None
            self._mock_users = {}

        def add_mock_user(
            self, username: str, email: str, role: UserRole = UserRole.EMPLOYEE
        ):
            """Add a mock user to LDAP responses."""
            self._mock_users[username] = UserResponse(
                id=uuid.uuid5(uuid.NAMESPACE_DNS, f"ldap-{username}"),
                user_name=username,
                mail=email,
                role=role,
                is_active=True,
                created_at=datetime.now(UTC),
            )

        async def test_connection(self) -> bool:
            return self.is_available

        async def get(self, username: str) -> UserResponse | None:
            return self._mock_users.get(username)

        async def get_all(
            self, search: str | None = None, skip: int = 0, limit: int = 50
        ):
            users = list(self._mock_users.values())
            if search:
                users = [u for u in users if search.lower() in u.user_name.lower()]
            return users[skip : skip + limit]

        async def authenticate_user(self, username: str, password: str) -> bool:
            # Simple mock: any user with password "ldappass123"
            return username in self._mock_users and password == "ldappass123"

        def get_status(self):
            return (self.is_available, self._last_error)

    return MockLdapService()


# ============================================================================
# Service Layer Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def user_repo(db_session):
    """Create UserRepository instance."""
    return UserRepository(db_session)


# Alias for backward compatibility with tests
@pytest_asyncio.fixture
async def user_crud(user_repo):
    """Alias for user_repo (backward compatibility)."""
    return user_repo


@pytest_asyncio.fixture
async def user_service(mock_ldap_service, user_repo, cache_manager):
    """Create UserService with mocked dependencies."""
    return UserService(
        ldap=mock_ldap_service,
        repo=user_repo,
        cache=cache_manager,
    )


# ============================================================================
# Test Data Factories
# ============================================================================


@pytest.fixture
def sample_user_data():
    """Factory for creating sample user data."""

    def _create_user_data(
        username: str = "testuser",
        email: str = "test@example.com",
        password: str = "TestPass123!",
        role: UserRole = UserRole.EMPLOYEE,
    ) -> UserCreate:
        return UserCreate(
            user_name=username,
            mail=email,
            password=password,
            role=role,
        )

    return _create_user_data


@pytest_asyncio.fixture
async def sample_db_user(db_session) -> User:
    """Create a sample user in the database."""
    user = User(
        id=uuid.uuid4(),
        user_name="dbuser",
        mail="dbuser@example.com",
        hashed_password=get_password_hash("DbPass123!"),
        role=UserRole.EMPLOYEE,
        is_active=True,
        is_verified=True,
        failed_login_attempts=0,
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def sample_admin_user(db_session) -> User:
    """Create a sample admin user in the database."""
    user = User(
        id=uuid.uuid4(),
        user_name="adminuser",
        mail="admin@example.com",
        hashed_password=get_password_hash("AdminPass123!"),
        role=UserRole.ADMIN,
        is_active=True,
        is_verified=True,
        failed_login_attempts=0,
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def locked_user(db_session) -> User:
    """Create a locked user (for testing account lockout)."""
    user = User(
        id=uuid.uuid4(),
        user_name="lockeduser",
        mail="locked@example.com",
        hashed_password=get_password_hash("LockedPass123!"),
        role=UserRole.EMPLOYEE,
        is_active=True,
        is_verified=True,
        failed_login_attempts=5,
        locked_until=datetime.now(UTC) + timedelta(minutes=15),
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


# ============================================================================
# Utility Fixtures
# ============================================================================


@pytest.fixture
def valid_jwt_payload():
    """Factory for creating valid JWT payloads."""

    def _create_payload(
        user_id: str = str(uuid.uuid4()),
        username: str = "testuser",
        email: str = "test@example.com",
        role: str = UserRole.EMPLOYEE.value,
    ) -> dict:
        now = datetime.now(UTC)
        return {
            "sub": user_id,
            "name": username,
            "mail": email,
            "role": role,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        }

    return _create_payload
