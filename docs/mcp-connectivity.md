# ChatGPT MCP on unified DevStack public HTTP ingress

The single unprivileged `devstack-public` service handles MCP at
`https://<pinned-node>.ts.net/mcp` and optional webhooks at `/hooks/*`.
Tailscale Funnel is the HTTPS :443 reverse proxy to that one local service.
The dashboard remains private on Tailscale Serve HTTPS :8443.

The public MCP test exposes only `devstack_status` and `devstack_ping`.
It has no Docker privilege and no general controller administration socket.
Controller health is obtained through the existing private loopback Web
health API, with no sensitive project data returned.

To connect, open Administration → ChatGPT / MCP on your private dashboard,
explicitly enable the MCP public endpoint, and copy the URL into ChatGPT
custom MCP setup, choosing No authentication for this constrained test.
Use `devstack_status`, `devstack_ping`, then `devstack_status` to
confirm the ephemeral last ping.

New v0.7.3 installations use private :8443 directly. v0.7.2 updates
reconfigure Tailscale automatically from the verified wheel's postinstall
hook, and you simply reopen the dashboard at :8443. There is no one-time
migration control.

External TLS, MCP initialize and tools/list must be tested off-tailnet;
Funnel configuration status does not guarantee successful ChatGPT access.
Issue #101 tracks earlier remote Funnel incompatibility separately.

## v0.7.4 — single-owner OAuth approval

MCP requires OAuth 2.1 authorization-code + S256 PKCE; anonymous status/ping
calls are no longer accepted on public HTTPS :443. The only allowed client
is ChatGPT's stable CIMD identity (`https://chatgpt.com/oauth/client.json`)
with stable callback (`https://chatgpt.com/connector_platform_oauth_redirect`).
DevStack uses its pinned Tailscale HTTPS hostname for authorization-server
metadata, and advertises the protected resource `https://<node>.ts.net/mcp`.

On first connection, ChatGPT opens the public `/oauth/authorize` endpoint, which
sends the browser to the **private** :8443 Administration → ChatGPT / MCP screen.
The owner must be connected to Tailscale. Approving there redirects back to
ChatGPT with a short-lived, single-use authorization code. The refresh token
rotates, while the access token lasts one hour. Both are stored as SHA-256
hashes in a root-provisioned, group-restricted local JSON store; only
creation date appears in the private connected-client list. Revoke immediately
invalidates access and refresh tokens. No last-used tracking or external IdP.

An upgrade from v0.7.3 requires reauthorizing the ChatGPT connector (existing
anonymous MCP connections no longer work). Private dashboard and public
webhooks remain on :8443 and :443 respectively. No controller mutations, logs,
project listings or new tools are exposed. Status still checks the actual
controller through existing local Web /healthz → controller IPC; ping remains
ephemeral. Off-tailnet ChatGPT OAuth acceptance requires live manual testing.
