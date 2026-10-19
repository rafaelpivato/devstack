## Summary

Describe the change and why it is needed.

## Architecture / requirement traceability

Link the applicable ADR and acceptance contract. For DevStack v0.5.2 architecture work, refer to:
- [ADR-0001](docs/adr/0001-multiple-projects-single-runtime.md)
- [v0.5.2 acceptance contract](docs/v0.5.2-acceptance-plan.md)

| ADR invariant | Acceptance case | Implementation files | Automated test |
|---|---|---|---|
| INV-xx | AC-xx | | |

## Validation

- [ ] Unit tests
- [ ] Docker integration tests (two distinct projects for v0.5.2 lifecycle changes)
- [ ] Systemd / clean-host validation if installer, privileges or supervisor changes
- [ ] No unreviewed changes to architecture invariants
- [ ] No accidental project activation from selection/import
- [ ] No auto-merge or release of an incomplete integration branch

If a check is not relevant, say why. Do not treat single-project Smoke Test success as multi-project acceptance.
