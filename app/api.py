"""API endpoints for users."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, status
from fastapi.security import OAuth2PasswordRequestForm

from app.auth import create_access_token
from app.dependencies import (
    PaginationParams,
    UserFilterParams,
    admin_or_service_required,
    admin_role_required,
    get_current_user,
    get_email_client,
    get_pagination_params,
    get_user_filters,
    get_user_service,
    service_role_required,
)
from app.email_client import EmailClient
from app.exceptions import AuthenticationError
from app.logger import get_logger
from app.schemas import (
    EmailVerificationConfirm,
    EmailVerificationResponse,
    PaginatedResponse,
    PaginationMeta,
    PasswordResetConfirm,
    PasswordResetRequest,
    PasswordResetResponse,
    Token,
    TokenData,
    UserCreate,
    UserRegister,
    UserRegisterResponse,
    UserResponse,
    UserUpdate,
    mask_email,
)
from app.service import UserService

router = APIRouter(tags=["Users"])
logger = get_logger(__name__)


# ============================================================================
# Authentication (must be before /{user_id} to avoid route conflicts)
# ============================================================================


@router.post(
    "/login",
    response_model=Token,
    summary="Authenticate (login)",
    description="Login using email address or username. Supports both LDAP and local users.",
)
async def generate_token(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    user_service: UserService = Depends(get_user_service),
) -> Token:
    """Generate access token (login).

    Accepts either email address or username as the login identifier.
    - Email: user@example.com
    - Username: jdoe (sAMAccountName for LDAP users)
    """
    logger.info("🔑 Login attempt", extra={"extra_fields": {"identifier": form_data.username}})
    try:
        user = await user_service.authenticate(form_data.username, form_data.password)
        logger.info(
            "✅ Login successful",
            extra={
                "extra_fields": {
                    "identifier": form_data.username,
                    "user_id": str(user.id),
                }
            },
        )
        return await create_access_token(user)
    except AuthenticationError:
        # Re-raise authentication errors with their specific message
        # (account locked, email not verified, account inactive)
        raise
    except Exception as e:
        logger.warning(
            "❌ Login failed",
            extra={
                "extra_fields": {
                    "identifier": form_data.username,
                    "error": str(e),
                }
            },
        )
        raise AuthenticationError("Invalid username or password") from None


# ============================================================================
# Agent Registration (Service accounts only)
# ============================================================================


@router.post(
    "/register",
    response_model=UserRegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register new agent",
    description="Register a new agent user or resend verification for unverified user. Requires SERVICE account authentication.",
)
async def register_user(
    register_data: UserRegister,
    current_user: TokenData = Depends(service_role_required),
    user_service: UserService = Depends(get_user_service),
    email_client: EmailClient = Depends(get_email_client),
) -> UserRegisterResponse:
    """
    Register a new agent user or resend verification.

    - If user doesn't exist: creates new user and sends verification email
    - If user exists but NOT verified: resends verification email (with rate limit)
    - If user exists and IS verified: returns error

    Requires SERVICE role authentication (service-to-service calls).
    Only AGENT role is allowed for registration via this endpoint.
    """
    logger.info(
        "📝 Agent registration request",
        extra={
            "extra_fields": {
                "username": register_data.user_name,
                "registered_by": current_user.user_name,
                "email": register_data.mail,
            }
        },
    )

    try:
        user, is_resend = await user_service.register_user(register_data)

        # Send/resend email verification
        email_sent_to = None
        try:
            result = await user_service.request_email_verification(str(user.mail))
            if result:
                _, token = result
                await email_client.send_email_verification(
                    to_email=str(user.mail),
                    to_name=user.user_name,
                    verification_token=token,
                    callback_url=register_data.callback_url,
                )
                email_sent_to = mask_email(str(user.mail))
                action = "resent" if is_resend else "sent"
                logger.info(
                    f"✅ Verification email {action}",
                    extra={
                        "extra_fields": {
                            "username": user.user_name,
                            "user_id": str(user.id),
                            "is_resend": is_resend,
                            "email_sent_to": email_sent_to,
                            "callback_url": register_data.callback_url,
                        }
                    },
                )
            else:
                # Rate limited - don't reveal this, just log
                logger.info(
                    "⏳ Verification email rate limited",
                    extra={
                        "extra_fields": {
                            "username": user.user_name,
                            "user_id": str(user.id),
                        }
                    },
                )
        except Exception as e:
            logger.error(
                "❌ Failed to send verification email",
                extra={
                    "extra_fields": {
                        "username": user.user_name,
                        "error": str(e),
                    }
                },
                exc_info=True,
            )
            # Don't fail registration if email fails

        if is_resend:
            return UserRegisterResponse(
                message="Verification email has been resent. Please check your inbox.",
                success=True,
                user=user,
                is_resend=True,
                email_sent_to=email_sent_to,
            )
        else:
            return UserRegisterResponse(
                message="Registration successful. Please check your email to verify your account.",
                success=True,
                user=user,
                is_resend=False,
                email_sent_to=email_sent_to,
            )

    except Exception as e:
        logger.warning(
            "❌ Registration failed",
            extra={
                "extra_fields": {
                    "username": register_data.user_name,
                    "error": str(e),
                }
            },
        )
        raise


# Public endpoint - user accesses via email link, JWT token provides security
@router.post(
    "/register/confirm",
    response_model=EmailVerificationResponse,
    summary="Confirm email verification",
    description="Verify email using the token received by email. Public endpoint.",
)
async def confirm_registration(
    confirm_data: EmailVerificationConfirm,
    user_service: UserService = Depends(get_user_service),
    email_client: EmailClient = Depends(get_email_client),
) -> EmailVerificationResponse:
    """
    Confirm email verification with token to complete registration.

    Public endpoint - the JWT token in the email link provides authentication.
    The token is received via email after calling POST /register.
    """
    logger.info("📧 Registration confirmation attempt")

    user = await user_service.confirm_email_verification(token=confirm_data.token)

    logger.info("✅ Registration confirmed - email verified")

    # Send welcome email (don't fail if this fails)
    try:
        await email_client.send_welcome_email(
            to_email=str(user.mail),
            to_name=user.user_name,
        )
        logger.info(
            "✅ Welcome email sent",
            extra={"extra_fields": {"user_id": str(user.id), "email": user.mail}},
        )
    except Exception as e:
        logger.error(
            "❌ Failed to send welcome email",
            extra={"extra_fields": {"user_id": str(user.id), "error": str(e)}},
            exc_info=True,
        )

    return EmailVerificationResponse(
        message="Email verified successfully. You can now log in.",
        success=True,
        verified=True,
    )


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Get current user",
)
async def get_me(
    current_user: TokenData = Depends(get_current_user),
    user_service: UserService = Depends(get_user_service),
    fresh: Annotated[bool, Query(description="Reload from database/LDAP")] = False,
) -> UserResponse:
    """Get authenticated user info."""
    if fresh:
        return await user_service.get_user(current_user.user_name, use_cache=False)

    return UserResponse(
        id=current_user.id,
        user_name=current_user.user_name,
        mail=current_user.mail,
        role=current_user.role,
        is_active=True,
        created_at=None,
        updated_at=None,
    )


# ============================================================================
# User CRUD
# ============================================================================


@router.post(
    "",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create user",
)
async def create_user(
    user_data: UserCreate,
    current_user: TokenData = Depends(admin_role_required),
    user_service: UserService = Depends(get_user_service),
) -> UserResponse:
    """Create new user (admin only)."""
    logger.info(
        "👤 Creating new user",
        extra={
            "extra_fields": {
                "username": user_data.user_name,
                "created_by": current_user.user_name,
            }
        },
    )
    try:
        user = await user_service.create_user(user_data)
        logger.info(
            "✅ User created successfully",
            extra={
                "extra_fields": {
                    "username": user_data.user_name,
                    "user_id": str(user.id),
                    "created_by": current_user.user_name,
                }
            },
        )
        return user
    except Exception as e:
        logger.error(
            "❌ Failed to create user",
            extra={
                "extra_fields": {
                    "username": user_data.user_name,
                    "created_by": current_user.user_name,
                    "error": str(e),
                }
            },
            exc_info=True,
        )
        raise


@router.get(
    "",
    response_model=PaginatedResponse[UserResponse],
    summary="List users",
)
async def get_users(
    pagination: PaginationParams = Depends(get_pagination_params),
    filters: UserFilterParams = Depends(get_user_filters),
    current_user: TokenData = Depends(admin_or_service_required),
    user_service: UserService = Depends(get_user_service),
) -> PaginatedResponse[UserResponse]:
    """List users with pagination and filters (admin or service account)."""
    logger.debug(
        "📋 Listing users",
        extra={
            "extra_fields": {
                "requested_by": current_user.user_name,
                "skip": pagination.skip,
                "limit": pagination.limit,
                "filters": {
                    "role": filters.role,
                    "is_active": filters.is_active,
                    "search": filters.search,
                },
            }
        },
    )
    users = await user_service.list_users(
        role=filters.role,
        is_active=filters.is_active,
        search=filters.search,
        skip=pagination.skip,
        limit=pagination.limit,
        sort_order=filters.get_sort_order(),
    )

    total = await user_service.count_users(
        role=filters.role,
        is_active=filters.is_active,
        search=filters.search,
    )

    meta = PaginationMeta.from_params(total, pagination.skip, pagination.limit)
    return PaginatedResponse(data=users, meta=meta)


@router.get(
    "/{user_id}",
    response_model=UserResponse,
    summary="Get user by ID",
)
async def get_user(
    user_id: Annotated[UUID, Path(..., description="User UUID")],
    current_user: TokenData = Depends(admin_role_required),
    user_service: UserService = Depends(get_user_service),
    user_name: Annotated[str | None, Query(description="Search by username instead")] = None,
    fresh: Annotated[bool, Query(description="Bypass cache")] = False,
) -> UserResponse:
    """Get user by UUID or username (admin only)."""
    logger.debug(
        "🔍 Getting user details",
        extra={
            "extra_fields": {
                "requested_by": current_user.user_name,
                "user_id": str(user_id) if not user_name else None,
                "user_name": user_name,
                "bypass_cache": fresh,
            }
        },
    )
    if user_name:
        return await user_service.get_user(username=user_name, use_cache=not fresh)
    return await user_service.get_user_by_id(user_id=str(user_id), use_cache=not fresh)


@router.patch(
    "/{user_id}",
    response_model=UserResponse,
    summary="Update user",
    description="Update local database user. **Does not apply to LDAP users.**",
)
async def update_user(
    user_id: Annotated[UUID, Path(..., description="User UUID")],
    user_data: UserUpdate,
    current_user: TokenData = Depends(admin_role_required),
    user_service: UserService = Depends(get_user_service),
) -> UserResponse:
    """Update user (admin only). Only works for local DB users, not LDAP."""
    logger.info(
        "✏️ Updating user",
        extra={
            "extra_fields": {
                "user_id": str(user_id),
                "updated_by": current_user.user_name,
                "fields_updated": list(user_data.model_dump(exclude_unset=True).keys()),
            }
        },
    )
    try:
        return await user_service.update_user_by_id(str(user_id), user_data)
    except Exception as e:
        logger.error(
            "❌ Failed to update user",
            extra={
                "extra_fields": {
                    "user_id": str(user_id),
                    "updated_by": current_user.user_name,
                    "error": str(e),
                }
            },
            exc_info=True,
        )
        raise


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete user",
    description="Delete local database user. **Does not apply to LDAP users.**",
)
async def delete_user(
    user_id: Annotated[UUID, Path(..., description="User UUID")],
    current_user: TokenData = Depends(admin_role_required),
    user_service: UserService = Depends(get_user_service),
) -> None:
    """Delete user (admin only). Only works for local DB users, not LDAP."""
    logger.info(
        "🗑️ Deleting user",
        extra={
            "extra_fields": {
                "user_id": str(user_id),
                "deleted_by": current_user.user_name,
            }
        },
    )
    try:
        await user_service.delete_user_by_id(str(user_id))
        logger.info(
            "✅ User deleted successfully",
            extra={
                "extra_fields": {
                    "user_id": str(user_id),
                    "deleted_by": current_user.user_name,
                }
            },
        )
    except Exception as e:
        logger.error(
            "❌ Failed to delete user",
            extra={
                "extra_fields": {
                    "user_id": str(user_id),
                    "deleted_by": current_user.user_name,
                    "error": str(e),
                }
            },
            exc_info=True,
        )
        raise


# ============================================================================
# Password Reset
# ============================================================================


@router.post(
    "/password-reset/request",
    response_model=PasswordResetResponse,
    summary="Request password reset",
    description="Request a password reset email. Requires SERVICE account authentication.",
)
async def request_password_reset(
    request_data: PasswordResetRequest,
    current_user: TokenData = Depends(service_role_required),
    user_service: UserService = Depends(get_user_service),
    email_client: EmailClient = Depends(get_email_client),
) -> PasswordResetResponse:
    """
    Request password reset email.

    Requires SERVICE role authentication (service-to-service calls).
    For security, always returns success even if email doesn't exist.
    """
    logger.info(
        "🔐 Password reset requested",
        extra={
            "extra_fields": {
                "email": request_data.email,
                "requested_by": current_user.user_name,
            }
        },
    )

    result = await user_service.request_password_reset(request_data.email)

    email_sent_to = None
    if result:
        user, token = result
        try:
            await email_client.send_password_reset(
                to_email=user.mail,
                to_name=user.user_name,
                reset_token=token,
                callback_url=request_data.callback_url,
            )
            email_sent_to = mask_email(user.mail)

            logger.info(
                "✅ Password reset email sent",
                extra={
                    "extra_fields": {
                        "email": request_data.email,
                        "masked": email_sent_to,
                        "callback_url": request_data.callback_url,
                    }
                },
            )
        except Exception as e:
            logger.error(
                "❌ Failed to send password reset email",
                extra={
                    "extra_fields": {
                        "email": request_data.email,
                        "error": str(e),
                    }
                },
                exc_info=True,
            )
            # Don't reveal email sending failure

    return PasswordResetResponse(
        message="If the email exists, a password reset link has been sent.",
        success=True,
        email_sent_to=email_sent_to,
    )


# Public endpoint - user accesses via email link, JWT token provides security
@router.post(
    "/password-reset/confirm",
    response_model=PasswordResetResponse,
    summary="Confirm password reset",
    description="Reset password using the token received by email. Public endpoint.",
)
async def confirm_password_reset(
    confirm_data: PasswordResetConfirm,
    user_service: UserService = Depends(get_user_service),
    email_client: EmailClient = Depends(get_email_client),
) -> PasswordResetResponse:
    """
    Confirm password reset with token.

    Public endpoint - the JWT token in the email link provides authentication.
    The token is received via email after requesting a password reset.
    """
    logger.info("🔐 Password reset confirmation attempt")

    user = await user_service.confirm_password_reset(
        token=confirm_data.token,
        new_password=confirm_data.new_password,
    )

    logger.info("✅ Password reset successful")

    # Send password changed notification (don't fail if this fails)
    try:
        await email_client.send_password_changed(
            to_email=str(user.mail),
            to_name=user.user_name,
        )
        logger.info(
            "✅ Password changed notification sent",
            extra={"extra_fields": {"user_id": str(user.id), "email": user.mail}},
        )
    except Exception as e:
        logger.error(
            "❌ Failed to send password changed notification",
            extra={"extra_fields": {"user_id": str(user.id), "error": str(e)}},
            exc_info=True,
        )

    return PasswordResetResponse(
        message="Password has been reset successfully.",
        success=True,
    )
