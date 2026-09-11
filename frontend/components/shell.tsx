"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useRef } from "react";
import type { Client, Session } from "@/lib/contracts/service";
const navigation = [
  ["Dashboard", "/dashboard"],
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
      {navigation.map(([name, path]) => (
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
          {session.display_name} · fixture session
        </span>
      </header>
      <div className="shell">
        <aside className="sidebar">
          {links()}
          <p className="sidebar-note metadata">
            Read-only workbench
            <br />
            Fictional evidence
          </p>
        </aside>
        <main id="main" tabIndex={-1}>
          <div className="evidence-label">
            <span className="fixture-tag">Development fixtures</span>
            <span>Fictional records · no live sourcing or writes</span>
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
        <p className="secondary">{client.display_name} · fixture</p>
        {links(true)}
      </dialog>
    </>
  );
}
