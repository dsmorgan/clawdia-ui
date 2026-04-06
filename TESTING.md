# TESTING.md

Manual test procedures for Clawdia UI, with emphasis on the OIDC flow which is the trickiest part to get right in a homelab deployment.

## Prerequisites

- Clawdia UI running (Docker or local)
- Authentik instance accessible from both browser and Clawdia container
- OpenClaw gateway running and reachable from Clawdia container
- A test user account in Authentik

## 1. OIDC Authentication Flow

This is the most common failure point. Test each step independently.

### 1.1 Discovery endpoint

Verify Authentik's OIDC discovery is reachable **from the container**:

```bash
# From the container
docker-compose exec clawdia-ui curl -s \
  https://auth.yourdomain.com/application/o/clawdia-ui/.well-known/openid-configuration \
  | python -m json.tool
```

**Expected**: JSON with `authorization_endpoint`, `token_endpoint`, `userinfo_endpoint`.

**Common failures**:
- DNS resolution fails → add Authentik's IP to container's `/etc/hosts` or use IP directly
- TLS cert not trusted → if using self-signed certs, you may need to add the CA to the container
- Wrong issuer URL → ensure the slug matches (`/application/o/<slug>/`)

### 1.2 Login redirect

1. Open `http://localhost:8000` in a browser (not logged in)
2. **Expected**: Redirect to Authentik login page
3. Check the URL — it should contain `client_id=<your-client-id>` and `redirect_uri=https://your-domain/auth/callback`

**Common failures**:
- `invalid_client` → client ID doesn't match
- `redirect_uri_mismatch` → the callback URL in Authentik doesn't match exactly. Check for trailing slashes, http vs https

### 1.3 Callback and session

1. Log in through Authentik
2. **Expected**: Redirect back to Clawdia at `/` with the chat interface
3. Check browser cookies — should see `clawdia_session`

**Common failures**:
- Callback returns 400 → check `OIDC_CLIENT_SECRET` matches Authentik
- Session not persisting → check `SECRET_KEY` is set in `.env`
- Cookie not set → if behind HTTPS proxy, ensure `DEBUG=false` so `Secure` flag matches
- Cookie not set (dev) → if testing over HTTP, set `DEBUG=true` to disable `Secure` flag

### 1.4 Logout

1. Click "Sign out" in the sidebar
2. **Expected**: Redirect to `/logged-out` page showing the sign-in button
3. Click "Sign in with Authentik" → should redirect to Authentik login

### 1.5 Cross-user isolation

1. Log in as User A, create a conversation
2. Log out, log in as User B
3. **Expected**: User B sees no conversations (empty sidebar)
4. User B cannot access User A's conversation by URL (`/chat/<id>` should redirect to `/`)

## 2. Chat Functionality

### 2.1 New conversation

1. Click "New Conversation" or navigate to `/`
2. Type a message and press Enter (or tap Send)
3. **Expected**:
   - User message appears right-aligned in blue bubble
   - Typing indicator (three dots) appears
   - AI response streams in left-aligned in dark bubble
   - Conversation appears in sidebar
   - Title auto-generates after ~2 seconds

### 2.2 Quick actions

1. On a new conversation (no messages), check for quick action pills above input
2. Tap a pill (e.g., "Who's streaming now?")
3. **Expected**: Input field fills with the prompt text, does NOT auto-send
4. Press Enter to send

### 2.3 Conversation history

1. Create a conversation with a few messages
2. Navigate away (click "New Conversation")
3. Click the conversation in the sidebar
4. **Expected**: Previous messages load from OpenClaw history

### 2.4 WebSocket reconnect

1. Start a conversation
2. Open browser DevTools → Network → WS tab
3. Close the WebSocket connection manually
4. **Expected**: Reconnects automatically within 2 seconds (check console log)

### 2.5 Multi-turn conversation

1. Send multiple messages in sequence
2. **Expected**: Context is maintained — AI references previous messages

## 3. Mobile Layout

### 3.1 Sidebar drawer

1. Open on a mobile viewport (or resize browser to <1024px)
2. Tap the hamburger icon (top-left)
3. **Expected**: Sidebar slides in from left with dark overlay
4. Tap overlay → sidebar closes
5. Tap a conversation → navigates and sidebar closes

### 3.2 Touch targets

1. On mobile, verify all interactive elements are at least 44px tall:
   - Quick action pills
   - Send button
   - Sidebar conversation items
   - New Conversation button

### 3.3 Input bar

1. On mobile, verify the input bar is sticky at the bottom
2. Open the keyboard (or resize to simulate) → input should remain visible
3. Type a multi-line message → textarea should expand (up to 4 lines)

## 4. Edge Cases

### 4.1 OpenClaw gateway unreachable

1. Stop the OpenClaw gateway (or set wrong `OPENCLAW_HOST`)
2. Send a message
3. **Expected**: Error message appears in chat (not a crash)

### 4.2 Missing quick actions file

1. Remove or rename `config/quick_actions.yaml`
2. Reload the page
3. **Expected**: No quick action buttons shown, no error

### 4.3 Invalid quick actions YAML

1. Put invalid YAML in `config/quick_actions.yaml` (e.g., unbalanced quotes)
2. Reload the page
3. **Expected**: No quick action buttons shown, no error

### 4.4 Long messages

1. Send a very long message (1000+ characters)
2. **Expected**: Message wraps properly, no horizontal scroll, copy button works

### 4.5 Concurrent sessions

1. Open Clawdia in two browser tabs
2. Send messages in both
3. **Expected**: Both work independently, sidebar updates reflect in both after refresh

## 5. Deployment Checklist

Before going live behind Pangolin:

- [ ] `SECRET_KEY` is a strong random string (not the default)
- [ ] `DEBUG=false`
- [ ] `OIDC_*` variables point to production Authentik
- [ ] `OPENCLAW_GATEWAY_TOKEN` is set correctly
- [ ] Pangolin is configured to proxy WebSocket connections
- [ ] HTTPS is enforced (Pangolin handles TLS termination)
- [ ] Authentik redirect URI uses `https://` and matches exactly
- [ ] Cookie `Secure` flag works with HTTPS (requires `DEBUG=false`)
- [ ] Container can reach both Authentik and OpenClaw gateway
- [ ] `data/` directory is writable by the container user
- [ ] `config/quick_actions.yaml` is mounted read-only
