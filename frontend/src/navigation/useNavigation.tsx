import { useCallback, useEffect, useState, type MouseEvent, type PropsWithChildren } from "react";
import { parentUrl, PREVIEW_ROOT, routeFor, type AppRoute } from "./routes";

/** Deliberately small History API adapter: URLs own the route, no global store. */
export function useNavigation() {
  const [path, setPath] = useState(() => window.location.pathname);
  useEffect(() => {
    const onPop = () => { setPath(window.location.pathname); };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const navigate = useCallback((to: string) => {
    if (routeFor(to).kind === "not-found") throw new Error("Invalid navigation target");
    if (window.location.pathname === to) return;
    window.history.pushState({devstackNavigation: true}, "", to);
    setPath(to);
    window.scrollTo({top:0, behavior:"instant"});
  }, []);
  const route = routeFor(path);
  const back = useCallback(() => {
    // App-origin state is attached to the *current* entry on each push.
    if (window.history.state?.devstackNavigation === true && window.history.length > 1) {
      window.history.back();
    } else {
      const fallback = parentUrl(route);
      window.history.replaceState({devstackNavigation:false}, "", fallback);
      setPath(fallback);
    }
  }, [route]);
  return { route, navigate, back };
}
export type Navigator = ReturnType<typeof useNavigation>;

export function NavLink({to, navigate, children, className, current}: PropsWithChildren<{
  to:string; navigate:Navigator["navigate"]; className?:string; current?:boolean;
}>) {
  function onClick(event: MouseEvent<HTMLAnchorElement>) {
    if (event.defaultPrevented || event.button !== 0 ||
        event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigate(to);
  }
  return <a href={to} onClick={onClick} className={className} aria-current={current ? "page":undefined}>{children}</a>;
}
export function OldDashboardLink({projectId, children}: PropsWithChildren<{projectId?:string}>) {
  // This is a deliberate handoff to still-supported v0.5.3 functionality.
  return <a href={projectId ? "/projects/" + encodeURIComponent(projectId) : "/"}>{children ?? "Open current dashboard"}</a>;
}
export const GLOBAL_PROJECTS = PREVIEW_ROOT + "/projects";
