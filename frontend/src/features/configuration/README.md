# v0.6 I6 configuration and administration

- Project configuration is a nested lane; global SSH/update controls are secondary under Projects → Administration, not visible as omnipresent cross-navigation.
- Compose saves carry the server-reported revision; Docker Compose config is validated by the controller. No new Docker authority in Flask.
- Applications/tasks are **full collection** updates only. The I6 `settings-review` contract adds an opaque canonical SHA-256 revision. The controller holds its transition lock, verifies the inactive Docker state, recomputes the current revision, and rejects stale saves **before** the write. The legacy settings endpoint remains for old dashboard compatibility.
- Webhook changes use the registered project and exact existing ingress routes. Changing routes can take effect immediately; there is no speculative webhook replay.
- GitHub SSH management handles public key/test only, not the private key.
- Published updater is the existing systemd-tagged release workflow. It checks latest version and inactive state independently, with no host shell exposure.
- Browser POSTs are always deliberate and CSRF protected; error/unknown outcomes do not trigger automatic retries.
