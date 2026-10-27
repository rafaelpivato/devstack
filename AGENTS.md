# DevStack engineering and operations rules

## Default user experience: Web-first, no routine SSH

The primary operator uses the DevStack mobile-first Web console. **Never
require SSH for ordinary installation upgrades, configuration, start/stop,
service enablement, health checks, runtime changes, failure diagnosis or
log collection.** This applies to future DevStack features and optional
services, including MCP and Tailscale Funnel. The existing `dev` SSH CLI
is a *convenience*, not a prerequisite.

**SSH is an emergency escape hatch only when DevStack's Web UI or the
diagnostics/recovery pathway is itself unavailable or broken.** Do not
recommend remote sessions as the ordinary troubleshooting path.

### Every new feature/service PR must provide

1. Web-console controls for routine lifecycle and setup; any privileged
   operation must use an explicitly bounded, independently validated broker,
   not a general shell/host-command interface.
2. Meaningful inline errors and observable status: unavailable, queued,
   running, failed, recovered, and a clear next action.
3. **Copyable, bounded, time-filtered diagnostics** in Administration →
   DevStack diagnostics (1, 5, 30 minutes), including startup, requests,
   errors and recoveries for any new daemon/gateway/ingress component.
   Make copying work on narrow mobile screens.
4. Per-source coverage and partial/unavailable diagnostics indicators: no
   events must **not** mean a source was silently unreachable.
5. Safe diagnostic content. Do not copy arbitrary host journal text, secrets,
   raw commands, tokens, request payloads, Docker sockets, project data,
   or privileged error traces into the dashboard or a public MCP endpoint.
6. Automated tests for Web status/CSRF, operational failures, log visibility,
   copying/text formatting, and real-host integration where feasible.
7. Instructions that begin with Web UI steps. Recovery CLI instructions are
   secondary and explicitly marked as exceptional.

## MCP v0.7 constraint

An unauthenticated public MCP test may expose only the bounded status and
ephemeral ping tools. Network migration #102 moves the private dashboard to Serve :8443 and
public MCP/webhook integration paths to Funnel :443. **Never publish 443
while any private dashboard handler remains there**. The root-owned release reconciler and Web-controlled public opt-in broker
must reject unknown states and preserve private access across **automatic**
cutover. Do not add interactive one-time migration actions. Privileged MCP project or host
operations require independent authentication and controller-side authority
before they may be enabled.
