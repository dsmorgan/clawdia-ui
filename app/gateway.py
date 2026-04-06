"""OpenClaw Gateway v3 protocol client with Ed25519 device pairing.

Implements the WebSocket RPC protocol for communicating with the OpenClaw
gateway, including challenge-response device authentication, sessions.send
with streaming events, chat.history, and title generation.
"""

import json
import uuid
import asyncio
import hashlib
import logging
from pathlib import Path
from typing import AsyncGenerator, Optional

import websockets
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    InvalidStatusCode,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from cryptography.hazmat.primitives import serialization
import base64

from app.config import get_settings

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 3
CONNECT_TIMEOUT = 10


class GatewayError(Exception):
    """Raised when the gateway returns an error or is unreachable."""
    pass


class PairingRequiredError(GatewayError):
    """Raised when the device needs to be approved via openclaw devices approve."""
    pass


# --- Device identity management ---

_device_key: Optional[Ed25519PrivateKey] = None
_device_id: Optional[str] = None
_device_token: Optional[str] = None


def _get_device_dir() -> Path:
    """Get the directory for storing device identity files."""
    settings = get_settings()
    return Path(settings.database_path).parent


def _get_device_key() -> tuple[Ed25519PrivateKey, str, str]:
    """Get or generate the Ed25519 device key pair.

    Returns (private_key, device_id, public_key_b64).
    Key is persisted to disk so the device identity survives restarts.
    """
    global _device_key, _device_id

    if _device_key and _device_id:
        pub_bytes = _device_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return _device_key, _device_id, base64.b64encode(pub_bytes).decode()

    device_dir = _get_device_dir()
    key_path = device_dir / "device_key.pem"

    if key_path.exists():
        pem_data = key_path.read_bytes()
        _device_key = serialization.load_pem_private_key(pem_data, password=None)
    else:
        _device_key = Ed25519PrivateKey.generate()
        pem_data = _device_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        device_dir.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(pem_data)
        print(f"[GW] Generated new device key: {key_path}", flush=True)

    # Derive device ID: SHA-256(raw_public_key_bytes).hex — must match server's derivation
    pub_bytes = _device_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    _device_id = hashlib.sha256(pub_bytes).hexdigest()
    pub_b64 = base64.b64encode(pub_bytes).decode()

    return _device_key, _device_id, pub_b64


def _load_device_token() -> Optional[str]:
    """Load persisted device token from disk."""
    global _device_token
    if _device_token:
        return _device_token
    token_path = _get_device_dir() / "device_token.txt"
    if token_path.exists():
        _device_token = token_path.read_text().strip()
        return _device_token
    return None


def _save_device_token(token: str) -> None:
    """Persist device token to disk."""
    global _device_token
    _device_token = token
    token_path = _get_device_dir() / "device_token.txt"
    token_path.write_text(token)
    print(f"[GW] Device token saved", flush=True)


# --- Protocol helpers ---

def _make_req(method: str, params: dict) -> dict:
    """Build an RPC request frame."""
    return {
        "type": "req",
        "id": uuid.uuid4().hex[:12],
        "method": method,
        "params": params,
    }


def _build_signature_payload(
    device_id: str,
    client_id: str,
    client_mode: str,
    role: str,
    scopes: list[str],
    signed_at: int,
    token: Optional[str],
    nonce: str,
) -> str:
    """Build the v2 pipe-delimited signature payload."""
    scopes_str = ",".join(scopes)
    token_str = token if token else "null"
    return f"v2|{device_id}|{client_id}|{client_mode}|{role}|{scopes_str}|{signed_at}|{token_str}|{nonce}"


def _sign_challenge(
    private_key: Ed25519PrivateKey,
    device_id: str,
    client_id: str,
    client_mode: str,
    role: str,
    scopes: list[str],
    signed_at: int,
    token: Optional[str],
    nonce: str,
) -> str:
    """Sign the v2 device auth payload with our Ed25519 key."""
    payload = _build_signature_payload(
        device_id, client_id, client_mode, role, scopes, signed_at, token, nonce
    )
    signature = private_key.sign(payload.encode("utf-8"))
    return base64.b64encode(signature).decode()


