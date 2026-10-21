import type { PropsWithChildren, ReactNode } from "react";

export function Screen({title, children, footer}: PropsWithChildren<{title: string; footer?: ReactNode}>) {
  return <main className="screen">
    <header className="screen-header"><span className="brand">DevStack</span><span className="section">{title}</span></header>
    <div className="screen-body">{children}</div>
    {footer && <footer className="screen-footer">{footer}</footer>}
  </main>;
}

export function StateLabel({text, icon}: {text: string; icon: string}) {
  return <span className="state-label"><span aria-hidden="true">{icon}</span>{text}</span>;
}
