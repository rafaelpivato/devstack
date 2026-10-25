# ADR-0008: Controller-supervised targeted Compose reactions (#90)

- **Status:** Accepted for v0.6.8 implementation review
- **Baseline:** v0.6.7, SQLite schema 11 → 12
- **Supersedes:** No existing ADR. Complements ADR-0007.

## Decision

Repository sync remains the owner of Git worktree updates. A successful and
**changed-HEAD** fast-forward records an explicitly opted-in reaction intent,
keyed by project UUID, repository UUID, Compose service and resulting commit.
No policy is configured by default. Read-only checks, branch previews and
unchanged Git updates never record or execute Compose actions.

Repositories and services are linked by declared pairs, not guessed from file
paths or Docker image names. Service selection comes from the validated managed
Compose configuration, not arbitrary client-supplied Docker argv. One repository
may trigger several services and multiple repositories can target one service.
Pending intents targeting the same service/generation are combined; Redeploy
takes precedence over Restart. Reactions are only admitted against the
currently active project and activation generation. Inactive updates are
recorded as skipped. Source changes are blocked during pending/active
maintenance, preventing a build from racing a fast-forward.

Actions:

- **None:** default; no intent.
- **Restart:** `docker compose restart --no-deps <service>`.
- **Redeploy:** `docker compose up -d --no-deps --build --force-recreate --wait
  --wait-timeout 150 <service>`. Rebuild applies to services with a build
  definition; recreate happens regardless. No unrelated Compose services are
  started or recreated, and no named volumes are deleted.

Existing whole-Compose Build/Restart jobs retain their IPC and CLI contracts.
Manual targeted actions use the same JobSupervisor (logs, generation fencing,
conflict checks, project scope); controller is still the only Docker authority.

## Recovery / limits

Reactions have SQLite intent receipts; admission and job linking commit
atomically. Jobs retain their existing bounded output/history. Unsubmitted
intents are marked interrupted after controller restart and are **not** replayed
automatically. Stale generations are skipped. Failed or ambiguous submissions
require operator attention; no retry loop or rollback promises. The service
list and command line arguments are verified on admission. Existing
non-independent jobs are cancelled only through a guarded project transition.

Policies are managed under each repository. Live actions are available under
the project's Services screen, even without a Git update. No new daemon,
webhook, domain, arbitrary script or file watcher is introduced.

## Acceptance

Regression tests must cover schema migration, opt-in policy, CSRF, active
generation fencing, no GET side effects, same-service deduplication, stopped
or foreign project rejection, unchanged HEAD, manual actions, and legacy
whole-stack Build/Restart. Docker smoke must confirm single-service
redeployment leaves unrelated containers intact. Physical-host verification
remains distinct from CI; v0.6.8 can be installed after normal safe
deactivation and release validation.
