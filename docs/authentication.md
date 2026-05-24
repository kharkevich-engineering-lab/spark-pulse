# Authentication

Spark Pulse supports two authentication modes: **no authentication** (all access, no login) and **SSO via OIDC** (requires login through an identity provider).

| Mode | Access | Login Required | Use Case |
|---|---|---|---|
| **No auth** | Everyone | No | Local development, trusted environments |
| **OIDC SSO** | Authenticated users only | Yes | Production, shared access, multi-user |

---

## Mode 1: No Authentication

When authentication is disabled, **all routes are public**. The login/logout buttons are hidden from the UI and no API route requires authentication.

### Configuration

Ensure `auth_enabled` is `false` (or unset, as `false` is the default):

```yaml
# config.yaml or ~/.config/spark-pulse/settings.json
auth_enabled: false
```

That is all — no additional keys are needed. The app starts immediately without any login UI.

### Environment Variable Override

You can also disable auth via environment variable:

```bash
SPARK_PULSE_AUTH_ENABLED=false spark-pulse start
```

### What's Protected

- **Nothing** — all API and UI routes are accessible without a session.

---

## Mode 2: OIDC SSO Authentication

When authentication is enabled, Spark Pulse redirects users through your OIDC provider's login page. The login/logout buttons appear in the top-right corner of the UI, and **all routes except health, auth, static assets, and `/api/config` require a valid session**.

### Prerequisites

1. An OIDC-compliant identity provider (Keycloak, Authelia, Cognito, Google, GitHub, etc.)
2. An OIDC application/registration in your provider with:
   - **Redirect URI:** `https://your-domain/auth/callback` (adjust for your deployment)
   - **Scopes:** `openid profile email`
   - **Response type:** `code` (authorization code flow)

### Configuration

#### Step 1: Enable auth and provide provider metadata

Set `auth_enabled: true` and fill in the OIDC provider details:

```yaml
# ~/.config/spark-pulse/settings.json
{
    "auth_enabled": true,
    "oidc_provider_url": "https://keycloak.example.com/realms/myrealm",
    "oidc_client_id": "spark-pulse"
}
```

#### Step 2: Set the client secret

For security, the client secret is stored separately in `~/.config/spark-pulse/secrets.json` (mode `0600`):

```json
{
    "oidc_client_secret": "your-secret-here"
}
```

You can also set it via the UI Settings page under the Authentication section.

#### Step 3: Start the server

```bash
SPARK_PULSE_AUTH_ENABLED=true spark-pulse start
```

The server discovers the OIDC provider automatically on login (via `/.well-known/openid-configuration`) to find the authorization and token endpoints.

### How Login Works

```
User clicks "Login"
  → Redirected to OIDC provider
  → User authenticates
  → Provider redirects back to /auth/callback with authorization code
  → Backend exchanges code for tokens
  → User info is fetched from /userinfo endpoint
  → A session token (cookie) is created and set
  → User is redirected to /
```

### How Logout Works

```
User clicks "Logout"
  → POST /auth/logout invalidates the session token
  → Cookie is deleted
  → User is redirected to /login page
```

### Protected vs. Public Routes

| Public (no auth required) | Protected (valid session required) |
|---|---|
| `/` | All UI pages |
| `/login` | All API endpoints under `/api/*` |
| `/health` | WebSocket/SSE streams |
| `/auth/*` | Static UI assets served by the backend |
| `/api/config` | |
| `/assets/*` | |
| `/static/*` | |

### Session Management

- Sessions are stored **in-memory** on the server (the `_active_tokens` dictionary).
- A `token` cookie (httponly, samesite=lax) is issued to the browser.
- The cookie lifetime matches the OIDC access token `expires_in` value.
- On server restart, all sessions are lost (users must re-authenticate).
- For production deployments with many users, consider migrating the token store to Redis.

### Configuration Reference

| Key | Type | Default | Description |
|---|---|---|---|
| `auth_enabled` | bool | `false` | Enable OIDC authentication |
| `oidc_provider_url` | string | *(empty)* | OIDC provider URL (e.g. `https://keycloak.example.com/realms/myrealm`) |
| `oidc_client_id` | string | *(empty)* | OIDC client ID from your identity provider |
| `oidc_client_secret` | string | *(empty)* | OIDC client secret — stored in `secrets.json` |

### Environment Variable Overrides

| Environment Variable | Config Key |
|---|---|
| `SPARK_PULSE_AUTH_ENABLED` | `auth_enabled` |

---

## Switching Between Modes

You can switch between no-auth and SSO modes at any time by changing the configuration and restarting the server. The app re-reads configuration on each startup — no additional cache clearing is needed.

**No auth → SSO:**
1. Set `auth_enabled: true` and provide OIDC details
2. Set the client secret in `secrets.json`
3. Restart the server

**SSO → No auth:**
1. Set `auth_enabled: false`
2. Restart the server

All existing session tokens are invalidated on restart regardless of the mode.
