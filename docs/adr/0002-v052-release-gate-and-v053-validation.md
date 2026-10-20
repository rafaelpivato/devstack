# ADR-0002: Ship v0.5.2 on automated gates; validate deployed behavior in v0.5.3

**Status:** Accepted by the project owner, 2026-10-20; recorded before v0.5.2 review/merge.
**Supersedes:** Only the *timing of physical-host acceptance* in [ADR-0001](0001-multiple-projects-single-runtime.md) and [the v0.5.2 acceptance contract](../v0.5.2-acceptance-plan.md). Architectural invariants INV-01–INV-12 and behavior requirements AC-01–AC-17 remain binding.

## Context and decision

The integration candidate for v0.5.2 has completed the architectural implementation, v1 clean-break cleanup, version bump and release notes. A clean-host test through the published stable installer cannot be executed until a release exists. The owner explicitly chooses **v0.5.2 as the first automated-test-verified deployable release** and **v0.5.3 as the next SSH/CLI and deployment-stabilization release**.

### Requirements to publish v0.5.2 (still blocking)

1. Frozen feature scope and security boundaries unchanged; no known critical security/data-loss defect or failed mandatory test left unresolved.
2. Green **full Python tests, two-project real Docker A → B → A with job/webhook/volume isolation, and fresh Ubuntu systemd web-versus-controller permission/restart tests** on the final integration commit. The `main` release workflow must run its own mandatory Docker/systemd and unit gates.
3. Verified clean-install-only behavior; legacy v0.5.1 state is rejected, not migrated. No autoactivation, cross-project leaks or privilege escalation is acceptable.
4. Human review of [release PR #40](https://github.com/rafaelpivato/devstack/pull/40), explicit Ready-for-review transition and deliberate **human merge to `main`**. No automated merge or tag creation from integration.

### Deferred operational verification — NOT considered passed

- AC-14: real `scripts/install.sh` install **after** v0.5.2 is published on disposable clean Ubuntu; verify UI, two projects and A → B → A.
- AC-09: crash/reboot mid-transition and fail-closed recovery on an actual host.
- AC-15: actual newer-release update/rollback on a test installation, beyond existing updater unit/Ubuntu CI.
- Positive direct TCP/HTTP access from a second tailnet device to a Compose service deliberately bound to the Tailscale IP.
- Additional manual mobile/browser usability checks.

Record results, fixes and regression tests for **v0.5.3**, without altering the published v0.5.2 tag. Do not equate automated regression coverage with real-host verification. Test on disposable infrastructure before relying on the new release for important project state and back up worktrees/volumes. Unexpected critical safety bugs found **before** merge still block v0.5.2.

### Next release: v0.5.3

Target SSH-accessible native DevStack CLI operations (not a browser shell), plus deployment validation and confirmed fixes. The CLI design will be scoped separately; v0.6 is not blocked on GPT-facing observability.

## Consequences

v0.5.2 is an initial deployable release with green automated verification, **not a claim of fully field-tested production readiness**. Actual hardware/tailnet evidence remains open post-release work. Mandatory release CI is unchanged. This decision changes acceptance timing only; it does not authorize skipping failing CI or merging without human review.
