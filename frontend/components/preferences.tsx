"use client";
import { useSyncExternalStore } from "react";
export type Preferences = {
  theme: "system" | "light" | "dark";
  density: "default" | "compact" | "comfortable";
  shortcuts: boolean;
};
const defaults: Preferences = {
  theme: "system",
  density: "default",
  shortcuts: true,
};
let cached: Preferences = defaults;
let loaded = false;
function read() {
  if (!loaded && typeof window !== "undefined") {
    loaded = true;
    try {
      const saved = JSON.parse(
        localStorage.getItem("jobsift-presentation") ?? "{}",
      );
      cached = {
        theme: ["system", "light", "dark"].includes(saved.theme)
          ? saved.theme
          : "system",
        density: ["default", "compact", "comfortable"].includes(saved.density)
          ? saved.density
          : "default",
        shortcuts: saved.shortcuts !== false,
      };
    } catch {
      /* Local preferences are optional. */
    }
  }
  return cached;
}
const subscribe = (listener: () => void) => {
  window.addEventListener("jobsift-preferences", listener);
  return () => window.removeEventListener("jobsift-preferences", listener);
};
export function usePreferences() {
  return useSyncExternalStore(subscribe, read, () => defaults);
}
export function savePreferences(patch: Partial<Preferences>) {
  cached = { ...read(), ...patch };
  document.documentElement.dataset.theme = cached.theme;
  document.documentElement.dataset.density = cached.density;
  try {
    localStorage.setItem("jobsift-presentation", JSON.stringify(cached));
  } catch {
    /* Keep in-memory settings if storage is unavailable. */
  }
  window.dispatchEvent(new Event("jobsift-preferences"));
}
export function PresentationSettings() {
  const preferences = usePreferences();
  return (
    <>
      <label className="settings-field">
        Theme
        <select
          value={preferences.theme}
          onChange={(e) =>
            savePreferences({ theme: e.target.value as Preferences["theme"] })
          }
        >
          <option value="system">System</option>
          <option value="light">Light</option>
          <option value="dark">Dark</option>
        </select>
      </label>
      <label className="settings-field">
        Table density
        <select
          value={preferences.density}
          onChange={(e) =>
            savePreferences({
              density: e.target.value as Preferences["density"],
            })
          }
        >
          <option value="default">Default · 40px minimum</option>
          <option value="compact">Compact · desktop only</option>
          <option value="comfortable">Comfortable</option>
        </select>
      </label>
      <label className="settings-field">
        Character keyboard shortcuts
        <select
          value={preferences.shortcuts ? "enabled" : "disabled"}
          onChange={(e) =>
            savePreferences({ shortcuts: e.target.value === "enabled" })
          }
        >
          <option value="enabled">Enabled</option>
          <option value="disabled">Disabled</option>
        </select>
      </label>
      <p className="secondary">
        Saved on this browser. No account or sourcing rules are changed.
      </p>
      <details>
        <summary>Keyboard shortcuts</summary>
        <p>
          / focuses Jobs search. In the Jobs list, j/k move between records;
          Enter inspects a record; Esc closes detail and restores focus. These
          keys do not operate while typing.
        </p>
      </details>
    </>
  );
}
