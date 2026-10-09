"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useRef } from "react";
import { liveMode, operatorMode } from "@/lib/api/client";
import type { Client, Session } from "@/lib/contracts/service";

const navigation = [
  ["Dashboard", "/dashboard"],
  ["Clients", "/clients"],
  ["Review", "/review"],
  ["Operations", "/operations"],
  ["Jobs", "/jobs"],
  ["Search Briefs", "/briefs"],
  ["Runs", "/runs"],
  ["History", "/history"],
  ["Diagnostics", "/diagnostics"],
  ["Settings", "/settings"],
];

const operatorNavigation = [
  ["Operations", "/operations"],
  ["Clients", "/clients"],
  ["Review", "/review"],
  ["Settings", "/settings"],
];

const localServiceNavigation = navigation.filter(([, path]) =>
  ["/clients", "/review", "/operations", "/jobs", "/settings"].includes(path),
);

export function Shell({
  children,
  client,
  session,
}: {
  children: React.ReactNode;
  client: Client | null;
  session: Session | null;
}) {
  const pathname = usePathname();
  const dialog = useRef<HTMLDialogElement>(null);
  const menu = useRef<HTMLButtonElement>(null);

  const close = () => {
    dialog.current?.close();
    menu.current?.focus();
  };

  const links = (mobile = false) => {
    const items = operatorMode
      ? operatorNavigation
      : liveMode
        ? localServiceNavigation
        : navigation;
    return (
      <nav
        aria-label={mobile ? "Mobile primary navigation" : "Primary navigation"}
      >
        {items.map(([name, path]) => (
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
  };

  const clientScope = operatorMode
    ? "Clients: production"
    : `Client: ${client?.display_name ?? "Unavailable"}`;
  const operatorLabel = operatorMode
    ? "Production workspace"
    : `${session?.display_name ?? "Operator"} · ${liveMode ? "trusted local session" : "fixture session"}`;

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
        <Link href={operatorMode ? "/operations" : liveMode ? "/clients" : "/jobs"} className="wordmark">
          JobSift
        </Link>
        <span className="client-scope">{clientScope}</span>
        <span className="operator">{operatorLabel}</span>
      </header>
      <div className="shell">
        <aside className="sidebar">
          {links()}
          <p className="sidebar-note metadata">
            Operations is your home
            <br />
            Clients and Review support delivery
          </p>
        </aside>
        <main id="main" tabIndex={-1}>
          <div className="evidence-label">
            <span className="fixture-tag">
              {operatorMode
                ? "Production"
                : liveMode
                  ? "Local service"
                  : "Development fixtures"}
            </span>
            <span>
              {operatorMode
                ? "Live operator controls · backend remains authoritative"
                : liveMode
                  ? "Registered local-service evidence"
                  : "Development fixtures · production commands stay server-side"}
            </span>
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
        <p className="secondary">
          {operatorMode
            ? "Production operator workspace"
            : `${client?.display_name ?? "Unavailable"} · ${liveMode ? "local service" : "fixture"}`}
        </p>
        {links(true)}
      </dialog>
    </>
  );
}
