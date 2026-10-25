# DevStack 0.7 MCP Hello World — public connectivity test

This is intentionally **not yet a DevStack remote controller**. It is an
unauthenticated, two-tool MCP service packaged with DevStack. No administrative
socket, project metadata, Docker, Git, secrets, disk writes, or runtime state are
accessible from these tools. `hello_write` changes only a process-local
string (reset on restart). Do not provide sensitive text to that public tool.

## Run on a deployed v0.7.0 host

Upgrade DevStack using its existing reviewed release/updater workflow.
This preview does not auto-enable MCP or change the private dashboard.

1. Obtain your exact `<machine>.<tailnet>.ts.net` Tailscale hostname.
2. Start the MCP process **under a user without devstackipc or docker group
   membership**, ideally as a transient restricted systemd unit:

   ```sh
   sudo systemd-run --unit=devstack-mcp-hello \
     --property=DynamicUser=yes --property=NoNewPrivileges=yes \
     --property=ProtectSystem=strict --property=ProtectHome=yes \
     --setenv=DEVSTACK_MCP_PUBLIC_HOST=<machine>.<tailnet>.ts.net \
     /opt/devstack/current/venv/bin/devstack-mcp
   ```

3. Inspect the MCP service before enabling public access:

   ```sh
   sudo systemctl status devstack-mcp-hello
   sudo journalctl -u devstack-mcp-hello -n 40 --no-pager
   ```

4. Enable a *separate* Tailscale Funnel listener, leaving existing private
   port 443 and any webhook Funnel at port 8443 unchanged:

   ```sh
   sudo tailscale funnel --bg --https=10000 http://127.0.0.1:8768
   sudo tailscale funnel status
   ```

   Public MCP URL: `https://<machine>.<tailnet>.ts.net:10000/mcp`.
   Port 10000 reachability from ChatGPT is an **acceptance test**, not a
   guaranteed capability. If it fails despite successful MCP Inspector access,
   evaluate a dedicated path on an allowed HTTPS port or Secure MCP Tunnel.
   Never publish the private dashboard just to make MCP work.

5. On ChatGPT web, open **Plugins > + > Add custom MCP server**. Supply the
   complete HTTPS `/mcp` URL, select **No authentication**, acknowledge the
   risk warning, and create the personal plugin. Install it, select it with
   `@` in a new chat, and run:
   - `Use DevStack Hello World hello_write with value "connected"`
   - `Use DevStack Hello World hello_read and tell me the value`

   Verify the read returns `connected`. Custom-plugin creation availability
   depends on your account/workspace; if the control isn't shown, first try
   MCP Inspector or a different supported MCP client. Tests must record which
   ChatGPT account and client surfaces actually worked, without secrets.

6. Stop public access when the test ends:

   ```sh
   sudo tailscale funnel --https=10000 off
   sudo systemctl stop devstack-mcp-hello
   ```

## Limitations and non-goals

- One MCP server process, Streamable HTTP with stateless JSON replies.
- MCP SDK's explicit Host/Origin allowlist requires the actual Funnel hostname.
- In-memory hello state is shared across **all callers**, because there is no
  authentication. Anyone can set/read it while Funnel is enabled.
- No auto-installed public ingress, OAuth, state persistence, per-user access,
  multiple workers, controller IPC, container commands, or real project tools.
- This tests protocol, remote reachability, ChatGPT tool discovery, and safe
  read/write calls only. It **does not** validate privileged controller access.

Official docs: https://developers.openai.com/api/docs/guides/custom-mcp-server
and https://tailscale.com/docs/reference/tailscale-cli/funnel
