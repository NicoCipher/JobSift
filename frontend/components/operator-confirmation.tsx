"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { UiIcon } from "@/components/ui-icon";

export type Confirmation = {
  title: string;
  description: string;
  confirmLabel: string;
  tone?: "danger" | "primary";
};

/**
 * Native <dialog> provides focus trapping and Escape handling. A decision is
 * resolved only after the explicit user action; never auto-confirm mutations.
 */
export function useOperatorConfirmation() {
  const [request, setRequest] = useState<Confirmation | null>(null);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const resolver = useRef<((confirmed: boolean) => void) | null>(null);
  const previousFocus = useRef<HTMLElement | null>(null);
  const titleId = useId();
  const descriptionId = useId();

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (request && !dialog.open) dialog.showModal();
    if (!request && dialog.open) dialog.close();
  }, [request]);

  const ask = useCallback((options: Confirmation): Promise<boolean> => {
    if (resolver.current) return Promise.resolve(false);
    return new Promise<boolean>((resolve) => {
      previousFocus.current =
        document.activeElement instanceof HTMLElement ? document.activeElement : null;
      resolver.current = resolve;
      setRequest(options);
    });
  }, []);

  const decide = useCallback((approved: boolean) => {
    const resolve = resolver.current;
    resolver.current = null;
    dialogRef.current?.close();
    setRequest(null);
    resolve?.(approved);
    previousFocus.current?.focus();
  }, []);

  const confirmationDialog = (
    <dialog
      ref={dialogRef}
      className="operator-confirm-dialog"
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      onCancel={(event) => {
        event.preventDefault();
        decide(false);
      }}
      onClose={() => {
        if (resolver.current) decide(false);
      }}
    >
      {request ? (
        <div className="operator-confirm-content">
          <span className="operator-confirm-symbol" data-tone={request.tone ?? "primary"}>
            <UiIcon name={request.tone === "danger" ? "alert" : "shield"} size={25} />
          </span>
          <h2 id={titleId}>{request.title}</h2>
          <p id={descriptionId}>{request.description}</p>
          <p className="operator-confirm-note">
            <UiIcon name="info" size={16} />
            No change will happen until you confirm.
          </p>
          <div className="operator-confirm-actions">
            <button type="button" className="secondary-action" onClick={() => decide(false)}>
              Cancel
            </button>
            <button
              type="button"
              className="operator-confirm-accept"
              data-tone={request.tone ?? "primary"}
              onClick={() => decide(true)}
            >
              {request.confirmLabel}
            </button>
          </div>
        </div>
      ) : null}
    </dialog>
  );

  return { ask, confirmationDialog };
}
