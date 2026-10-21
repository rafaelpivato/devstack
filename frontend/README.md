# DevStack v0.6 browser foundation

- `npm ci`, `npm run typecheck`, `npm test`, `npm run build` (Node is a **development/CI-only** requirement).
- Source lives in `frontend/src/`. Compiled **versioned** assets and Vite `manifest.json` are committed under `src/devstack/static/v06/`.
- Flask serves the packaged assets at `/__v06` **as an internal staging entry only**. The existing `/` dashboard and v0.5.3 APIs are intentionally unchanged until feature-complete navigation can replace them.
- The stable installer/updater clones a GitHub release tag and executes `pip install`; it must **not** run Node, `npm install` or download browser dependencies on the host. Therefore source + lockfile + checked-in output must match and be present in both Git checkout and Python wheel. Frontend CI rebuilds and fails if the tracked output changes.
- Never put controller credentials, secrets or private host paths into JavaScript bundles. Frontend calls only same-origin `/api/v2`; Flask retains CSRF/session checks, controller permissions and typed IPC.
- Do not treat browser-selected project as active. No automatic activation; no client-side Docker/Git or shell commands.
- I1 intentionally uses only `GET /api/v2/runtime` to demonstrate safe read-only server-state integration. Feature-specific navigation and actions follow in I2–I7.