async def _connect_and_auth(ws) -> None:
    """Perform the v3 connect handshake with device identity.

    1. Wait for connect.challenge event
    2. Sign the challenge with our Ed25519 key
    3. Send connect request with device identity + token auth
    4. Handle hello-ok or pairing-required response
    """
    settings = get_settings()
    private_key, device_id, pub_b64 = _get_device_key()
    device_token = _load_device_token()

    # Step 1: Receive the challenge event
    raw = await asyncio.wait_for(ws.recv(), timeout=CONNECT_TIMEOUT)
    challenge = json.loads(raw)

    if challenge.get("type") != "event" or challenge.get("event") != "connect.challenge":
        raise GatewayError(f"Expected connect.challenge, got: {challenge.get('event', challenge.get('type'))}")

    nonce = challenge["payload"]["nonce"]
    ts = challenge["payload"]["ts"]

    # Step 2: Build connect params and sign
    client_id = "cli"
    client_mode = "cli"
    role = "operator"
    scopes = ["operator.read", "operator.write", "operator.admin"]

    # Token field in signature: server reconstructs using auth.token ?? auth.deviceToken ?? null
    # So we must sign with the gateway token (since that's what we send in auth.token)
    sig_token = settings.openclaw_gateway_token

    signature = _sign_challenge(
        private_key, device_id, client_id, client_mode,
        role, scopes, ts, sig_token, nonce
    )

    # Step 3: Build connect request
    connect_params = {
        "minProtocol": PROTOCOL_VERSION,
        "maxProtocol": PROTOCOL_VERSION,
        "client": {
            "id": client_id,
            "version": "2026.3.28",
            "platform": "linux",
            "mode": client_mode,
        },
        "role": role,
        "scopes": scopes,
        "auth": {"token": settings.openclaw_gateway_token},
        "device": {
            "id": device_id,
            "publicKey": pub_b64,
            "signature": signature,
            "signedAt": ts,
            "nonce": nonce,
        },
    }

    # If we have a device token from a previous pairing, include it
    if device_token:
        connect_params["auth"]["deviceToken"] = device_token

    connect_req = _make_req("connect", connect_params)
    print(f"[GW] connect: device={device_id}, hasDeviceToken={bool(device_token)}", flush=True)
    await ws.send(json.dumps(connect_req))

    # Step 4: Wait for response — skip any intervening events
    res = None
    for _ in range(10):
        raw = await asyncio.wait_for(ws.recv(), timeout=CONNECT_TIMEOUT)
        frame = json.loads(raw)
        if frame.get("type") == "res":
            res = frame
            break

    if res is None:
        raise GatewayError("No connect response received from gateway")

    if not res.get("ok"):
        error = res.get("error", {})
        msg = error.get("message", "Connect failed") if isinstance(error, dict) else str(error)
        code = error.get("code", "") if isinstance(error, dict) else ""

        if "pairing" in msg.lower() or "pairing" in str(code).lower():
            print(f"[GW] PAIRING REQUIRED — run on your OpenClaw host:", flush=True)
            print(f"[GW]   docker compose exec openclaw openclaw devices approve --latest", flush=True)
            raise PairingRequiredError(
                "Device pairing required. Run on your OpenClaw host: "
                "docker compose exec openclaw openclaw devices approve --latest"
            )
        raise GatewayError(f"Gateway connect rejected: {msg}")

    # Success — check for device token in response
    payload = res.get("payload", {})
    auth_info = payload.get("auth", {})
    new_token = auth_info.get("deviceToken")
    if new_token:
        _save_device_token(new_token)

    scopes = auth_info.get("scopes", [])
    print(f"[GW] connected: protocol=v{payload.get('protocol', '?')}, scopes={scopes}", flush=True)



# --- Public API ---

