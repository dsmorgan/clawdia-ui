# Clawdia UI

A clean, family-friendly chat interface for interacting with an [OpenClaw](https://github.com/dsmorgan/openclaw) AI agent focused on home media server management. Built for non-technical family members who want to check on Jellyfin, manage downloads, and troubleshoot media server issues through natural conversation.

## Overview

- **Chat interface** with streaming AI responses via WebSocket
- **OIDC authentication** via Authentik (or any OpenID Connect provider)
- **Session proxy** — messages live in OpenClaw, not in this app
- **Quick action buttons** — configurable one-tap prompts for common tasks
- **Mobile-first** dark glass-morphism design
- **Conversation history** — browse and continue past conversations
- **Auto-generated titles** for new conversations

## Architecture

| Component | Technology |
|-----------|------------|
| Backend | Python 3.12, FastAPI, Uvicorn |
| Templates | Jinja2 server-side rendering |
| Frontend | Tailwind CSS (CDN), HTMX |
| Auth | authlib (OIDC against Authentik) |
| Database | SQLite via aiosqlite |
| Real-time | WebSockets (browser ↔ app ↔ OpenClaw) |
| Deployment | Docker / Podman with docker-compose |

Clawdia UI is a **thin proxy + session index layer**. It does not store chat messages. All messages live in OpenClaw. The only database responsibility is tracking which OpenClaw session keys belong to which users.

## Quick Start

### Prerequisites

- Docker and docker-compose (or Podman equivalents)
- An Authentik instance with an OIDC application configured (see [Authentik Setup](#authentik-oidc-setup))
- A running OpenClaw gateway instance

### 1. Clone and configure

```bash
git clone https://github.com/dsmorgan/clawdia-ui.git
cd clawdia-ui
cp .env.example .env
# Edit .env with your values
```

### 2. Configure quick actions (optional)

Edit `config/quick_actions.yaml` to customize the quick action buttons. Changes take effect without restart.

### 3. Build and run

```bash
docker-compose up -d --build
```

The app will be available at `http://localhost:8000`.

### 4. Access behind Pangolin

If running behind Pangolin reverse proxy, configure Pangolin to forward to port 8000 with WebSocket support enabled. Ensure the `ALLOWED_HOSTS` environment variable includes your domain.

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SECRET_KEY` | Yes | — | Random string for session encryption |
| `DEBUG` | No | `false` | Enable debug mode |
| `ALLOWED_HOSTS` | No | `*` | Comma-separated allowed hostnames |
| `DATABASE_PATH` | No | `/app/data/clawdia.db` | SQLite database file path |
| `QUICK_ACTIONS_PATH` | No | `/app/config/quick_actions.yaml` | Quick actions config file path |
| `OPENCLAW_HOST` | Yes | `10.11.24.52` | OpenClaw gateway IP/hostname |
| `OPENCLAW_PORT` | No | `18789` | OpenClaw gateway port |
| `OPENCLAW_GATEWAY_TOKEN` | Yes | — | Authentication token for OpenClaw gateway |
| `OPENCLAW_AGENT_ID` | No | `main` | OpenClaw agent identifier |
| `OIDC_CLIENT_ID` | Yes | — | Authentik OIDC client ID |
| `OIDC_CLIENT_SECRET` | Yes | — | Authentik OIDC client secret |
| `OIDC_ISSUER` | Yes | — | Authentik OIDC issuer URL |
| `LOGIN_REDIRECT_URL` | No | `/` | Redirect after successful login |
| `LOGOUT_REDIRECT_URL` | No | `/logged-out` | Redirect after logout |

## Authentik OIDC Setup

1. In Authentik Admin, go to **Applications → Providers** and create a new **OAuth2/OpenID Provider**:
   - **Name**: `clawdia-ui`
   - **Authorization flow**: Select your default authorization flow
   - **Client type**: Confidential
   - **Redirect URIs**: `https://your-clawdia-domain.com/auth/callback`
   - **Scopes**: `openid`, `profile`, `email`

2. Create an **Application**:
   - **Name**: `Clawdia UI`
   - **Slug**: `clawdia-ui`
   - **Provider**: Select the provider created above

3. Note the **Client ID** and **Client Secret** from the provider and add them to your `.env`.

4. Set `OIDC_ISSUER` to: `https://your-authentik-domain.com/application/o/clawdia-ui/`

## Quick Actions

Quick actions are configurable buttons that pre-fill the chat input with common prompts. They are loaded from a YAML file mounted into the container.

### Format

```yaml
quick_actions:
  - label: "Who's streaming now?"
    prompt: "Check Jellyfin and tell me who is currently streaming."

  - label: "Is Jellyfin running?"
    prompt: "Check the Jellyfin container status."
```

- **label**: Button text shown in the UI
- **prompt**: Text that fills the input field when tapped (does not auto-send)

The file is re-read on each page load, so edits take effect without restart. If the file is missing or empty, no quick action buttons are shown.

## Development

### Local development (without Docker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env

uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Database

The SQLite database is created automatically on first run. It contains a single table:

```sql
CREATE TABLE conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_sub TEXT NOT NULL,
    user_name TEXT NOT NULL,
    session_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT 'New conversation',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

No migration tool is needed — the schema is applied on startup via `CREATE TABLE IF NOT EXISTS`.

### Common tasks

```bash
# Rebuild container
docker-compose up -d --build

# View logs
docker-compose logs -f clawdia-ui

# Shell into container
docker-compose exec clawdia-ui bash

# Reset database (delete and restart)
rm data/clawdia.db && docker-compose restart clawdia-ui
```

## Project Structure

```
clawdia-ui/
├── Dockerfile                  # Python 3.12-slim container
├── docker-compose.yml          # Single service, bind mounts
├── requirements.txt            # Pinned Python dependencies
├── .env.example                # Environment variable template
├── config/
│   └── quick_actions.yaml      # Quick action button definitions
├── data/                       # SQLite database (bind mount)
├── app/
│   ├── main.py                 # FastAPI app, routes, WebSocket proxy
│   ├── auth.py                 # OIDC login/callback/logout via authlib
│   ├── config.py               # Settings from env + YAML loader
│   ├── database.py             # aiosqlite schema and queries
│   ├── gateway.py              # OpenClaw WebSocket client
│   ├── models.py               # Pydantic models
│   └── templates/
│       ├── base.html           # Layout with glass-morphism CSS
│       ├── chat.html           # Main chat interface
│       ├── sidebar.html        # Conversation list partial
│       ├── messages.html       # Message history partial
│       └── logged_out.html     # Post-logout landing page
├── CLAUDE.md                   # AI development guide
├── TESTING.md                  # Manual test procedures
└── README.md                   # This file
```

## Troubleshooting

### OIDC callback fails with "invalid_client"
- Verify `OIDC_CLIENT_ID` and `OIDC_CLIENT_SECRET` match exactly what Authentik shows
- Ensure the redirect URI in Authentik matches your domain exactly: `https://your-domain/auth/callback`

### WebSocket connection drops
- Ensure Pangolin is configured to proxy WebSocket connections (upgrade headers)
- Check that `OPENCLAW_HOST` and `OPENCLAW_PORT` are reachable from the container
- Review logs: `docker-compose logs -f clawdia-ui`

### Quick actions not showing
- Verify `config/quick_actions.yaml` exists and is valid YAML
- Check the volume mount in `docker-compose.yml` maps to the correct host path

### "Not authenticated" after login
- Verify `SECRET_KEY` is set — session cookies require it for encryption
- If behind a reverse proxy, ensure cookies are forwarded correctly (check `Secure` flag vs HTTP)
- Set `DEBUG=true` temporarily to use HTTP-only cookies during testing
