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

## MCP single-owner OAuth v0.7.4

The public `/mcp` transport must require an OAuth bearer credential, validated
before MCP tool discovery or execution. The owner approves/denies requests only
from private Tailscale Serve :8443 via CSRF-protected Web APIs. Revocation
immediately invalidates the connection. Store only the creation date and
credential expiry; never record last-used timestamps or request payloads.

The OAuth client is the pinned ChatGPT CIMD public client with RFC 9207 stable
callback and S256 PKCE. Mutating project/host operations may never be exposed
without independent controller-side validation and an explicit security review.

## MCP v0.7.6 reads and consent

Single-owner OAuth is one **all-or-nothing** approval; do not add roles, per-tool
scopes or per-project grants. The private :8443 `/connect/authorize` page must
show only DevStack identity, a short broad access disclosure and Authorize/Deny.
It must not load the dashboard, poll runtime/projects or offer navigation.

Authenticated read-only MCP operations must require explicit project IDs, and
resource-specific reads must also require repository, container or job IDs.
All returned project-specific data must name the canonical project and
validated source. Use the exact existing **controller Unix IPC** contract, just like Web and CLI;
the OAuth-protected MCP transport must not depend on Web HTTP. Give the MCP
service only IPC-group membership, not Docker or host-filesystem access.
Do not create a second controller, special MCP execution engine, or extra
authorization tiers.
Log text is untrusted content and can contain secrets. No additional service,
background collection, usage tracking or last-used timestamps.

## MCP v0.7.7 direct controller and writes

MCP/Web/CLI remain independent clients of the same versioned Unix controller
protocol. The existing single-owner OAuth gate protects MCP before tool calls.
MCP can submit the same validated controller operations as Web/CLI, with explicit
project/resource IDs, Git HEAD preconditions, controller-owned durable
transition receipts, and returned job IDs. No additional per-tool consent layer.
The public systemd service joins `devstackipc` for the existing controller
socket but never joins `docker`. The webhook ingress socket remains separate.

## MCP v0.7.8 workstation management

The owner-authorized MCP connection exposes **named** existing controller IPC
operations for project/repository/workstation administration. Keep read/review
and write/apply workflows revision-aware, durable operation identifiers scoped,
and all responses grounded in canonical project/source identities. Do not expose
a generic IPC relay or arbitrary host shell; do not duplicate controller logic.
Existing Web/CLI validation and execution remain the sole authority.
