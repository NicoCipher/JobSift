import type { ReactNode } from "react";
import { UiIcon } from "@/components/ui-icon";

export type FeedbackTone = "error" | "warning" | "success" | "info";

export function OperatorNotice({
  tone,
  title,
  children,
  onRetry,
  retryLabel = "Try again",
  className = "",
}: {
  tone: FeedbackTone;
  title: string;
  children?: ReactNode;
  onRetry?: () => void;
  retryLabel?: string;
  className?: string;
}) {
  const icon = tone === "error" || tone === "warning"
    ? "alert"
    : tone === "success"
      ? "check"
      : "info";
  return (
    <div
      className={"operator-notice " + className}
      data-tone={tone}
      role={tone === "error" ? "alert" : "status"}
    >
      <span className="operator-notice-icon"><UiIcon name={icon} size={21} /></span>
      <div className="operator-notice-body">
        <strong>{title}</strong>
        {children ? <div className="operator-notice-detail">{children}</div> : null}
        {onRetry ? (
          <button type="button" className="operator-notice-retry" onClick={onRetry}>
            <UiIcon name="refresh" size={16} /> {retryLabel}
          </button>
        ) : null}
      </div>
    </div>
  );
}

export function OperatorFeedback({
  error,
  message,
  onRetry,
}: {
  error?: string;
  message?: string;
  onRetry?: () => void;
}) {
  const pending = message && /waiting|applying|starting|accepted|processing|still running|verifying|checking/i.test(message);
  const failed = message && /failed|could not|cannot|can't|not verified|locked|unavailable|invalid|no jobs are selected|not safe|missing|uncertain/i.test(message);
  return (
    <div className="operator-feedback-stack" aria-live="polite">
      {error ? (
        <OperatorNotice tone="error" title="Couldn't refresh JobSift" onRetry={onRetry}>
          <p>{error}</p>
          <p>This status check has not changed your delivery settings.</p>
        </OperatorNotice>
      ) : null}
      {message ? (
        <OperatorNotice
          tone={failed ? "error" : pending ? "info" : "success"}
          title={failed ? "Action needs attention" : pending ? "Working on it" : "Update from JobSift"}
        >
          <p>{message}</p>
          {pending && !failed ? <p>Wait for confirmation before starting another action.</p> : null}
        </OperatorNotice>
      ) : null}
    </div>
  );
}
