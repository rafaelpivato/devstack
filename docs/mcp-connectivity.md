# DevStack 0.7 MCP connectivity — first live ChatGPT test

This is a two-tool, **opt-in public connectivity experiment** packaged with
DevStack. It provides a real health check via the existing
`MCP gateway -> web /healthz -> controller IPC` path. It does **not** give
unauthenticated callers controller IPC credentials. No project data, repository
information, logs, Docker commands, configuration or secrets are exposed.

The stable MCP tool names are:
- `devstack_status`: installed gateway version, controller health
  (`connected`, `unavailable`, or `recovery_required`), last diagnostic ping.
- `devstack_ping(value)`: store a printable 1-80 character diagnostic payload
  in volatile memory and return `pong: <value>`. This is a harmless MCP write;
  `devstack_status` reads the latest value back. It doesn't write to disk or
  mutate the running project.

**The status tool is real; the ping write is intentionally non-privileged.**
These are stable long-term names: future status fields can expand while ping
remains a simple connectivity diagnostic.

## Run on a deployed v0.7.0 host

Upgrade using the existing approved DevStack release/updater process. No public
listener is automatically enabled by the v0.7.0 package.

1. Find the exact `<machine>.<tailnet>.ts.net` Tailscale hostname.
2. Start MCP as an isolated account **without `devstackipc` or `docker` group
   membership**. It needs only local HTTP access to the existing DevStack Web
   listener (`127.0.0.1:8765/healthz`):

   ```sh
   sudo systemd-run --unit=devstack-mcp \
     --property=DynamicUser=yes --property=NoNewPrivileges=yes \
     --property=ProtectSystem=strict --property=ProtectHome=yes \
     --setenv=DEVSTACK_MCP_PUBLIC_HOST=<machine>.<tailnet>.ts.net \
     /opt/devstack/current/venv/bin/devstack-mcp
   ```

3. Inspect the service and verify the private health path:

   ```sh
   curl -i http://127.0.0.1:8765/healthz
   sudo systemctl status devstack-mcp
   sudo journalctl -u devstack-mcp -n 40 --no-pager
   ```

4. Enable a **separate** Tailscale Funnel listener, leaving private Serve port
   443 and any webhook Funnel at port 8443 unchanged:

   ```sh
   sudo tailscale funnel --bg --https=10000 http://127.0.0.1:8768
   sudo tailscale funnel status
   ```

   MCP URL: `https://<machine>.<tailnet>.ts.net:10000/mcp`. A public HTTPS URL
   is **not authentication**, and anyone with the URL can ping and read status.
   The tool intentionally reveals only the gateway version, a coarse controller
   health state, and a shared diagnostic test value. Don't send secrets as ping
   values.

   Whether ChatGPT can connect to port 10000 is to be verified in the actual
   client; if blocked, consider another isolated HTTPS path or Secure MCP Tunnel.
   Never expose the private dashboard for this experiment.

5. In ChatGPT web, use the available custom MCP/plugin creation interface.
   Enter the full HTTPS `/mcp` URL and choose **No authentication**. The setup
   UI and eligibility vary by account. After installation, try:

   - "Using DevStack, check its installed version and controller status."
   - "Use DevStack ping with value connected, then read DevStack status."
   - Confirm that `controller` is `connected`, `pong` is `connected`, and
     `last_ping` is `connected`.
   - If practical, stop `devstack-control` temporarily and verify that
     `devstack_status` reports `unavailable`; restart immediately and confirm
     recovery. Do not interrupt active development workloads just for this test.

   Also check if the installed custom plugin is selectable from the mobile app.
   Record actual client availability separately from server-side test success.

6. Disable public access when the test ends:

   ```sh
   sudo tailscale funnel --https=10000 off
   sudo systemctl stop devstack-mcp
   ```

## Boundaries

- Streamable HTTP, process-local diagnostics, and one fixed, loopback health URL;
  the MCP client cannot choose a destination URL or controller operation.
- No authorization, persistence, credentials, project list, private logs,
  Docker access, controller Unix-socket group membership, or filesystem writes.
- Public clients share the last ping value. It is reset when the gateway
  process restarts, and other callers can overwrite it while Funnel is on.
- `version` is the installed MCP gateway wheel version, not proof that every
  DevStack service is running the same wheel. Controller health reports a
  live response from the existing Web->IPC path.
- Do not add real read/write controller operations to this unauthenticated
  prototype. Revisit authentication before exposing privileged tools.

Official docs: https://developers.openai.com/api/docs/guides/custom-mcp-server
and https://tailscale.com/docs/reference/tailscale-cli/funnel
