"""OpenClaw WebSocket gateway proxy and session history client."""

import json
import asyncio
import logging
from typing import AsyncGenerator, Optional

import websockets
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    InvalidStatusCode,
)

from app.config import get_settings

logger = logging.getLogger(__name__)


class GatewayError(Exception):
    """Raised when the gateway returns an error or is unreachable."""
    pass


async def send_message(
    session_key: str,
    message: str,
) -> AsyncGenerator[str, None]:
    """Send a message to OpenClaw and yield streamed response chunks.

    Connects to the OpenClaw gateway WebSocket, sends the user message
    with the given session key, and yields response text as it arrives.
    """
    settings = get_settings()
    ws_url = settings.openclaw_ws_url

    try:
        async with websockets.connect(ws_url) as ws:
            # Send the message payload
            payload = {
                "action": "message",
                "token": settings.openclaw_gateway_token,
                "agent_id": settings.openclaw_agent_id,
                "session_key": session_key,
                "message": message,
            }
            await ws.send(json.dumps(payload))

            # Stream the response
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    # Plain text chunk
                    yield raw
                    continue

                msg_type = data.get("type", "")

                if msg_type == "error":
                    error_msg = data.get("message", "Unknown gateway error")
                    logger.error("Gateway error: %s", error_msg)
                    yield f"\n\n*Error: {error_msg}*"
                    return

                if msg_type == "done":
                    return

                if msg_type == "chunk" or "content" in data:
                    content = data.get("content", data.get("text", ""))
                    if content:
                        yield content

                if msg_type == "message":
                    content = data.get("content", data.get("text", ""))
                    if content:
                        yield content
                    return

    except ConnectionClosed as e:
        logger.error("Gateway WebSocket closed unexpectedly: %s", e)
        raise GatewayError("Connection to OpenClaw closed unexpectedly") from e
    except (OSError, InvalidStatusCode) as e:
        logger.error("Failed to connect to gateway: %s", e)
        raise GatewayError(f"Cannot reach OpenClaw gateway: {e}") from e


async def get_session_history(session_key: str) -> list[dict]:
    """Retrieve conversation history from OpenClaw for a session key.

    Returns a list of message dicts with 'role' and 'content' keys.
    """
    settings = get_settings()
    ws_url = settings.openclaw_ws_url

    try:
        async with websockets.connect(ws_url) as ws:
            payload = {
                "action": "sessions_history",
                "token": settings.openclaw_gateway_token,
                "agent_id": settings.openclaw_agent_id,
                "session_key": session_key,
            }
            await ws.send(json.dumps(payload))

            # Collect the full response
            messages = []
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type", "")

                if msg_type == "error":
                    logger.error("History error: %s", data.get("message"))
                    return []

                if msg_type == "done":
                    break

                if msg_type == "history" or "messages" in data:
                    raw_messages = data.get("messages", data.get("history", []))
                    for m in raw_messages:
                        messages.append({
                            "role": m.get("role", "assistant"),
                            "content": m.get("content", ""),
                        })
                    break

            return messages

    except (ConnectionClosed, OSError, InvalidStatusCode) as e:
        logger.error("Failed to fetch session history: %s", e)
        return []


async def generate_title(session_key: str, first_message: str) -> Optional[str]:
    """Ask OpenClaw to generate a short conversation title.

    Sends a special prompt to generate a concise title based on the
    first message exchange. Returns None on failure.
    """
    settings = get_settings()
    ws_url = settings.openclaw_ws_url

    title_prompt = (
        f"Generate a very short title (max 6 words) for a conversation that started "
        f"with this message: \"{first_message[:200]}\". "
        f"Reply with ONLY the title, no quotes, no explanation."
    )

    try:
        async with websockets.connect(ws_url) as ws:
            payload = {
                "action": "message",
                "token": settings.openclaw_gateway_token,
                "agent_id": settings.openclaw_agent_id,
                "session_key": f"{session_key}-title",
                "message": title_prompt,
            }
            await ws.send(json.dumps(payload))

            title_parts = []
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    title_parts.append(raw)
                    continue

                msg_type = data.get("type", "")
                if msg_type == "done":
                    break
                if msg_type == "error":
                    return None
                content = data.get("content", data.get("text", ""))
                if content:
                    title_parts.append(content)
                if msg_type == "message":
                    break

            title = "".join(title_parts).strip()
            # Sanitize: remove quotes, limit length
            title = title.strip("\"'").strip()
            if len(title) > 80:
                title = title[:77] + "..."
            return title if title else None

    except (ConnectionClosed, OSError, InvalidStatusCode) as e:
        logger.error("Failed to generate title: %s", e)
        return None
