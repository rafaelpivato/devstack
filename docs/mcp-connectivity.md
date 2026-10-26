# DevStack 0.7 MCP — managed ChatGPT connectivity

The private DevStack Administration → ChatGPT / MCP screen now provisions
a bounded opt-in connectivity session. A verified release's existing root
updater invokes the established CLI installation hook to register a disabled
MCP gateway systemd unit and a private marker→root one-shot watcher.
No remote SSH commands are necessary for routine enable/disable.

## Browser steps

1. Install v0.7.0 through the normal DevStack update UI (the existing inactive
   project/update safety gate still applies).
2. In **Administration → ChatGPT / MCP**, verify the gateway/Funnel status.
3. Acknowledge that this first two-tool MCP server is public and unauthenticated,
   and choose **Enable MCP connection**. The browser queues one literal action
   in a private directory; the privileged watcher starts only the dedicated
   gateway and Funnel HTTPS 10000, without touching the private dashboard 443
   or optional webhook Funnel 8443. The browser polls status automatically.
4. Copy the displayed public URL ending in `/mcp`.
5. On ChatGPT web, add a personal custom MCP server, choose **No authentication**,
   and provide the copied URL. Try `devstack_status` and
   `devstack_ping(value="connected")`, then `devstack_status` again to verify
   `last_ping` is `connected`. Also test tool visibility in ChatGPT mobile.
6. Use **Disable public connection** after testing. Disabling removes only the
   verified MCP route on HTTPS port 10000 and stops/disables its gateway.
   It does not change private Serve or webhook settings.

First-time Tailscale Funnel permission may require approval on the Tailscale
admin site. ChatGPT may also reject nonstandard HTTPS port 10000 even though
Tailscale supports it; that remains a manual acceptance test, not a guarantee.

## Boundaries

- No new controller daemon or Unix IPC permissions for MCP. The unauthenticated
  gateway calls **only** local Web `/healthz`, which already reaches controller
  IPC; no project names, logs, repository config or shell commands are exposed.
- `devstack_status` returns installed gateway wheel version, coarse live
  controller state, and the last short ping. `devstack_ping(value)` returns
  a matching pong and stores value **only in process memory**, shared with
  other public callers and lost on restart. Never send secrets.
- A root helper accepts only `enable` and `disable` request markers; it
  validates pinned Tailscale identity and exact known Serve/Funnel routes
  before making changes. On unknown routes, it fails closed.
- The persistent background route and systemd gateway restart automatically
  across host restarts **until disabled**. The user controls exposure in the UI.
- The first release does not provide OAuth, per-user permissions or a centrally
  hosted plugin. Do not add privileged real DevStack tools without authentication.

## Troubleshooting

The browser displays queued state and bounded result codes from the host
broker. If Tailscale reports `host_command_failed`, check device connectivity,
Funnel eligibility/policy and whether the tailnet's HTTPS feature is enabled.
If you have manually edited Tailscale Serve/Funnel, an unknown-route error
means DevStack refused to replace foreign configuration. MCP lifecycle, health and failure events appear in Administration →
DevStack diagnostics, which offers 1/5/30-minute views and a copyable
snapshot. Do not request SSH for routine MCP/Tailscale troubleshooting.
Only use host recovery when the Web diagnostic surface itself cannot function.

## Development

Unit tests cover the fixed action request contract and the verified
Serve/Funnel configuration state machine using mocked Tailscale/systemd commands.
End-to-end CI exercises the real MCP → Web → controller IPC health path.
Real public ingress still needs a host with Tailscale and a supported ChatGPT
client; CI does not assert cloud ChatGPT connectivity.

Official Tailscale documentation: https://tailscale.com/docs/reference/tailscale-cli/funnel

The root diagnostic collector samples only the two dedicated MCP systemd
units once per minute, emitting fixed-schema events, without exposing arbitrary
journal entries or request parameters. SDK/internal crashes appear as generic
MCP process-error events; explicit gateway and Funnel failures use safe codes.

The new diagnostic unit is a root-owned **read-only journal collector**, limited
to the MCP gateway and MCP apply services. It refreshes its safe JSON snapshot
every minute. If the collector fails or becomes stale, source coverage becomes
`mcp=unavailable` rather than quietly hiding failures. The browser never
requests arbitrary host logs and never needs shell access to collect them.

## v0.7.1 field hotfix

The v0.7.0 Enable MCP action could fail before Funnel with
`request_failed`, accompanied by `web=unavailable` diagnostic coverage.
The MCP request is now queued in the existing, verified private Web
RuntimeDirectory (`/run/devstack-update-request/mcp-action`), separate from
the updater's `trigger` marker. The installer replaces the fixed systemd.path
unit to watch the new marker. No operator SSH or manual permission repair.

The Web diagnostics ring prefers its persistent state and falls back to
`/run/devstack-update-request/web-diagnostics.sqlite3` when unavailable.
The UI reports `partial (volatile fallback)` when its persistent storage is
unavailable. The MCP Web screen identifies queue-readiness and reports
permission, read-only storage or capacity errors instead of a generic
request failure. The marker path and underlying operator checks are covered
by automated host permission tests.
