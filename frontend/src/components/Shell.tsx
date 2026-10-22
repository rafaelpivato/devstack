import {useEffect,useRef,type PropsWithChildren,type ReactNode} from "react";
import type { Navigator } from "../navigation/useNavigation";
import { NavLink, GLOBAL_PROJECTS } from "../navigation/useNavigation";
import { PREVIEW_ROOT } from "../navigation/routes";

export function Shell({title, subtitle, navigation, children, back, badge}:PropsWithChildren<{
  title:string; subtitle?:string; navigation:Navigator; back?:boolean; badge?:ReactNode;
}>) {
  return <div className="app-shell">
    <header className="app-header">
      <div className="topline">
        {back && <button type="button" className="icon-button" aria-label="Back" onClick={navigation.back}>‹</button>}
        <span className="wordmark"><span aria-hidden="true" className="brand-mark">▤</span> DevStack</span>
        <NavLink to={GLOBAL_PROJECTS} navigate={navigation.navigate} className="global-projects">Projects</NavLink>
      </div>
      <div className="page-heading"><h1 tabIndex={-1} ref={heading}>{title}</h1>{badge}</div>
      {subtitle && <p className="page-subtitle">{subtitle}</p>}
    </header>
    <main className="app-content" id="content">{children}</main>
    <footer className="app-footer">
      <NavLink to={PREVIEW_ROOT} navigate={navigation.navigate}>Current project</NavLink>
      <span className="footer-separator" aria-hidden="true">·</span>
      <a href="/">Current UI</a>
    </footer>
  </div>;
}

export function ItemLink({icon, title, detail, suffix, href, navigation}:{
  icon:string; title:string; detail?:string; suffix?:ReactNode; href:string; navigation:Navigator;
}) {
  return <NavLink to={href} navigate={navigation.navigate} className="item-link">
    <span className="item-icon" aria-hidden="true">{icon}</span>
    <span className="item-text"><strong>{title}</strong>{detail && <small>{detail}</small>}</span>
    {suffix && <span className="item-suffix">{suffix}</span>}
    <span aria-hidden="true" className="chevron">›</span>
  </NavLink>;
}

export function Notice({title, children, role="status"}:PropsWithChildren<{title:string; role?:"status"|"alert"}>) {
  return <section className="notice" role={role}><strong>{title}</strong>{children && <div className="notice-content">{children}</div>}</section>;
}

export function Status({text, kind="neutral"}:{text:string;kind?:"positive"|"neutral"|"warning"}) {
  const sign = kind === "positive" ? "✓" : kind === "warning" ? "!" : "◌";
  return <span className={"status status--" + kind}><span aria-hidden="true">{sign}</span>{text}</span>;
}

export function Skeleton(){return <div className="skeleton" role="status">Loading…</div>;}
