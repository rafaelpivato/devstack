import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../api/client";

export type ResourceState<T> =
  | { kind: "loading"; data: null; error: null }
  | { kind: "ready"; data: T; error: null }
  | { kind: "error"; data: null; error: ApiError };

export function useResource<T>(
  load: (signal: AbortSignal) => Promise<T>,
  refreshMs = 15000,
): ResourceState<T> {
  const [state, setState] = useState<ResourceState<T>>({kind:"loading", data:null, error:null});
  const refresh = useCallback(load, [load]);
  useEffect(() => {
    let closed = false;
    let pending = false;
    const controller = new AbortController();

    const poll = async () => {
      if (closed || pending || document.hidden) return;
      pending = true;
      try {
        const data = await refresh(controller.signal);
        if (!closed) setState({kind:"ready", data, error:null});
      } catch (error) {
        if (!closed && !controller.signal.aborted) {
          const safe = error instanceof ApiError ? error :
            new ApiError("unavailable", "DevStack is temporarily unavailable.");
          setState({kind:"error", data:null, error:safe});
        }
      } finally {
        pending = false;
      }
    };
    void poll();
    const timer = refreshMs > 0 ? window.setInterval(() => { void poll(); }, refreshMs) : null;
    document.addEventListener("visibilitychange", poll);
    return () => {
      closed = true;
      controller.abort();
      if (timer !== null) window.clearInterval(timer);
      document.removeEventListener("visibilitychange", poll);
    };
  }, [refresh, refreshMs]);
  return state;
}
