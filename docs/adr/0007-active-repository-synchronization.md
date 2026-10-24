# ADR-0007: Active-project Git synchronization and opt-in polling

- **Status:** Proposed for review in issue #89 (implementation branch)
- **Baseline:** v0.6.6 / schema 10; backward-compatible migration required
- **Scope:** Repository bytes and remote state; #90 owns targeted Compose reactions

## Decision

The existing controller remains the only Git execution authority. Repository
identity is always the pair of immutable project and repository UUIDs, resolved
against the registry and workspace. Web and SSH CLI remain thin IPC clients.

Manual fast-forward is allowed when the runtime is **inactive** (subject to the
existing owned-resource safety check) or **active for the same project**. It is
forbidden during transitions, error recovery or when a different project owns
the runtime. The coordinator's process-shared source-write lock serializes
updates with activation/switch/deactivation, branch changes and other source
mutations. Active updates neither change the activation generation nor perform
Compose operations. Git working trees may be bind-mounted and application
processes may observe changed files; deployment/reload policy belongs to #90.

A checked-out branch must track `origin`, be clean (including untracked files),
have an expected local HEAD, and be fast-forwardable after fetching. Detached,
dirty, diverged, missing-upstream and unexpectedly changed worktrees fail closed.
Never hard reset, force checkout, merge non-fast-forward, install remote hooks,
auto-import `.devstack` configuration, or roll back an already-completed
fast-forward. The operator inspects an interrupted checkout before retry.

## Polling and persistence

Each registered repository defaults to **manual**. Explicit `check` policy
fetches remote refs and records behind/ahead without changing the worktree.
Explicit `update` policy additionally fast-forwards only when all manual
safety gates pass. A single bounded poller runs in the existing controller
daemon; it is not an additional service or public webhook. Fixed 5-minute
interval for this first iteration; no domain or inbound access required.

Persist per-repository policy, last check/update, safe result codes and bounded
operation receipts in controller SQLite. Schema migration from v0.6.6 must
preserve all registered projects and runtime state. A crash marks in-flight
work interrupted; it is never silently replayed. Refuse automatic updates for
unresolved interrupted work until a deliberate manual action. Polling skips
unavailable runtime phases and foreign active projects without activating
anything. Never store Git stderr, auth tokens or remote URLs in receipts.

## Out of scope and verification

No service restart/rebuild, webhook triggers, branch auto-switches, targeted
application reactions, or GitHub user credential provisioning (#90 separate).
Tests must exercise real local Git repositories, active same-project success,
foreign-project rejection, dirty/diverged/missing upstream, idempotent refresh,
serialized transitions, interrupted operation recovery, and absence of Compose
side effects. Physical-host verification needs an active project receiving a
new remote commit without deactivation; not claimed from CI alone.
