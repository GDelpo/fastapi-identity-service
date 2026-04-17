"""Email templates module — powered by Jinja2.

Uses Jinja2 Environment with FileSystemLoader for proper template
inheritance ({% extends %}, {% block %}), autoescaping, and cleaner code.
"""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

# Template directory
TEMPLATE_DIR = Path(__file__).parent

# Jinja2 environment — shared across all renders
_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),  # Auto-escape HTML, not .txt
    trim_blocks=True,
    lstrip_blocks=True,
)


def _render(template_name: str, **kwargs: object) -> str:
    """Render a single template with the given context."""
    return _env.get_template(template_name).render(**kwargs)


def render_password_reset(
    to_name: str,
    reset_link: str,
    expire_minutes: int,
) -> tuple[str, str]:
    """Render password reset email (HTML + text)."""
    ctx = {"name": to_name, "reset_link": reset_link, "expire_minutes": expire_minutes}
    return _render("password_reset.html", **ctx), _render("password_reset.txt", **ctx)


def render_email_verification(
    to_name: str,
    verify_link: str,
    expire_hours: int,
) -> tuple[str, str]:
    """Render email verification email (HTML + text)."""
    ctx = {"name": to_name, "verify_link": verify_link, "expire_hours": expire_hours}
    return _render("email_verification.html", **ctx), _render("email_verification.txt", **ctx)


def render_welcome(to_name: str) -> tuple[str, str]:
    """Render welcome email (HTML + text)."""
    ctx = {"name": to_name}
    return _render("welcome.html", **ctx), _render("welcome.txt", **ctx)


def render_password_changed(
    to_name: str,
    changed_at: str,
) -> tuple[str, str]:
    """Render password changed notification (HTML + text)."""
    ctx = {"name": to_name, "changed_at": changed_at}
    return _render("password_changed.html", **ctx), _render("password_changed.txt", **ctx)


# Template registry for easy access
TEMPLATES = {
    "password_reset": render_password_reset,
    "email_verification": render_email_verification,
    "welcome": render_welcome,
    "password_changed": render_password_changed,
}