async def send_message(
    session_key: str,
    message: str,
) -> AsyncGenerator[str, None]:
    """Send a message to OpenClaw and yield streamed response chunks."""
    settings = get_settings()
    ws_url = settings.openclaw_ws_url

    try:
        async with websockets.connect(ws_url, open_timeout=CONNECT_TIMEOUT) as ws:
            await _connect_and_auth(ws)

            # Create session without explicit agentId — let OpenClaw bind the default agent
            create_req = _make_req("sessions.create", {
                "key": session_key,
            })
            await ws.send(json.dumps(create_req))
            for _ in range(10):
                raw = await asyncio.wait_for(ws.recv(), timeout=CONNECT_TIMEOUT)
                frame = json.loads(raw)
                if frame.get("type") == "res" and frame.get("id") == create_req["id"]:
                    print(f"[GW] session create: ok={frame.get('ok')} payload={json.dumps(frame.get('payload', {}), default=str)[:200]}", flush=True)
                    break

            # Subscribe to message updates for this session
            sub_req = _make_req("sessions.messages.subscribe", {
                "key": session_key,
            })
            await ws.send(json.dumps(sub_req))
            for _ in range(10):
                raw = await asyncio.wait_for(ws.recv(), timeout=CONNECT_TIMEOUT)
                frame = json.loads(raw)
                if frame.get("type") == "res" and frame.get("id") == sub_req["id"]:
                    print(f"[GW] subscribe result: ok={frame.get('ok')} payload={json.dumps(frame.get('payload', {}), default=str)[:200]}", flush=True)
                    break

            # Now send the message
            req = _make_req("sessions.send", {
                "key": session_key,
                "message": message,
            })
            req_id = req["id"]
            print(f"[GW] sending sessions.send req_id={req_id} key={session_key}", flush=True)
            await ws.send(json.dumps(req))
            print(f"[GW] sessions.send dispatched", flush=True)

            got_response = False
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                frame_type = data.get("type")
                event_name = data.get("event", "")

                # RPC response to our sessions.send request
                if frame_type == "res" and data.get("id") == req_id:
                    if not data.get("ok"):
                        error = data.get("error", {})
                        msg = error.get("message", "Unknown error") if isinstance(error, dict) else str(error)
                        yield f"\n\n*Error: {msg}*"
                    got_response = True

                # Agent stream events
                if frame_type == "event" and data.get("event") == "agent":
                    payload = data.get("payload", {})
                    event_session = payload.get("sessionKey", "")
                    if not event_session.endswith(session_key):
                        continue
                    stream = payload.get("stream", "")
                    agent_data = payload.get("data", {})

                    if stream == "assistant":
                        delta = agent_data.get("delta", "")
                        if delta:
                            yield delta

                    elif stream == "lifecycle":
                        phase = agent_data.get("phase", "")
                        if phase == "start":
                            # Signal that the agent is working (subagent calls, tool use, etc.)
                            yield {"type": "status", "status": "working"}
                        elif phase == "end":
                            return
                        elif phase == "error":
                            error_text = agent_data.get("error", "Agent error")
                            yield f"\n\n*Error: {error_text}*"
                            return

                # Session message events — detect tool calls for status updates
                if frame_type == "event" and event_name == "session.message":
                    payload = data.get("payload", {})
                    if not payload.get("sessionKey", "").endswith(session_key):
                        continue
                    msg = payload.get("message", {})
                    if msg.get("role") == "assistant":
                        msg_content = msg.get("content", [])
                        if isinstance(msg_content, list):
                            for block in msg_content:
                                if isinstance(block, dict) and block.get("type") == "toolCall":
                                    tool_name = block.get("name", "")
                                    if tool_name:
                                        yield {"type": "status", "status": "tool", "tool": tool_name}

                # Chat final as a fallback completion signal
                if frame_type == "event" and data.get("event") == "chat":
                    payload = data.get("payload", {})
                    if not payload.get("sessionKey", "").endswith(session_key):
                        continue
                    if payload.get("state") == "final":
                        return

    except asyncio.TimeoutError:
        raise GatewayError("Gateway connection timed out")
    except ConnectionClosed as e:
        logger.error("Gateway WebSocket closed unexpectedly: %s", e)
        raise GatewayError("Connection to OpenClaw closed unexpectedly") from e
    except (OSError, InvalidStatusCode) as e:
        logger.error("Failed to connect to gateway: %s", e)
        raise GatewayError(f"Cannot reach OpenClaw gateway: {e}") from e


