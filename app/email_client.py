"""HTTP client for Mail Sender service integration."""

import httpx
from pydantic import BaseModel

from app.config import settings
from app.logger import get_logger
from app.templates import (
    render_email_verification,
    render_password_changed,
    render_password_reset,
    render_welcome,
)

logger = get_logger(__name__)


class EmailRecipient(BaseModel):
    """Email recipient."""

    email: str
    name: str | None = None


class SendEmailRequest(BaseModel):
    """Request to send an email."""

    to: list[EmailRecipient]
    subject: str
    html_content: str
    text_content: str | None = None
    from_email: str | None = None
    priority: str = "high"  # Password reset/verification are high priority


class EmailClient:
    """Client for Mail Sender service."""

    def __init__(
        self,
        url: str,
        client: httpx.AsyncClient,
        auth_token: str | None = None,
    ):
        """Initialize email client.

        Args:
            url: Full URL to the mailsender emails endpoint
                 (e.g., http://mailsender_api:8081/api/v1/emails)
            client: Shared httpx.AsyncClient instance
            auth_token: Bearer token for authentication (propagated from caller)
        """
        self.url = url
        self._client = client
        self.auth_token = auth_token

    async def send_email(self, request: SendEmailRequest) -> dict:
        """Send email via mailsender service.

        Args:
            request: Email request data

        Returns:
            Response from mailsender

        Raises:
            Exception: If sending fails
        """
        # Use propagated auth token
        headers = {}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        try:
            response = await self._client.post(
                self.url,
                json=request.model_dump(exclude_none=True),
                headers=headers,
            )

            if response.status_code in (200, 201, 202):
                result = response.json()
                logger.info(
                    "✉️ Email sent via mailsender",
                    extra={
                        "extra_fields": {
                            "email_id": result.get("id"),
                            "to": request.to[0].email if request.to else None,
                            "subject": request.subject,
                        }
                    },
                )
                return result

            error_detail = response.json().get("detail", response.text)
            logger.error(
                f"❌ Mailsender error: {error_detail}",
                extra={"extra_fields": {"status": response.status_code}},
            )
            raise Exception(f"Failed to send email: {error_detail}")

        except httpx.RequestError as e:
            logger.error(
                f"❌ Mailsender connection error: {e}",
                extra={"extra_fields": {"url": self.url}},
            )
            raise Exception(f"Mail service unavailable: {e}") from e

    async def send_password_reset(
        self,
        to_email: str,
        to_name: str,
        reset_token: str,
        callback_url: str,
    ) -> dict:
        """Send password reset email.

        Args:
            to_email: User's email address
            to_name: User's display name
            reset_token: Password reset token
            callback_url: Base URL for password reset (e.g., https://myapp.com/reset-password)

        Returns:
            Response from mailsender
        """
        # Build full link: callback_url?token=xxx
        separator = "&" if "?" in callback_url else "?"
        reset_link = f"{callback_url}{separator}token={reset_token}"

        html_content, text_content = render_password_reset(
            to_name=to_name,
            reset_link=reset_link,
            expire_minutes=settings.password_reset_token_expire_minutes,
        )

        return await self.send_email(
            SendEmailRequest(
                to=[EmailRecipient(email=to_email, name=to_name)],
                subject="Restablecer tu contraseña",
                html_content=html_content,
                text_content=text_content,
                priority="critical",
            )
        )

    async def send_email_verification(
        self,
        to_email: str,
        to_name: str,
        verification_token: str,
        callback_url: str,
    ) -> dict:
        """Send email verification email.

        Args:
            to_email: User's email address
            to_name: User's display name
            verification_token: Email verification token
            callback_url: Base URL for email verification (e.g., https://myapp.com/verify-email)

        Returns:
            Response from mailsender
        """
        # Build full link: callback_url?token=xxx
        separator = "&" if "?" in callback_url else "?"
        verify_link = f"{callback_url}{separator}token={verification_token}"

        html_content, text_content = render_email_verification(
            to_name=to_name,
            verify_link=verify_link,
            expire_hours=settings.email_verification_token_expire_minutes // 60,
        )

        return await self.send_email(
            SendEmailRequest(
                to=[EmailRecipient(email=to_email, name=to_name)],
                subject="Verifica tu email",
                html_content=html_content,
                text_content=text_content,
                priority="high",
            )
        )

    async def send_welcome_email(
        self,
        to_email: str,
        to_name: str,
    ) -> dict:
        """Send welcome email after registration.

        Args:
            to_email: User's email address
            to_name: User's display name

        Returns:
            Response from mailsender
        """
        html_content, text_content = render_welcome(to_name=to_name)

        return await self.send_email(
            SendEmailRequest(
                to=[EmailRecipient(email=to_email, name=to_name)],
                subject="Bienvenido",
                html_content=html_content,
                text_content=text_content,
                priority="normal",
            )
        )

    async def send_password_changed(
        self,
        to_email: str,
        to_name: str,
    ) -> dict:
        """Send password changed notification email.

        This is a security notification sent after a password has been changed.

        Args:
            to_email: User's email address
            to_name: User's display name

        Returns:
            Response from mailsender
        """
        from datetime import datetime

        changed_at = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        html_content, text_content = render_password_changed(
            to_name=to_name,
            changed_at=changed_at,
        )

        return await self.send_email(
            SendEmailRequest(
                to=[EmailRecipient(email=to_email, name=to_name)],
                subject="Tu contraseña ha sido cambiada",
                html_content=html_content,
                text_content=text_content,
                priority="high",
            )
        )
