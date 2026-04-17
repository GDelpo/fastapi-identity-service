"""LDAP client for user authentication and lookup."""

import uuid
from typing import TYPE_CHECKING

from fastapi.concurrency import run_in_threadpool

if TYPE_CHECKING:
    from ldap3 import Connection

from app.exceptions import ExternalServiceError
from app.logger import get_logger
from app.models import UserRole
from app.schemas import UserResponse

logger = get_logger(__name__)


def _escape_ldap_filter_chars(value: str) -> str:
    """
    Escape special characters in LDAP filter values (RFC 4515).

    Prevents LDAP injection by converting special chars to hex.
    """
    if not value:
        return value

    escape_map = {
        "*": r"\2a",
        "(": r"\28",
        ")": r"\29",
        "\\": r"\5c",
        "\x00": r"\00",
    }

    result = value
    for char, escaped in escape_map.items():
        result = result.replace(char, escaped)
    return result


class LDAPClient:
    """Thread-safe LDAP client for FastAPI."""

    def __init__(
        self,
        ldap_url: str,
        admin_username: str,
        admin_password: str,
        search_bases: list[str] | None = None,
        ou_to_avoid: list[str] | None = None,
        attributes: list[str] | None = None,
    ):
        self.ldap_url = ldap_url
        self.admin_username = admin_username
        self.admin_password = admin_password
        self.search_bases = search_bases or []
        self.ou_to_avoid = ou_to_avoid or []
        self.attributes = attributes
        self.is_available = False
        self._last_error: str | None = None

    def _get_connection(
        self, user: str | None = None, password: str | None = None
    ) -> Connection:
        """Create sync connection for threadpool execution."""
        from ldap3 import ALL, SAFE_SYNC, SIMPLE, Connection, Server

        server = Server(self.ldap_url, get_info=ALL)
        return Connection(
            server,
            user=user or self.admin_username,
            password=password or self.admin_password,
            authentication=SIMPLE,
            client_strategy=SAFE_SYNC,
            auto_bind=True,
        )

    # --- Public async methods ---

    async def test_connection(self) -> bool:
        """Test LDAP connection."""
        return await run_in_threadpool(self._sync_test_connection)

    async def authenticate(self, username: str, password: str) -> bool:
        """Authenticate user against LDAP."""
        return await run_in_threadpool(self._sync_authenticate, username, password)

    async def get(self, account_name: str) -> UserResponse | None:
        """Get single user by account name."""
        users = await run_in_threadpool(self._sync_search, account_name)
        return users[0] if users else None

    async def get_by_email(self, email: str) -> UserResponse | None:
        """Get single user by email address."""
        users = await run_in_threadpool(self._sync_search, None, None, 0, 1, email)
        return users[0] if users else None

    async def get_all(
        self,
        search: str | None = None,
        skip: int = 0,
        limit: int | None = None,
    ) -> list[UserResponse]:
        """Get all users with optional search and pagination."""
        return await run_in_threadpool(self._sync_search, None, search, skip, limit)

    def get_status(self) -> tuple[bool, str | None]:
        """Get connection status."""
        return (self.is_available, self._last_error)

    # --- Sync methods (run in threadpool) ---

    def _sync_test_connection(self) -> bool:
        try:
            with self._get_connection() as conn:
                if conn.bound:
                    self.is_available = True
                    self._last_error = None
                    logger.info("LDAP is available")
                    return True
            return False
        except Exception as e:
            self.is_available = False
            self._last_error = str(e)
            logger.warning("LDAP unavailable: %s", self._last_error)
            return False

    def _sync_authenticate(self, username: str, password: str) -> bool:
        from ldap3.core.exceptions import LDAPBindError

        try:
            user_data = self._sync_search(account_name=username)
            if not user_data:
                logger.warning("User %s not found in LDAP", username)
                return False

            user_dn = f"{username}@{self.admin_username.split('@')[1]}"
            with self._get_connection(user=user_dn, password=password):
                logger.info("LDAP auth successful: %s", username)
                return True

        except LDAPBindError:
            logger.warning("LDAP auth failed: %s", username)
            return False
        except Exception as e:
            logger.error("LDAP auth error for %s: %s", username, e)
            return False

    def _sync_search(
        self,
        account_name: str | None = None,
        search_term: str | None = None,
        skip: int = 0,
        limit: int | None = None,
        email_exact: str | None = None,
        page_size: int = 50,
    ) -> list[UserResponse]:
        """Search users in LDAP with pagination."""
        from ldap3 import SUBTREE

        users: list[UserResponse] = []
        search_filter = self._build_filter(account_name, search_term, email_exact)
        collected = 0

        try:
            with self._get_connection() as conn:
                for search_base in self.search_bases:
                    entries = conn.extend.standard.paged_search(
                        search_base=search_base,
                        search_filter=search_filter,
                        search_scope=SUBTREE,
                        attributes=self.attributes,
                        paged_size=page_size,
                        generator=True,
                    )

                    for entry in entries:
                        if limit and collected >= skip + limit:
                            break

                        if "attributes" in entry:
                            for user in self._process_entry(entry):
                                if collected >= skip:
                                    users.append(user)
                                    if limit and len(users) >= limit:
                                        break
                                collected += 1

                        if limit and len(users) >= limit:
                            break

                    if limit and len(users) >= limit:
                        break

            return users
        except Exception as e:
            logger.error("LDAP search error: %s", e)
            raise ExternalServiceError("LDAP", f"Search failed: {e}") from None

    def _build_filter(
        self,
        account_name: str | None,
        search_term: str | None,
        email_exact: str | None = None,
    ) -> str:
        """Build LDAP filter with injection protection."""
        base = "(&(objectClass=user)(!(objectClass=computer)))"

        if account_name:
            escaped = _escape_ldap_filter_chars(account_name)
            return f"(&{base}(sAMAccountName={escaped}))"

        if email_exact:
            escaped = _escape_ldap_filter_chars(email_exact.lower())
            return f"(&{base}(mail={escaped}))"

        if search_term:
            escaped = _escape_ldap_filter_chars(search_term)
            return f"(&{base}(|(sAMAccountName=*{escaped}*)(mail=*{escaped}*)))"

        return base

    def _process_entry(self, entry: dict) -> list[UserResponse]:
        """Process LDAP entry into UserResponse."""
        if "dn" not in entry:
            return []

        dn = entry["dn"]
        if any(ou in dn for ou in self.ou_to_avoid):
            return []

        cn = self._extract_cn(dn)
        if not cn:
            return []

        attrs = entry.get("attributes", {})
        mail = attrs.get("mail")
        sam = attrs.get("sAMAccountName")

        if not mail or not sam:
            return []

        if isinstance(mail, list):
            mail = mail[0] if mail else None
        if isinstance(sam, str):
            sam = sam.strip()

        if not mail or not sam:
            return []

        try:
            return [
                UserResponse(
                    id=uuid.uuid5(uuid.NAMESPACE_DNS, sam),
                    user_name=sam,
                    role=UserRole.EMPLOYEE,
                    mail=mail.strip().lower(),
                    is_active=True,
                    created_at=attrs.get("whenCreated"),
                    updated_at=attrs.get("whenChanged"),
                )
            ]
        except ValueError as e:
            logger.debug("Skipping LDAP user %s: %s", sam, e)
            return []

    def _extract_cn(self, dn: str) -> str | None:
        """Extract CN from DN."""
        for component in dn.split(","):
            if component.startswith("CN="):
                return component.split("=")[1]
        return None
