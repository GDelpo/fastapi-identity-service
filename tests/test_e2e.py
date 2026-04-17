"""
E2E Tests - End-to-end workflow tests

Tests complete user flows like login → create → get → update → delete

REQUIREMENTS:
    These tests require running infrastructure:
    - PostgreSQL with migrations applied (admin user exists)
    - Redis for caching

    Start with: docker compose -f docker/docker-compose.dev.yml up -d
    Apply migrations: alembic upgrade head

    Or run: pytest tests/test_e2e.py -m e2e
"""

import pytest

from app.config import settings

# Build API routes dynamically from settings
API_PREFIX = settings.api_prefix
ADMIN_USER = settings.admin_user
ADMIN_PASSWORD = settings.admin_password.get_secret_value()


def _get_admin_token(client):
    """Helper to get admin token, returns None if login fails."""
    response = client.post(
        f"{API_PREFIX}/login",
        data={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
    )
    if response.status_code == 200:
        return response.json().get("access_token")
    return None


# ============================================================================
# Authentication E2E Tests
# ============================================================================


@pytest.mark.e2e
class TestAuthenticationE2E:
    """End-to-end authentication flow tests."""

    def test_login_success_admin(self, client):
        """Test successful admin login (requires infrastructure)."""
        response = client.post(
            f"{API_PREFIX}/login",
            data={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
        )
        if response.status_code == 401:
            pytest.skip("Infrastructure not available (no admin user)")
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["expires_in"] == 3600

    def test_login_invalid_username(self, client):
        """Test login with invalid username."""
        response = client.post(
            f"{API_PREFIX}/login",
            data={"username": "nonexistent", "password": "AnyPass123!"},
        )
        assert response.status_code == 401
        assert "Invalid username or password" in response.text

    def test_login_invalid_password(self, client):
        """Test login with invalid password."""
        response = client.post(
            f"{API_PREFIX}/login",
            data={"username": ADMIN_USER, "password": "WrongPassword123!"},
        )
        assert response.status_code == 401
        assert "Invalid username or password" in response.text

    def test_login_missing_fields(self, client):
        """Test login with missing fields."""
        response = client.post(
            f"{API_PREFIX}/login",
            data={"username": ADMIN_USER},
        )
        assert response.status_code in [400, 422]

    def test_token_via_me(self, client):
        """Test token validation via /me endpoint."""
        # Get token
        token = _get_admin_token(client)
        if not token:
            pytest.skip("Infrastructure not available (no admin user)")

        # Validate token via /me
        me_response = client.get(
            f"{API_PREFIX}/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me_response.status_code == 200
        data = me_response.json()
        assert data["user_name"] == ADMIN_USER
        assert data["role"] == "admin"

    def test_invalid_token(self, client):
        """Test access with invalid token."""
        response = client.get(
            f"{API_PREFIX}/me",
            headers={"Authorization": "Bearer invalid_token_123"},
        )
        assert response.status_code == 401

    def test_missing_token(self, client):
        """Test access without token."""
        response = client.get(f"{API_PREFIX}/me")
        assert response.status_code == 401  # Not authenticated

    def test_get_current_user(self, client):
        """Test getting current user info."""
        # Login
        token = _get_admin_token(client)
        if not token:
            pytest.skip("Infrastructure not available (no admin user)")

        # Get me
        me_response = client.get(
            f"{API_PREFIX}/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me_response.status_code == 200
        data = me_response.json()
        assert data["user_name"] == ADMIN_USER
        assert data["role"] == "admin"
        assert data["is_active"] is True


# ============================================================================
# Health Check Tests
# ============================================================================


@pytest.mark.e2e
class TestHealthE2E:
    """Health check endpoint tests."""

    def test_health_check(self, client):
        """Test main health check endpoint returns valid response."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] in ["healthy", "unhealthy", "degraded"]
        assert "version" in data
        assert "environment" in data
        assert "dependencies" in data

    def test_health_check_healthy(self, client):
        """Test health check is healthy when infra is available."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        if data["status"] != "healthy":
            pytest.skip("Infrastructure not fully available")
        # dependencies is a dict with postgresql, redis, etc.
        assert isinstance(data["dependencies"], dict)

    def test_root_endpoint(self, client):
        """Test root endpoint returns service info."""
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert "service" in data
        assert "version" in data
        assert data["health"] == "/health"


# ============================================================================
# API Documentation Tests
# ============================================================================


@pytest.mark.e2e
class TestAPIDocumentationE2E:
    """API documentation endpoint tests."""

    def test_swagger_ui(self, client):
        """Test Swagger UI is available."""
        response = client.get("/docs")
        assert response.status_code == 200
        assert "swagger" in response.text.lower()

    def test_redoc(self, client):
        """Test ReDoc is available."""
        response = client.get("/redoc")
        assert response.status_code == 200


# ============================================================================
# Error Handling E2E Tests
# ============================================================================


@pytest.mark.e2e
class TestErrorHandlingE2E:
    """Error handling and edge case tests."""

    def test_nonexistent_endpoint(self, client):
        """Test accessing nonexistent endpoint."""
        response = client.get(f"{API_PREFIX}/nonexistent")
        assert response.status_code == 404

    def test_invalid_json_body(self, client):
        """Test sending invalid JSON."""
        response = client.post(
            f"{API_PREFIX}/login",
            content="not json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code in [400, 422]

    def test_wrong_http_method(self, client):
        """Test using wrong HTTP method."""
        # Root endpoint only accepts GET, not POST
        response = client.post("/")
        assert response.status_code == 405  # Method not allowed

    def test_malformed_auth_header(self, client):
        """Test with malformed authorization header."""
        response = client.get(
            f"{API_PREFIX}/me",
            headers={"Authorization": "NotBearer invalid_token"},
        )
        assert response.status_code == 401

    def test_expired_token_behavior(self, client):
        """Test behavior with expired/invalid token format."""
        response = client.get(
            f"{API_PREFIX}/me",
            headers={
                "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.invalid.invalid"
            },
        )
        assert response.status_code == 401
