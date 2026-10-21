# DevStack v0.6 browser shell

- `npm ci`, `npm run typecheck`, `npm test`, `npm run build` (Node is a **development/CI-only** requirement).
- Source lives in `frontend/src/`. Compiled **versioned** assets and Vite `manifest.json` are tracked under `src/devstack/static/v06/`. CI rebuilds and rejects stale bundles; Flask and the Python wheel serve only these checked-in assets.
- Opt-in browser shell: `/__v06`. Deep links such as `/__v06/projects/<uuid>/repositories` are allowlisted by Flask. Stable `/` and `/projects/<uuid>` remain the default existing UI until feature parity is complete.
- Opening the v0.6 preview reads **only** `GET /api/v2/runtime`, `GET /api/v2/projects`, and when needed, `GET /api/v2/projects/<uuid>`. These are existing controller IPC-backed read routes. No project selection or browsing triggers a runtime mutation.
- Root entry uses the **global active runtime**: active project → active project home; genuinely inactive → choose a project; transitioning, error, unavailable, or unknown → explicit status/error, never autoactivation. Inactive project inspection is safe and remains distinct.
- Hierarchical routes follow Project → Lane → Repository detail. The app uses native `history.pushState`, `popstate` and deliberate Back; a cold deep link uses the canonical parent. There is no synthetic global `current` UUID or browser-persisted active project, and no new routing dependency.
- I2 provides an accessible navigation shell, real project/repository/application-definition summaries, and a clearly labeled link to existing UI for operations not yet migrated. Service/job execution, switching and recovery still use the current working dashboard. I3 adds receipt-backed runtime transitions; I4–I6 move feature workflows.
- The stable installer/updater clones a GitHub release tag and runs `pip install`; it must **not** run Node, npm, or download browser dependencies on the host. Tracked build assets must be reproducible and included in the wheel/tag.
- Never put controller credentials, secrets, or private host paths into JavaScript bundles. Frontend calls only same-origin `/api/v2`; Flask retains CSRF/session, controller permissions, and typed IPC.
- Desktop responsiveness matters, but screen structure is mobile-first. Status uses text and icons as well as accessible accents; navigation order/tiles can change without rewriting the typed client.