async def get_session_history(session_key: str) -> list[dict]:
    """Retrieve conversation history from OpenClaw for a session key."""
    settings = get_settings()
    ws_url = settings.openclaw_ws_url

    try:
        async with websockets.connect(ws_url, open_timeout=CONNECT_TIMEOUT) as ws:
            await _connect_and_auth(ws)

            req = _make_req("chat.history", {
                "sessionKey": session_key,
            })
            req_id = req["id"]
            await ws.send(json.dumps(req))

            async for raw in ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if data.get("type") == "res" and data.get("id") == req_id:
                    if not data.get("ok"):
                        error = data.get("error", {})
                        msg = error.get("message", "History fetch failed") if isinstance(error, dict) else str(error)
                        logger.error("History error: %s", msg)
                        return []

                    payload = data.get("payload", {})
                    raw_messages = payload.get("messages", payload.get("history", payload.get("transcript", [])))

                    # Debug: log raw transcript to understand structure
                    for i, m in enumerate(raw_messages):
                        oc = m.get("__openclaw", {})
                        print(f"[GW] history[{i}]: role={m.get('role')} oc={json.dumps(oc, default=str)[:200]} content_preview={json.dumps(m.get('content', ''), default=str)[:150]}", flush=True)

                    messages = []
                    seen = set()
                    for m in raw_messages:
                        role = m.get("role", "assistant")
                        raw_content = m.get("content", "")

                        # Skip tool results and system messages
                        if role in ("tool", "toolResult", "system"):
                            continue

                        # Skip internal/injected user messages (runtime context, subagent results)
                        # Real user messages have 'senderLabel'; injected ones have 'provenance'
                        if role == "user" and ("provenance" in m or "senderLabel" not in m):
                            continue

                        # Extract visible text from content blocks, skipping toolCall blocks
                        if isinstance(raw_content, list):
                            text_parts = [
                                block.get("text", "")
                                for block in raw_content
                                if isinstance(block, dict) and block.get("type") == "text"
                            ]
                            content = "".join(text_parts)
                        else:
                            content = str(raw_content) if raw_content else ""

                        if not content:
                            continue

                        mapped_role = "user" if role in ("user", "human") else "assistant"

                        # Deduplicate: OpenClaw's multi-channel delivery creates
                        # duplicate messages (assistant echo, user echo, re-delivery)
                        # with identical content but different seq/role
                        dedup_key = content.strip()
                        if dedup_key in seen:
                            continue
                        seen.add(dedup_key)

                        messages.append({
                            "role": mapped_role,
                            "content": content,
                        })
                    return messages

        return []

    except asyncio.TimeoutError:
        logger.error("Timed out fetching session history")
        return []
    except (ConnectionClosed, OSError, InvalidStatusCode) as e:
        logger.error("Failed to fetch session history: %s", e)
        return []


async def delete_session(session_key: str) -> bool:
    """Delete a session from OpenClaw. Returns True if successful."""
    settings = get_settings()
    ws_url = settings.openclaw_ws_url

    try:
        async with websockets.connect(ws_url, open_timeout=CONNECT_TIMEOUT) as ws:
            await _connect_and_auth(ws)

            req = _make_req("sessions.delete", {
                "key": session_key,
            })
            req_id = req["id"]
            await ws.send(json.dumps(req))

            async for raw in ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "res" and data.get("id") == req_id:
                    return data.get("ok", False)

        return False

    except (asyncio.TimeoutError, ConnectionClosed, OSError, InvalidStatusCode) as e:
        logger.error("Failed to delete session: %s", e)
        return False


async def generate_title(session_key: str, first_message: str) -> Optional[str]:
    """Ask OpenClaw to generate a short conversation title."""
    settings = get_settings()
    ws_url = settings.openclaw_ws_url

    title_prompt = (
        f"Generate a very short title (max 6 words) for a conversation that started "
        f"with this message: \"{first_message[:200]}\". "
        f"Reply with ONLY the title, no quotes, no explanation."
    )

    try:
        async with websockets.connect(ws_url, open_timeout=CONNECT_TIMEOUT) as ws:
            await _connect_and_auth(ws)

            title_key = f"{session_key}-title"
            create_req = _make_req("sessions.create", {"key": title_key})
            await ws.send(json.dumps(create_req))
            for _ in range(10):
                raw = await asyncio.wait_for(ws.recv(), timeout=CONNECT_TIMEOUT)
                frame = json.loads(raw)
                if frame.get("type") == "res" and frame.get("id") == create_req["id"]:
                    break

            req = _make_req("sessions.send", {
                "key": title_key,
                "message": title_prompt,
            })
            req_id = req["id"]
            await ws.send(json.dumps(req))

            title_parts = []
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                frame_type = data.get("type")

                if frame_type == "event" and data.get("event") == "chat":
                    payload = data.get("payload", {})
                    if not payload.get("sessionKey", "").endswith(f"{session_key}-title"):
                        continue

                    state = payload.get("state", "")
                    text = payload.get("text", "") or payload.get("message", "")

                    if state == "delta" and text:
                        title_parts.append(text)
                    elif state == "final":
                        if text:
                            title_parts.append(text)
                        break
                    elif state == "error":
                        return None

                if frame_type == "event" and data.get("event") == "chat.lifecycle":
                    payload = data.get("payload", {})
                    if payload.get("sessionKey", "").endswith(f"{session_key}-title"):
                        if payload.get("phase") in ("end", "error"):
                            break

                if frame_type == "res" and data.get("id") == req_id:
                    if not data.get("ok"):
                        return None

            title = "".join(title_parts).strip()
            title = title.strip("\"'").strip()
            if len(title) > 80:
                title = title[:77] + "..."
            return title if title else None

    except (asyncio.TimeoutError, ConnectionClosed, OSError, InvalidStatusCode) as e:
        logger.error("Failed to generate title: %s", e)
        return None
