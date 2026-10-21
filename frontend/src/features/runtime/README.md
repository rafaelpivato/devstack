# Runtime transitions (I3)

Only the controller creates or advances durable runtime receipts; Flask validates narrow requests, same-origin CSRF and idempotency keys, and transports existing `transition_submit/status/list` over typed IPC.

- Navigation/GETs never transition a runtime. Actions require an explicit confirmation view and user click. Destructive actions require an additional checkbox.
- Generate a 128-bit key once, record it in sessionStorage **before** POST, and reuse it when explicitly retrying a response with unknown outcome. SessionStorage holds only action, target ID, key and optional receipt ID — no secrets or outputs. Browser storage can be unavailable; in-memory recovery still works while the tab stays open.
- A submitted controller receipt survives browser/SSH disconnect. Browser list/detail are read-only and can be bookmarked. A controller restart may report `interrupted`; no automatic resubmission, no assumption that Docker reverted to inactive.
- Read the current runtime immediately before submitting, and let the controller revalidate the expected snapshot under its own exclusive lock. Never optimistically mark a project active.
- Active project home has Deactivate, inactive project detail has Activate or Switch, and error state offers reviewed recovery. After completion navigate back to the controller-driven root; runtime state is not derived from a receipt alone.
- No new privileged IPC or execution mechanism; existing synchronous legacy endpoints remain for compatibility until release parity.
