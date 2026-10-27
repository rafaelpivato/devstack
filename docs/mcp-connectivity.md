# DevStack MCP — ChatGPT connectivity

The intended endpoint after the migration described in
[public ingress](public-ingress.md) is:

- **Public:** `https://<pinned-node>.ts.net/mcp` (Tailscale Funnel HTTPS 443).
- **Private dashboard:** `https://<pinned-node>.ts.net:8443/` (Tailscale Serve).
- **Webhooks:** public `https://<pinned-node>.ts.net/hooks/<name>`.

v0.7.2 and older used *private* dashboard 443, optional webhook 8443,
and public MCP 10000. Migrating is **not** simply changing one flag:
public Funnel 443 is forbidden until the private dashboard 443 handler
has been safely removed. The dedicated Web-console migration action
prepares and verifies private Serve 8443 first, using a bounded,
fail-closed root-owned watcher. It never resets unknown Tailscale state.

## Use from ChatGPT

Open **Administration → ChatGPT / MCP** on your private dashboard.
Complete the guarded port migration if prompted, then opt in to
**Enable MCP connection**. Copy the public HTTPS URL. In the ChatGPT Web
custom MCP setup, enter that URL and select **No authentication** for
this deliberately constrained test. Verify `devstack_status`, then
`devstack_ping(value="connected")`, then another status to confirm
`last_ping`. The gateway remains bound to localhost 8768.

Without independent authentication, the publicly exposed tools are
limited to coarse controller health and an ephemeral diagnostic ping.
There is no project, filesystem, log, Docker or controller execution
authority and there must not be one without a new authenticated design.

## Diagnostics and recovery

The private Web UI provides all routine lifecycle and error handling;
there is no routine SSH prerequisite. Operations are literal bounded
commands through the existing verified root watcher. Administration →
DevStack diagnostics provides 1/5/30-minute redacted, copyable events.

A configured Funnel route **does not prove** off-tailnet TLS, MCP
initialization, tool discovery or ChatGPT client access. Test externally
with MCP `initialize` and `tools/list` and record the HTTP/TLS outcome.
Issue #101 tracks earlier mixed results from Tailscale client 1.104.2;
simply switching ports does not establish that the upstream behavior
is resolved. If the new external port fails, keep private 8443 intact
and use Web-only diagnostics to identify TLS versus MCP failures.

Tailscale strips mounted prefixes with `--set-path`, so the proxy
target includes `/mcp` to reconstruct the expected backend path.

Official reference: https://tailscale.com/docs/reference/tailscale-cli/funnel
