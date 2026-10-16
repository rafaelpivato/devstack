/* Poll without overlapping requests; suspend updates in hidden tabs. */
export function startPolling(refresh, intervalMs) {
  let stopped = false;
  let inFlight = false;
  const run = async () => {
    if (stopped || inFlight || document.hidden) return;
    inFlight = true;
    try { await refresh(); } finally { inFlight = false; }
  };
  void run();
  const interval = window.setInterval(() => { void run(); }, intervalMs);
  const onVisibility = () => { if (!document.hidden) void run(); };
  document.addEventListener("visibilitychange", onVisibility);
  return () => {
    stopped = true;
    window.clearInterval(interval);
    document.removeEventListener("visibilitychange", onVisibility);
  };
}
