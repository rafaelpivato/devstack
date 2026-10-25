# ADR-0009: v0.7 MCP connectivity-first experiment

- **Status:** Proposed — owner review required
- **Baseline:** v0.6.8; supersedes no accepted ADR
- **Scope:** A real remote MCP handshake, tool discovery, and harmless read/write.

## Decision

Use the maintained Python MCP SDK to provide a single localhost-only
Streamable HTTP MCP server on port 8768, with explicit external Host allowlisting.
The first tools are `hello_read` and `hello_write`. State exists exclusively
in the unprivileged MCP process's memory; all clients share this toy state.
Only these two tools are registered. No controller socket, Docker socket,
DevStack configuration, filesystem or private metadata is exposed.

The MCP server is shipped as a separate executable in the normal DevStack
wheel but is not started or exposed automatically. The operator explicitly
starts it without membership in `devstackipc`/`docker`. For public HTTPS
without a purchased domain, manually use a separate Tailscale Funnel port
10000, avoiding private Serve 443 and existing webhook Funnel 8443.
No authentication is offered while exposed tools remain harmless.

## Explicitly deferred

Real observability or changes to projects/jobs/Compose, access to privileged
controller IPC, secrets/logs, authentication/authorization, multi-client
isolation, and public directory publication. Before adding any actual controller
tools, require another threat-model review and authenticated access; the
unauthenticated prototype must not be extended in place to privileged actions.

## Acceptance gate

Automated tests exercise the SDK's live HTTP transport (not a fake endpoint),
tool discovery, a write-then-read, unknown tool rejection, hostname validation
and no credential or project exposure. Manual acceptance additionally requires
a real cloud ChatGPT plugin connection, not just a local Inspector call.
ChatGPT account capability and HTTPS port 10000 reachability are empirical
dependencies, not assumptions. No assistant-initiated merge or release.
