# CLAUDE.md

Technical reference for AI-assisted development of Clawdia UI.

## Overview

Clawdia UI is a FastAPI web application that provides a family-friendly chat interface for the OpenClaw AI agent. It handles home media server management tasks (Jellyfin, downloads, media requests) through natural conversation. Non-technical family members are the primary users.

## Project Structure

```
app/
├── main.py          # FastAPI app, all routes, WebSocket proxy, lifespan
├── auth.py          # OIDC via authlib against Authentik
├── config.py        # pydantic-settings config + YAML quick actions loader
├── database.py      # aiosqlite connection, schema, all queries
├── gateway.py       # OpenClaw WebSocket client (send, history, title gen)
├── models.py        # Pydantic models (Conversation, UserInfo, etc.)
└── templates/       # Jinja2 templates
    ├── base.html    # Layout, CSS, Tailwind config
    ├── chat.html    # Main chat page with JS WebSocket client
    ├── sidebar.html # HTMX partial for conversation list
    ├── messages.html # HTMX partial for message history
    └── logged_out.html
```

## Key Architectural Decisions

### Thin Proxy Pattern

Clawdia UI does **not** store messages. It is a session index + WebSocket proxy:

1. User sends message via browser WebSocket → Clawdia backend
2. Backend opens WebSocket to OpenClaw gateway, forwards message with session key
3. OpenClaw streams response back through the proxy to the browser
4. Only the session key mapping (user ↔ conversation) is stored in SQLite

This means message search, history, and persistence are OpenClaw's responsibility.

### Session Key Scheme

Each conversation gets a unique OpenClaw session key:
```
clawdia-<username>-<uuid4_short>
```
Example: `clawdia-dave-a3f9`

- Generated in `main.py:create_new_conversation()`
- Username is cleaned (alphanumeric + hyphens, max 20 chars)
- UUID short is 4 hex chars from uuid4
- Stored in `conversations.session_key` (UNIQUE constraint)
- Passed to OpenClaw on every WebSocket interaction

### Authentication Flow

1. Unauthenticated request hits middleware → redirect to `/auth/login`
2. `/auth/login` → authlib redirects to Authentik OIDC authorize endpoint
3. Authentik authenticates user → redirects to `/auth/callback`
4. `/auth/callback` → exchanges code for token, extracts userinfo, stores in session
5. Session cookie (`clawdia_session`) is HttpOnly, Secure (when not DEBUG)

### WebSocket Architecture

Two separate WebSocket connections per active chat:

1. **Browser ↔ Clawdia** (`/ws/chat`): Authenticated via session cookie. Handles message routing, conversation validation, and response streaming.
2. **Clawdia ↔ OpenClaw** (gateway): Authenticated via `OPENCLAW_GATEWAY_TOKEN`. Created per-message (not persistent).

The browser WebSocket stays open for the session. The gateway WebSocket is opened per-message in `gateway.py:send_message()` and closed when the response completes.

### Auto-Title Generation

After the first AI response in a new conversation:
1. `main.py` fires an `asyncio.create_task()` background task
2. Task calls `gateway.py:generate_title()` which sends a title-generation prompt to OpenClaw using a separate session key (`{key}-title`)
3. Generated title is saved to the database
4. Frontend refreshes sidebar after 1.5s delay to pick up the new title

### Quick Actions

Loaded from YAML file at `QUICK_ACTIONS_PATH`. Re-read on every page load (no caching) so edits take effect without restart. If the file is missing or malformed, an empty list is returned — the UI hides the quick actions bar.

Quick action buttons pre-fill the input field but do **not** auto-send.

## Database

Single SQLite table. No migrations — schema is `CREATE TABLE IF NOT EXISTS` on startup.

```sql
conversations (id, user_sub, user_name, session_key, title, created_at, updated_at)
```

All queries in `database.py` are scoped by `user_sub` — users cannot see each other's conversations.

## Security Model

- All routes except `/logged-out` and `/auth/*` require authentication (middleware in `main.py`)
- `OPENCLAW_GATEWAY_TOKEN` never leaves the backend — not in HTML, JS, or API responses
- Every database query filters by `user_sub` for row-level access control
- Session cookie is HttpOnly + Secure (Secure disabled when `DEBUG=true`)
- WebSocket auth checks session cookie on connect; closes with 4001 if missing

## Development Commands

```bash
# Local dev
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Docker
docker-compose up -d --build
docker-compose logs -f clawdia-ui

# Reset database
rm data/clawdia.db && docker-compose restart clawdia-ui
```

## Common Modifications

### Adding a new page route

1. Add route in `main.py` with `@app.get()` decorator
2. Create template in `app/templates/`
3. Route must check `get_current_user(request)` — middleware redirects HTML but API routes need explicit checks

### Adding a new HTMX partial

1. Create template fragment in `app/templates/`
2. Add endpoint in `main.py` returning `TemplateResponse`
3. Reference with `hx-get` in the parent template

### Modifying the OpenClaw gateway protocol

All gateway communication is in `gateway.py`. The three methods:
- `send_message()` — streaming message proxy
- `get_session_history()` — fetch conversation transcript
- `generate_title()` — one-shot title generation

The WebSocket message format sent to OpenClaw:
```json
{
    "action": "message|sessions_history",
    "token": "<gateway_token>",
    "agent_id": "<agent_id>",
    "session_key": "<session_key>",
    "message": "<user_message>"  // only for "message" action
}
```

Responses are JSON with `type` field: `chunk`, `done`, `error`, `message`, `history`.

### Changing the UI theme

All CSS is in `base.html` `<style>` block. Key values:
- Background: `#0a0a0f`
- Glass: `rgba(255,255,255,0.05)` with `backdrop-filter: blur(20px)`
- Accent (user/interactive): `#4f8ef7`
- AI accent: `#8b5cf6`
- Tailwind config is inline in `base.html` `<script>` block

## Environment Variables

See `.env.example` for all variables. Critical ones:
- `SECRET_KEY` — must be set for session security
- `OPENCLAW_GATEWAY_TOKEN` — must match your OpenClaw gateway config
- `OIDC_*` — must match your Authentik OIDC application

## Notes for Future Development

- The gateway WebSocket protocol may need adjustment based on OpenClaw version — test with a raw WS client first
- If message volume grows, consider connection pooling for the OpenClaw WebSocket
- The sidebar refresh is a simple fetch-and-replace; for real-time multi-tab sync, consider SSE
- No rate limiting currently — add if exposed to untrusted networks
