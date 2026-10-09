import type { ReactNode } from "react";
import { UiIcon } from "@/components/ui-icon";

function safeHttpUrl(value: string | null | undefined) {
  if (!value) return null;
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.toString() : null;
  } catch {
    return null;
  }
}

/** Employer and workflow URLs are data, not authority to navigate to unsafe schemes. */
export function ExternalLink({
  href,
  children,
  className = "",
}: {
  href?: string | null;
  children: ReactNode;
  className?: string;
}) {
  const url = safeHttpUrl(href);
  if (!url) return <span className="operator-link-unavailable">Link unavailable</span>;
  return (
    <a className={"operator-external-link " + className} href={url}
      target="_blank" rel="noopener noreferrer" title="Opens in a new tab">
      <span>{children}</span>
      <UiIcon name="arrow-up-right" size={16} />
    </a>
  );
}
