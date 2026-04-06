"""Clawdia UI — FastAPI application entry point."""

import uuid
import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import (
    FastAPI,
    Request,
    WebSocket,
    WebSocketDisconnect,
    Depends,
    Form,
    HTTPException,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings, load_quick_actions
from app.auth import router as auth_router, setup_oauth, get_current_user, require_user
from app.models import UserInfo
from app.database import (
    get_db,
    close_db,
    create_conversation,
    get_conversations_for_user,
    get_conversation,
    get_conversation_by_session_key,
    update_conversation_title,
    update_conversation_timestamp,
    delete_conversation,
)
from app.gateway import send_message, get_session_history, delete_session, GatewayError

logger = logging.getLogger(__name__)

# Jinja2 templates
templates = Jinja2Templates(directory="app/templates")


def relative_time(dt_value) -> str:
    """Convert a datetime to a human-readable relative time string."""
    if isinstance(dt_value, str):
        try:
            dt_value = datetime.fromisoformat(dt_value)
        except (ValueError, TypeError):
            return dt_value
    now = datetime.utcnow()
    diff = now - dt_value
    if diff < timedelta(minutes=1):
        return "Just now"
    if diff < timedelta(hours=1):
        mins = int(diff.total_seconds() / 60)
        return f"{mins}m ago"
    if diff < timedelta(hours=24):
        hrs = int(diff.total_seconds() / 3600)
        return f"{hrs}h ago"
    if diff < timedelta(days=2):
        return "Yesterday"
    if diff < timedelta(days=7):
        return f"{diff.days}d ago"
    return dt_value.strftime("%b %d")


def group_conversations(conversations: list[dict]) -> dict[str, list[dict]]:
    """Group conversations into time-based categories."""
    now = datetime.utcnow()
    today = now.date()
    yesterday = today - timedelta(days=1)
    week_ago = today - timedelta(days=7)

    groups = {
        "Today": [],
        "Yesterday": [],
        "Last 7 days": [],
        "Older": [],
    }

    for conv in conversations:
        updated = conv.get("updated_at", "")
        if isinstance(updated, str):
            try:
                updated = datetime.fromisoformat(updated)
            except (ValueError, TypeError):
                groups["Older"].append(conv)
                continue
        conv_date = updated.date()
        if conv_date == today:
            groups["Today"].append(conv)
        elif conv_date == yesterday:
            groups["Yesterday"].append(conv)
        elif conv_date >= week_ago:
            groups["Last 7 days"].append(conv)
        else:
            groups["Older"].append(conv)

    return {k: v for k, v in groups.items() if v}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    setup_oauth()
    await get_db()
    logger.info("Clawdia UI started")
    yield
    await close_db()
    logger.info("Clawdia UI stopped")


app = FastAPI(title="Clawdia UI", lifespan=lifespan)

settings = get_settings()

# Register auth routes
app.include_router(auth_router)

# Add template globals
templates.env.globals["relative_time"] = relative_time


# --- Middleware: auth redirect for HTML pages ---
# NOTE: Starlette processes middleware last-added-first (outermost).
# SessionMiddleware must be added AFTER this so it runs first and
# populates request.session before auth_redirect_middleware reads it.

@app.middleware("http")
async def auth_redirect_middleware(request: Request, call_next):
    """Redirect unauthenticated requests to login for page routes.
    Allow /auth/*, /logged-out, and static assets through."""
    path = request.url.path

    # Public paths
    public_prefixes = ("/auth/", "/logged-out", "/favicon", "/static/")
    if any(path.startswith(p) for p in public_prefixes):
        return await call_next(request)

    # WebSocket connections are handled separately
    if request.headers.get("upgrade", "").lower() == "websocket":
        return await call_next(request)

    # DEBUG mode: auto-populate a fake session so auth is bypassed
    if settings.debug and not request.session.get("user"):
        request.session["user"] = {
            "sub": "debug-user",
            "name": "Debug User",
            "email": "debug@localhost",
            "preferred_username": "debug",
        }

    # Check auth for everything else
    user = get_current_user(request)
    if user is None and path not in ("/logged-out",):
        return RedirectResponse(url="/auth/login", status_code=302)

    return await call_next(request)


# Session middleware — added after auth_redirect so it's outermost and runs first
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="clawdia_session",
    same_site="lax",
    https_only=not settings.debug,
)


# --- Page routes ---

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Main chat page — new conversation."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login")

    conversations = await get_conversations_for_user(user.sub)
    grouped = group_conversations(conversations)
    quick_actions = load_quick_actions()

    return templates.TemplateResponse("chat.html", {
        "request": request,
        "user": user,
        "conversations": grouped,
        "current_conversation": None,
        "messages": [],
        "quick_actions": quick_actions,
    })


