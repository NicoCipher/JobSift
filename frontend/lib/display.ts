import type {
  ApplicationDestination,
  Decision,
  Fact,
  Metric,
  OutcomeSummary,
} from "./contracts/service";
export const decisionLabel: Record<Decision, string> = {
  strong_match: "Strong match",
  possible_match: "Possible",
  needs_review: "Needs review",
  reject: "Rejected",
};
export const outcomeLabels = {
  applied: "Applied",
  not_applied: "Not applied",
  unknown: "Unknown",
};
export function factText<T>(
  fact: Fact<T>,
  format: (value: T) => string = String,
): string {
  return fact.availability === "reported"
    ? format(fact.value)
    : fact.availability === "unknown"
      ? "Unknown"
      : "Not reported";
}
export const metricText = (metric: Metric) =>
  factText(metric, (value) => value.toLocaleString("en-US"));
export const outcomeText = (outcome: OutcomeSummary) =>
  outcome.resolution === "conflicting"
    ? "Conflicting evidence"
    : factText(outcome, (value) => outcomeLabels[value]);
export function applicationLabel(destination: ApplicationDestination) {
  return destination.application_url_kind === "direct_apply"
    ? "Open application"
    : destination.application_url_kind === "vacancy_page"
      ? "Open listing"
      : "Application URL unavailable";
}
/** Defense in depth only; does not synthesize or establish vacancy eligibility. */
export function safeExternalUrl(url: string | null): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    return ["https:", "http:"].includes(parsed.protocol) &&
      !parsed.username &&
      !parsed.password
      ? url
      : null;
  } catch {
    return null;
  }
}
export function isTyping(target: EventTarget | null): boolean {
  return (
    target instanceof Element &&
    ((target instanceof HTMLElement && target.isContentEditable) ||
      Boolean(target.closest('input, textarea, select, [role="textbox"]')))
  );
}
export function rowStep(key: string, index: number, count: number): number {
  return key === "j"
    ? Math.min(count - 1, index + 1)
    : key === "k"
      ? Math.max(0, index - 1)
      : index;
}
export function dateText(value: string | null) {
  return value
    ? new Date(value).toLocaleDateString("en-US", {
        month: "short",
        day: "numeric",
        timeZone: "UTC",
      })
    : "Not recorded";
}
