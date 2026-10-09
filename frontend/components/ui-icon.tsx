import type { ReactNode } from "react";

export type IconName =
  | "home" | "users" | "review" | "settings" | "menu" | "close" | "arrow-right"
  | "arrow-up-right" | "refresh" | "shield" | "alert" | "check" | "briefcase"
  | "send" | "clock" | "sheet" | "sparkles" | "activity" | "info"
  | "layers" | "bolt" | "lock" | "pause" | "play" | "chevron-down";

const symbols: Record<IconName, ReactNode> = {
  home: <><path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z" /><path d="M9 21v-8h6v8" /></>,
  users: <><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" /><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></>,
  review: <><rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4.5h6M9 11l1.5 1.5L14 9M9 17h6"/></>,
  settings: <><path d="M4 7h16M4 17h16M8 4v6M16 14v6"/><circle cx="8" cy="7" r="2"/><circle cx="16" cy="17" r="2"/></>,
  menu: <path d="M4 7h16M4 12h16M4 17h16"/>,
  close: <path d="M5 5l14 14M19 5 5 19"/>,
  "arrow-right": <><path d="M4 12h16M13 5l7 7-7 7"/></>,
  "arrow-up-right": <><path d="M7 17 17 7M7 7h10v10"/></>,
  refresh: <><path d="M20 7v5h-5M4 17v-5h5"/><path d="M5 9a7 7 0 0 1 12-3l3 6M4 12l3 6a7 7 0 0 0 12-3"/></>,
  shield: <><path d="M12 22s8-4 8-11V5l-8-3-8 3v6c0 7 8 11 8 11z"/><path d="m9 12 2 2 4-4"/></>,
  alert: <><path d="m10.3 3-8.7 15a2 2 0 0 0 1.7 3h17.4a2 2 0 0 0 1.7-3l-8.7-15a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></>,
  check: <><circle cx="12" cy="12" r="10"/><path d="m7.5 12 3 3 6-6"/></>,
  briefcase: <><rect x="3" y="7" width="18" height="14" rx="2"/><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M3 12a20 20 0 0 0 18 0M12 11v3"/></>,
  send: <><path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/></>,
  clock: <><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></>,
  sheet: <><path d="M7 2h7l5 5v13a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z"/><path d="M14 2v5h5M8 12h8M8 16h8M12 10v9"/></>,
  sparkles: <><path d="m12 3 1.6 5.4L19 10l-5.4 1.6L12 17l-1.6-5.4L5 10l5.4-1.6L12 3zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8L19 16zM4 2l.6 1.4L6 4l-1.4.6L4 6l-.6-1.4L2 4l1.4-.6L4 2z"/></>,
  activity: <><path d="M3 12h4l3-8 4 16 3-8h4"/></>,
  info: <><circle cx="12" cy="12" r="10"/><path d="M12 11v5M12 7h.01"/></>,
  layers: <><path d="m12 2 9 5-9 5-9-5 9-5zM3 12l9 5 9-5M3 17l9 5 9-5"/></>,
  bolt: <path d="m13 2-9 12h7l-1 8 10-12h-7l1-8z"/>,
  lock: <><rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3M12 15v2"/></>,
  pause: <><circle cx="12" cy="12" r="10"/><path d="M10 8v8M14 8v8"/></>,
  play: <><circle cx="12" cy="12" r="10"/><path d="m10 8 6 4-6 4z"/></>,
  "chevron-down": <path d="m6 9 6 6 6-6"/>,
};

/** Decorative icons sit beside visible labels; icons never replace action text. */
export function UiIcon({
  name,
  size = 20,
  className,
}: {
  name: IconName;
  size?: number;
  className?: string;
}) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.85"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {symbols[name]}
    </svg>
  );
}