@app.get("/chat/{conversation_id}", response_class=HTMLResponse)
async def chat_page(request: Request, conversation_id: int):
    """Chat page for an existing conversation."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login")

    conversation = await get_conversation(conversation_id, user.sub)
    if not conversation:
        return RedirectResponse(url="/")

    conversations = await get_conversations_for_user(user.sub)
    grouped = group_conversations(conversations)
    quick_actions = load_quick_actions()

    # Fetch message history from OpenClaw
    messages = await get_session_history(conversation["session_key"])

    return templates.TemplateResponse("chat.html", {
        "request": request,
        "user": user,
        "conversations": grouped,
        "current_conversation": conversation,
        "messages": messages,
        "quick_actions": quick_actions,
    })


@app.get("/logged-out", response_class=HTMLResponse)
async def logged_out(request: Request):
    """Post-logout landing page."""
    return templates.TemplateResponse("logged_out.html", {
        "request": request,
    })


# --- HTMX partials ---

@app.get("/partials/sidebar", response_class=HTMLResponse)
async def sidebar_partial(request: Request):
    """Return updated sidebar HTML."""
    user = get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401)

    conversations = await get_conversations_for_user(user.sub)
    grouped = group_conversations(conversations)

    return templates.TemplateResponse("sidebar.html", {
        "request": request,
        "user": user,
        "conversations": grouped,
        "current_conversation": None,
    })


@app.get("/partials/messages/{conversation_id}", response_class=HTMLResponse)
async def messages_partial(request: Request, conversation_id: int):
    """Return message history HTML for a conversation."""
    user = get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401)

    conversation = await get_conversation(conversation_id, user.sub)
    if not conversation:
        raise HTTPException(status_code=404)

    messages = await get_session_history(conversation["session_key"])

    return templates.TemplateResponse("messages.html", {
        "request": request,
        "messages": messages,
    })


# --- API routes ---

@app.post("/api/conversations")
async def create_new_conversation(request: Request):
    """Create a new conversation and return its ID."""
    user = get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401)

    username = (user.preferred_username or user.name or "user").lower()
    # Clean username for session key
    username = "".join(c for c in username if c.isalnum() or c == "-")[:20]
    short_uuid = uuid.uuid4().hex[:4]
    session_key = f"clawdia-{username}-{short_uuid}"

    conversation = await create_conversation(
        user_sub=user.sub,
        user_name=user.name,
        session_key=session_key,
    )

    return {"id": conversation["id"], "session_key": conversation["session_key"]}


@app.delete("/api/conversations/{conversation_id}")
async def delete_conv(request: Request, conversation_id: int):
    """Delete a conversation and its OpenClaw session."""
    user = get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401)

    # Get the session key before deleting locally
    conversation = await get_conversation(conversation_id, user.sub)
    if not conversation:
        raise HTTPException(status_code=404)

    # Delete from local DB
    await delete_conversation(conversation_id, user.sub)

    # Best-effort delete from OpenClaw (don't fail if gateway is unreachable)
    try:
        await delete_session(conversation["session_key"])
    except Exception as e:
        logger.warning("Failed to delete OpenClaw session %s: %s", conversation["session_key"], e)

    return {"ok": True}


@app.patch("/api/conversations/{conversation_id}")
async def update_conv(request: Request, conversation_id: int):
    """Update a conversation title."""
    user = get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401)

    body = await request.json()
    title = body.get("title")
    if not title:
        raise HTTPException(status_code=400, detail="Title required")

    conversation = await update_conversation_title(conversation_id, user.sub, title)
    if not conversation:
        raise HTTPException(status_code=404)
    return {"ok": True, "title": conversation["title"]}


# --- WebSocket chat proxy ---

@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """WebSocket endpoint that proxies messages between browser and OpenClaw."""
    await websocket.accept()

    # Authenticate from session
    user_data = websocket.session.get("user")
    logger.info("WebSocket auth check: user_data=%s", "present" if user_data else "MISSING")
    if not user_data:
        await websocket.send_json({"type": "error", "content": "Not authenticated"})
        await websocket.close(code=4001)
        return

    user = UserInfo(**user_data)

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action", "message")

            if action == "message":
                session_key = data.get("session_key", "")
                content = data.get("content", "").strip()

                if not session_key or not content:
                    await websocket.send_json({
                        "type": "error",
                        "content": "Missing session_key or content",
                    })
                    continue

                # Verify the session key belongs to this user
                conversation = await get_conversation_by_session_key(
                    session_key, user.sub
                )
                if not conversation:
                    await websocket.send_json({
                        "type": "error",
                        "content": "Conversation not found",
                    })
                    continue

                # Update timestamp
                await update_conversation_timestamp(session_key, user.sub)

                # Stream response from OpenClaw
                is_first_message = conversation["title"] == "New conversation"
                full_response = []

                try:
                    async for chunk in send_message(session_key, content):
                        # Status events (dict) vs text chunks (str)
                        if isinstance(chunk, dict) and chunk.get("type") == "status":
                            msg = {"type": "status", "status": chunk["status"]}
                            if "tool" in chunk:
                                msg["tool"] = chunk["tool"]
                            await websocket.send_json(msg)
                        else:
                            full_response.append(chunk)
                            await websocket.send_json({
                                "type": "chunk",
                                "content": chunk,
                            })

                    await websocket.send_json({"type": "done"})

                    # Generate title after first exchange
                    if is_first_message and full_response:
                        asyncio.create_task(
                            _generate_title_background(
                                conversation["id"], user.sub,
                                session_key, content
                            )
                        )

                except GatewayError as e:
                    await websocket.send_json({
                        "type": "error",
                        "content": str(e),
                    })

    except WebSocketDisconnect:
        logger.debug("WebSocket disconnected for user %s", user.sub)
    except Exception as e:
        logger.error("WebSocket error: %s", e)
        try:
            await websocket.send_json({
                "type": "error",
                "content": "An unexpected error occurred",
            })
        except Exception:
            pass


async def _generate_title_background(
    conversation_id: int, user_sub: str, session_key: str, first_message: str
):
    """Background task to set a conversation title from the first message."""
    try:
        title = first_message.strip()
        if len(title) > 60:
            # Truncate at a word boundary
            title = title[:57].rsplit(" ", 1)[0] + "..."
        if title:
            await update_conversation_title(conversation_id, user_sub, title)
    except Exception as e:
        logger.error("Failed to auto-title conversation %d: %s", conversation_id, e)
