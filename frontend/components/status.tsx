import type { Decision } from "@/lib/contracts/service";
import { decisionLabel } from "@/lib/display";
export function MatchStatus({ decision }: { decision: Decision | undefined }) {
  return (
    <span className={`status ${decision ?? ""}`}>
      <span className="sr-only">Match decision: </span>
      {decision ? decisionLabel[decision] : "Not reported"}
    </span>
  );
}
