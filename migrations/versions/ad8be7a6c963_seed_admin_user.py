"""seed_admin_user

Revision ID: ad8be7a6c963
Revises: d46ca6d55b8d
Create Date: 2026-01-07 18:28:07.392995

"""

from typing import Sequence, Union
import os
import uuid
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa
import bcrypt


# revision identifiers, used by Alembic.
revision: str = "ad8be7a6c963"
down_revision: Union[str, None] = "d46ca6d55b8d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _hash_password(password: str) -> str:
    """Hash password with bcrypt."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def upgrade() -> None:
    """Seed initial admin user."""
    conn = op.get_bind()

    # Get credentials from environment - ALL REQUIRED
    admin_user = os.getenv("ADMIN_USER")
    admin_password = os.getenv("ADMIN_PASSWORD")
    admin_email = os.getenv("ADMIN_EMAIL")

    if not all([admin_user, admin_password, admin_email]):
        print(
            "[seed-admin] ADMIN_USER, ADMIN_PASSWORD, ADMIN_EMAIL required; skipping admin seed"
        )
        return

    # Check if user already exists
    existing = conn.execute(
        sa.text("SELECT id FROM users WHERE user_name = :user_name"),
        {"user_name": admin_user},
    ).scalar_one_or_none()

    if existing:
        print(f"[seed-admin] Admin '{admin_user}' already exists; skipping")
        return

    # Create admin user
    hashed_password = _hash_password(admin_password)
    now = datetime.now(timezone.utc)

    conn.execute(
        sa.text("""
            INSERT INTO users 
            (id, user_name, mail, hashed_password, role, is_active, is_verified, 
             failed_login_attempts, created_at)
            VALUES 
            (:id, :user_name, :mail, :hashed_password, :role, :is_active, 
             :is_verified, :failed_login_attempts, :created_at)
        """),
        {
            "id": str(uuid.uuid4()),
            "user_name": admin_user,
            "mail": admin_email,
            "hashed_password": hashed_password,
            "role": "ADMIN",
            "is_active": True,
            "is_verified": True,
            "failed_login_attempts": 0,
            "created_at": now,
        },
    )
    conn.commit()
    print(f"[seed-admin] Admin '{admin_user}' created successfully")


def downgrade() -> None:
    """Remove admin user."""
    conn = op.get_bind()
    admin_user = os.getenv("ADMIN_USER")
    if not admin_user:
        print("[seed-admin] ADMIN_USER not set; skipping downgrade")
        return
    conn.execute(
        sa.text("DELETE FROM users WHERE user_name = :user_name"),
        {"user_name": admin_user},
    )
    conn.commit()
    print(f"[seed-admin] Admin '{admin_user}' removed")
