"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useRef } from "react";
import type { Client, Session } from "@/lib/contracts/service";
import { liveMode } from "@/lib/api/client";
const navigation = [
  ["Dashboard", "/dashboard"],
  ["Operations", "/operations"],
  ["Jobs", "/jobs"],
  ["Review", "/review"],
  ["Search Briefs", "/briefs"],
  ["Runs", "/runs"],
  ["History", "/history"],
  ["Diagnostics", "/diagnostics"],
  ["Settings", "/settings"],
];
export function Shell({
  children,
  client,
  session,
}: {
  children: React.ReactNode;
  client: Client;
  session: Session;
}) {
  const pathname = usePathname();
  const dialog = useRef<HTMLDialogElement>(null);
  const menu = useRef<HTMLButtonElement>(null);
  const close = () => {
    dialog.current?.close();
    menu.current?.focus();
  };
  const links = (mobile = false) => (
    <nav
      aria-label={mobile ? "Mobile primary navigation" : "Primary navigation"}
    >
      {(liveMode ? navigation.filter(([, path]) => ["/jobs", "/operations", "/settings"].includes(path)) : navigation).map(([name, path]) => (
        <Link
          key={path}
          href={path}
          aria-current={pathname === path ? "page" : undefined}
          onClick={mobile ? close : undefined}
        >
          {name}
        </Link>
      ))}
    </nav>
  );
  return (
    <>
      <a className="skip" href="#main">
        Skip to main content
      </a>
      <header className="topbar">
        <button
          ref={menu}
          className="menu-button"
          onClick={() => dialog.current?.showModal()}
          aria-haspopup="dialog"
        >
          Menu
        </button>
        <Link href="/jobs" className="wordmark">
          JobSift
        </Link>
        <span className="client-scope">Client: {client.display_name}</span>
        <span className="operator">
          {session.display_name} · {liveMode ? "trusted local session" : "fixture session"}
        </span>
      </header>
      <div className="shell">
        <aside className="sidebar">
          {links()}
          <p className="sidebar-note metadata">
            Evidence views are read-only
            <br />
            Operations uses explicit production commands
          </p>
        </aside>
        <main id="main" tabIndex={-1}>
          <div className="evidence-label">
            <span className="fixture-tag">{liveMode ? "Local service" : "Development fixtures"}</span>
            <span>{liveMode ? "Registered evidence · production commands only in Operations" : "Fictional evidence · production commands only in Operations"}</span>
          </div>
          {children}
        </main>
      </div>
      <dialog
        ref={dialog}
        className="mobile-menu"
        aria-label="Navigation"
        onClose={() => menu.current?.focus()}
      >
        <div className="dialog-head">
          <strong>JobSift</strong>
          <button onClick={close}>Close menu</button>
        </div>
        <p className="secondary">{client.display_name} · {liveMode ? "local service" : "fixture"}</p>
        {links(true)}
      </dialog>
    </>
  );
}
