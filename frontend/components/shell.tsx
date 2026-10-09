"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useRef } from "react";
import { liveMode, operatorMode } from "@/lib/api/client";
import { UiIcon, type IconName } from "@/components/ui-icon";
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
  ["Home", "/operations"],
  ["Clients", "/clients"],
  ["Review", "/review"],
  ["Settings", "/settings"],
];

const navIcons: Record<string, IconName> = {
  "/dashboard": "home",
  "/operations": "home",
  "/clients": "users",
  "/review": "review",
  "/jobs": "briefcase",
  "/briefs": "review",
  "/runs": "activity",
  "/history": "clock",
  "/diagnostics": "activity",
  "/settings": "settings",
};

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
        className={mobile ? "menu-navigation" : "workspace-navigation"}
        aria-label={mobile ? "Mobile primary navigation" : "Primary navigation"}
      >
        {items.map(([name, path]) => (
          <Link
            key={path}
            href={path}
            aria-current={pathname === path ? "page" : undefined}
            onClick={mobile ? close : undefined}
          >
            <UiIcon name={navIcons[path] ?? "layers"} size={20} />
            <span>{name}</span>
          </Link>
        ))}
      </nav>
    );
  };

  const clientScope = operatorMode
    ? "Private workspace"
    : `Client: ${client?.display_name ?? "Unavailable"}`;
  const operatorLabel = operatorMode
    ? "Owner only"
    : `${session?.display_name ?? "Operator"} · ${liveMode ? "trusted local session" : "fixture session"}`;

  return (
    <>
      <a className="skip" href="#main">
        Skip to main content
      </a>
      <header className="topbar">
        {!operatorMode ? (
          <button
            ref={menu}
            className="menu-button"
            onClick={() => dialog.current?.showModal()}
            aria-haspopup="dialog"
            aria-label="Open menu"
          >
            <UiIcon name="menu" />
          </button>
        ) : null}
        <Link href={operatorMode ? "/operations" : liveMode ? "/clients" : "/jobs"} className="wordmark">
          <span className="brand-symbol"><UiIcon name="layers" size={23} /></span>
          <span className="brand-name">Job<span>Sift</span></span>
        </Link>
        <span className="workspace-chip"><UiIcon name="lock" size={15}/>{clientScope}</span>
        <span className="operator">{operatorLabel}</span>
      </header>
      <div className="shell">
        <aside className="sidebar">
          <p className="sidebar-heading">YOUR WORKSPACE</p>
          {links()}
          <div className="sidebar-foot">
            <div className="sidebar-foot-icon"><UiIcon name="sparkles" size={20} /></div>
            <strong>Less noise. More progress.</strong>
            <p>Focus on what matters today.</p>
          </div>
        </aside>
        <main id="main" tabIndex={-1}>
          <div className={operatorMode ? "sr-only" : "evidence-label"}>
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
      {operatorMode ? (
        <nav className="mobile-dock" aria-label="Mobile primary navigation">
          {operatorNavigation.map(([name, path]) => (
            <Link key={path} href={path} aria-current={pathname === path ? "page" : undefined}>
              <UiIcon name={navIcons[path] ?? "home"} size={22} />
              <span>{name}</span>
            </Link>
          ))}
        </nav>
      ) : null}
      <dialog
        ref={dialog}
        className="mobile-menu"
        aria-label="Navigation"
        onClose={() => menu.current?.focus()}
      >
        <div className="dialog-head">
          <strong>JobSift</strong>
          <button onClick={close}><UiIcon name="close" /> Close menu</button>
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
