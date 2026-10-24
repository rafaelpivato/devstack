# ADR-0006: Source-only Git tags and verified CI release wheels

**Status:** Proposed for #88, v0.6.5 migration bridge and v0.6.6 implementation.

## Context

Before v0.6.5 the standalone privileged updater cloned published Git tags and installed Python from checkout. This forced tracked compiled Vite assets into the source repository. An immediate wheel-only release would be unusable by existing standalone updaters, which are installed outside the versioned app and are not automatically overwritten.

## Decision

Two consecutive 0.6.x releases are required.

**v0.6.5** is a one-time source-compatible bridge with the existing compiled frontend retained in Git. The existing updater can install it. The operator must run the root-only, already tested `devstack.cli.install --refresh-updater` command against the trusted v0.6.5 release to atomically install the new standalone updater. It retains a backup of the older updater and supports source-tag installation through v0.6.5 for recovery.

**v0.6.6** is the first wheel-only release. Git tags contain Python/TypeScript/CSS source, locked dependencies, tests and build definitions but not generated frontend assets. On PR, the locked Node build, complete fail-closed npm audit, TypeScript/React tests and wheel validation run without writing Git. CI packages the React HTML entry, Vite manifest and referenced JS/CSS into a wheel and validates wheel RECORD checksums. Python, Compose A→B→A and fresh Ubuntu systemd/Unix socket tests execute against a built wheel. Release publication is gated on the verification jobs, publication is main-only, and the release job consumes the frontend job's exact wheel via GitHub Actions artifact. It attaches that wheel plus `SHA256SUMS` to a **draft** release, checks the asset set, then publishes it.

The standalone updater itself is staged from the canonical `scripts/updater.py` into a verified wheel resource. Its hash/content is checked against the canonical file by CI, and the root-only `--refresh-updater` command can load that resource only from the selected release venv. This retains independently updatable root-owned tooling after Git source checkouts disappear. The operation remains explicit, never an API side effect.

GitHub Releases may remain editable at the owner's discretion; immutable-release settings are optional. The updater validates every downloaded release with exact tag, uploaded asset metadata, size, GitHub SHA-256 digest and release checksum. Draft, incomplete or mismatched releases never qualify. Automated release CI must not overwrite an existing tag or artifacts, though a repository administrator can choose to replace assets later.

## Threat model and limitations

Artifact checks include strict stable tag/version, release published state, exact expected artifact names, approved HTTPS URLs and redirect hostnames, bounded compressed and uncompressed sizes, SHA256SUMS, mandatory GitHub API artifact `digest: sha256:`, ZIP member safety, Python distribution metadata and React manifest contents. SHA256SUMS fetched from the *same publisher* is corruption protection, **not independent cryptographic proof of publisher identity**. The independent provenance record is the GitHub Actions attestation created by `actions/attest@v4`; operators can use `gh attestation verify devstack-*.whl --repo rafaelpivato/devstack`. The stdlib-only host updater does **not yet verify attestation signatures**, and therefore trusts GitHub HTTPS/API permissions and asset digest metadata. A repository administrator could mutate an existing release and update its checksums and digests; this is an accepted tradeoff for a personal project. A future separate decision is required to move provenance enforcement on-host if a threat model demands it.

Release assets are **not locked against later changes** if GitHub immutability is disabled. The publication workflow is append-only by policy, not by enforced repository settings. Tags must be strictly managed and branch protection is advisable. Publication must never be re-run against an existing partial tag; inspect GitHub release/tag status and manually recover before rerunning. Rebuilding identical source is aided by pinned Node and Python build tools, locked dependencies, `SOURCE_DATE_EPOCH` and `PYTHONHASHSEED`, but bit-for-bit reproduction across different runner environments is **not yet a proven release invariant**.

## Migration, rollback and recovery

For each deployed server: back up persistent data and volumes, deactivate the project, install published v0.6.5, explicitly refresh the root-owned standalone updater, verify health and CLI, then publish v0.6.6 only after completing the fleet migration checkpoint. No reinstall, migration of project database or deletion of workspaces is permitted. Wheel installation occurs in an isolated release directory/venv and only swaps the versioned `current` symlink after artifact validation and inactive-health preflight. Post-swap health failures restore the prior pointer and restart the prior services. Legacy v0.6.5 tagged source remains available for fallback.

The web process remains unprivileged, with controller-owned Docker authority through existing restricted IPC; Tailscale isolation and zero-or-one-active-project invariant are unchanged. No new privileged web endpoints or host Node runtime are introduced.

Real-server upgrade, reboot/crash, rollback and mobile/tailnet checks are owner acceptance tasks, not claimed satisfied by CI.

No GitHub admin token or immutable-releases setting is required. The main-only workflow uses `GITHUB_TOKEN` to validate and publish the reviewed wheel. The standalone updater validates GitHub asset digests and release metadata, not the GitHub `immutable` flag. **Migration caveat:** The already-published v0.6.5 standalone updater itself requires `immutable: true`. Removing that condition from the v0.6.6 source code does not update an existing host. For an opt-out host, the root-owned standalone updater must be replaced with the reviewed new updater **before** v0.6.6 wheel installation, or the old updater will refuse v0.6.6. Never silently bypass that existing safety gate.

### One-time opt-out migration for already published v0.6.5

The immutable-only flag was released in v0.6.5 and cannot be undone by editing later source. Operators who choose mutable GitHub Releases must use the version-pinned `scripts/promote_mutable_release_updater.py` helper from the **published v0.6.6 Git tag**, running on a healthy inactive v0.6.5 server. It retrieves the published wheel using the artifact verifier in that tagged checkout, matches its embedded standalone updater bytes against the checked-out source, and atomically replaces only the standalone root updater with a saved backup. No application activation occurs. This is an explicit, one-time migration to satisfy compatibility with v0.6.5, not an ongoing requirement; do not publish/merge without manual acceptance of this operation.
