"""OIDC authentication via Authentik using authlib."""

import secrets
from typing import Optional

from authlib.integrations.starlette_client import OAuth
from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from fastapi import APIRouter, Depends, HTTPException

from app.config import get_settings
from app.models import UserInfo

router = APIRouter(prefix="/auth", tags=["auth"])

oauth = OAuth()


def setup_oauth():
    """Register the OIDC provider with authlib. Called at app startup."""
    settings = get_settings()

    # Strip trailing slash from issuer for consistency
    issuer = settings.oidc_issuer.rstrip("/")

    oauth.register(
        name="authentik",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        server_metadata_url=f"{issuer}/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile email"},
    )


def get_current_user(request: Request) -> Optional[UserInfo]:
    """Extract the current user from the session. Returns None if not authenticated."""
    user_data = request.session.get("user")
    if not user_data:
        return None
    return UserInfo(**user_data)


def require_user(request: Request) -> UserInfo:
    """Dependency that requires authentication. Raises 401 if not logged in."""
    user = get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


@router.get("/login")
async def login(request: Request):
    """Initiate OIDC login flow."""
    settings = get_settings()
    redirect_uri = str(request.url_for("auth_callback"))
    # Generate and store state for CSRF protection
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    return await oauth.authentik.authorize_redirect(
        request, redirect_uri, state=state
    )


@router.get("/callback")
async def auth_callback(request: Request):
    """Handle OIDC callback from Authentik."""
    settings = get_settings()
    token = await oauth.authentik.authorize_access_token(request)
    userinfo = token.get("userinfo")

    if not userinfo:
        raise HTTPException(status_code=400, detail="Failed to get user info")

    # Store user info in session
    user = UserInfo(
        sub=userinfo.get("sub", ""),
        name=userinfo.get("name") or userinfo.get("preferred_username", "Unknown"),
        email=userinfo.get("email"),
        preferred_username=userinfo.get("preferred_username"),
    )
    request.session["user"] = user.model_dump()

    return RedirectResponse(url=settings.login_redirect_url, status_code=302)


@router.get("/logout")
async def logout(request: Request):
    """Clear session and redirect to logged-out page."""
    settings = get_settings()
    request.session.clear()
    return RedirectResponse(url=settings.logout_redirect_url, status_code=302)
