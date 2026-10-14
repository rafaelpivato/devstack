# Smoke fixture

Disposable Docker Compose integration fixture for Devstack. Import it with the dashboard's **Install smoke example** button. The managed Compose file lives at `~/workspaces/smoke/.devstack/compose.yaml`, outside any Git repository. It builds a Python HTTP server, runs Redis and PostgreSQL, preserves a named database volume when stopped, and binds HTTP only to `127.0.0.1:18080`.

For manual CI smoke validation:

```sh
docker compose -f examples/smoke/.devstack/compose.yaml -p devstack-smoke up --detach --build --wait
curl --fail http://127.0.0.1:18080/health
docker compose -f examples/smoke/.devstack/compose.yaml -p devstack-smoke down --volumes
```
