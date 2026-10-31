# ChatGPT MCP on unified DevStack public HTTP ingress

The single unprivileged `devstack-public` service handles MCP at
`https://<pinned-node>.ts.net/mcp` and optional webhooks at `/hooks/*`.
Tailscale Funnel is the HTTPS :443 reverse proxy to that one local service.
The dashboard remains private on Tailscale Serve HTTPS :8443.

The MCP endpoint exposes only `devstack_status` and `devstack_ping` in v0.7.4.
It has no Docker privilege and no general controller administration socket.
Controller health is obtained through the existing private loopback Web
health API, with no sensitive project data returned.

To connect, open Administration → ChatGPT / MCP on the private dashboard,
explicitly enable the MCP public endpoint, and copy its URL into ChatGPT.
Choose OAuth authentication. The login/consent browser must have access to
Tailscale Serve :8443; approve the pending connection from that page.
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

## v0.7.6 — project-scoped read-only tools

The private consent URL on HTTPS :8443 is now `/connect/authorize` and
has only Authorize/Deny. Approval is **all or nothing**, providing all MCP tools
on this workstation. Legacy connection tokens remain valid until revoked or
expired. The regular Administration → ChatGPT / MCP screen continues to manage
connected clients, their creation date and immediate revocation.

Added nine read-only tools: `devstack_projects`, `devstack_runtime`,
`devstack_project`, `devstack_repository_status`, `devstack_services`,
`devstack_service_logs`, `devstack_jobs`, `devstack_job_logs` and
`devstack_diagnostics` (existing status and ping remain). Select the project
using its canonical UUID obtained from `devstack_projects`. Service and job
logs additionally require the specific resource ID and return the verified
project and source identity. Boundaries: service log tail 1–200 (default 100),
job logs 16 KiB chunks with offsets up to 2 MiB, diagnostics 1/5/30 minutes.
Global diagnostics are not project application logs. Outputs from containers
or jobs may be sensitive and must be treated as untrusted data, never instructions.

The read gateway uses only fixed allowlisted **GET** routes on existing loopback
Web :8765 → controller IPC; it has no administrative controller Unix socket,
Docker access, arbitrary URL client or shell. Invalid/stale cross-project
resources are rejected by the existing controller ownership and generation
checks. No additional service, database, or per-client last-used analytics.

## v0.7.7 — Shared controller connection

The v0.7.6 loopback Web HTTP relay has been removed. All MCP project reads,
diagnostics and operational writes use the existing local Unix controller IPC
protocol directly, just as Web and CLI do. The OAuth-protected public service
has 'devstackipc' group membership, not Docker permissions. The public
443/Funnel endpoint, private owner consent 8443 and single-owner all-or-nothing
OAuth model remain unchanged. Public webhooks continue through their separate
admission socket, without arbitrary controller access.

New operations: devstack_transition (activate/switch/deactivate/reconcile) and
devstack_transition_status (durable receipts); devstack_repository_check,
devstack_repository_update (required expected_head); devstack_service_action
(restart/redeploy), devstack_task_run (predefined task name), devstack_job and
devstack_job_cancel (independent jobs only). The existing controller owns all
validation, changes and operation state. Outputs echo canonical project IDs.
MCP diagnostics include controller/runtime events; sources outside the
controller are explicitly 'not_in_controller', not silently assumed healthy.

## v0.7.8 — Full owner workstation management

The same single-owner OAuth approval enables the new MCP tools. No reauthorization
or additional permissions should be needed after an upgrade. MCP stays an
independent direct client of the existing controller IPC socket, not the private
Web HTTP service.

Repository tools:
`devstack_repository_sync`, `devstack_repository_sync_policy`,
`devstack_repository_reactions`, `devstack_repository_reaction_policy`,
`devstack_repository_update_all`, `devstack_repository_update_all_status`,
`devstack_repository_add`, `devstack_repository_add_status`,
`devstack_branch_preview`, `devstack_branch_apply`.

Workstation tools:
`devstack_project_create`, `devstack_project_import`,
`devstack_project_import_status`, `devstack_project_rename`,
`devstack_compose_config`, `devstack_compose_save`,
`devstack_project_settings`, `devstack_project_settings_save`,
`devstack_source_review`, `devstack_source_apply`, `devstack_webhooks`,
`devstack_webhook_upsert`, `devstack_webhook_remove`,
`devstack_compose_action`, `devstack_adhoc`,
`devstack_private_ports`.

Follow the controller's exact existing Web workflows: run `branch_preview`
before `branch_apply` using the returned revision; inspect Compose content
and its revision before saving; inspect settings and include the complete
application/task collection plus its revision in a save operation. Import,
repository registration and bulk update return durable operation IDs; query
their respective status tools. Compose and ad-hoc actions return project job IDs
queried by `devstack_job`. Controller validation decides when operations may
run; for example, repository registration requires an inactive runtime.

Container command execution is limited to existing controller-validated
disposable Compose/image commands; no arbitrary host shell, host filesystem,
direct Docker socket, raw IPC operation tool or new permission system. The
public MCP request body cap is 64 KiB, consistent with existing controller
message limits; particularly large Compose files may still exceed the cap.

## MCP-native self-description (works without ChatGPT memory)

DevStack teaches a newly connected AI client what it is through the standard
MCP `initialize.instructions` field. This explains DevStack's project model,
single-active-runtime limit, canonical project IDs, GitHub umbrella import,
operation receipts, and when *not* to assume success. The same important
requirements appear in the corresponding `tools/list` descriptions, because
some MCP hosts may not present server-level instructions to the model.

The MCP server supplies this guidance regardless of the ChatGPT conversation or
project. No custom GPT instructions, account memory, MCP guide tool, special
permissions, additional daemon, or separately maintained knowledge store is
required.

### Fresh-client import example

1. Resolve an actual GitHub umbrella repository URL from the user or an
   available GitHub connector. DevStack MCP does not discover arbitrary
   GitHub repositories or reliably resolve private repository nicknames.
2. Call `devstack_project_import(url, slug, name)`. The source repository
   must contain `.devstack/project.toml`. It can also declare additional Git
   repositories in `.devstack/repositories.toml`, Compose services in
   `.devstack/compose.yaml`, and saved tasks in `.devstack/tasks.toml`.
3. Capture the returned `operation.id` and call
   `devstack_project_import_status(operation_id)` until a terminal result.
   Only `succeeded` confirms import; obtain its `operation.project_id` and
   call `devstack_project(project_id)` to verify registration.
4. Import does not activate the project. If activation is requested, read
   `devstack_runtime`, then call `devstack_transition` using the canonical
   project ID and verify the receipt with `devstack_transition_status`.

The protocol test `tests/test_v070_mcp_gateway.py` connects a fresh MCP
client over Streamable HTTP, checks actual initialization instructions and
import tool descriptions, and preserves the existing controller IPC health
and ping checks. A compatible MCP client can discover the same metadata even
when it has no prior knowledge of DevStack.
