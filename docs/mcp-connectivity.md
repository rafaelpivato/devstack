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
