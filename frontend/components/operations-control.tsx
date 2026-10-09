"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { UiIcon } from "@/components/ui-icon";
import { readOperatorJson } from "@/lib/operator-response";
import { OperatorFeedback, OperatorNotice } from "@/components/operator-notice";
import { ExternalLink } from "@/components/external-link";
import { useOperatorConfirmation } from "@/components/operator-confirmation";
import { readRememberedFailure, rememberFailedOperation, clearRememberedFailure, type RememberedFailedOperation } from "@/lib/remembered-operator-failure";

type Run = {
  id: number;
  run_number: number;
  event: string;
  status: string;
  conclusion: string | null;
  created_at: string;
  updated_at: string;
  url: string;
  head_sha: string;
};

type WorkflowStatus = {
  name: string;
  state: string;
  url: string;
  runs: Run[];
};

type FunnelSnapshot = {
  overall: Record<string, number>;
  age_buckets: Record<string, number>;
  delivery: Record<string, number>;
};

type PendingItem = {
  ordinal: number;
  title: string;
  company: string;
  link: string | null;
  platform: string;
};

type ProfileSnapshot = {
  action: string;
  profile_id: string;
  profile_status: string | null;
  delivery_mode: string | null;
  daily_quota: number | null;
  sheet_status: string | null;
  delivered_today: number | null;
  batch_id: string | null;
  batch_status: string | null;
  requested_quota: number | null;
  selected_count: number | null;
  shortfall: number | null;
  fresh_eligible_employers: number | null;
  match_eligible_postings: number | null;
  needs_review_postings: number | null;
  selection_eligible_postings: number | null;
  stale_posting_suppressed_groups: number | null;
  company_cap_suppressed_groups: number | null;
  pending_items: PendingItem[];
  pending_items_truncated: boolean;
  recovery_required: boolean;
  client_funnel: FunnelSnapshot | null;
  operator_managed: boolean;
  client_name: string | null;
  destination_name: string | null;
  sheet_handle: string | null;
  control_capability: string | null;
};

type OperatorSnapshot = {
  complete: boolean;
  observed_at: string | null;
  control_request_id: string | null;
  confirmed_control_request_id: string | null;
  confirmed_run?: {
    status: string;
    conclusion: string | null;
    run_number: number;
    url: string;
  } | null;
  state_error: string | null;
  run: {
    id: number;
    run_number: number;
    status: string;
    conclusion: string | null;
    created_at: string;
    updated_at: string;
    url: string;
    kind?: "inventory" | "delivery" | "configure" | "provision";
  } | null;
  profiles: ProfileSnapshot[];
  truncated: boolean;
};

type ControlStatus = {
  control_ready: boolean;
  inventory: WorkflowStatus;
  delivery: WorkflowStatus;
  operator_snapshot: OperatorSnapshot;
};


export type ManagedProfile = {
  profile_id: string;
  client_name: string;
  destination_name: string;
};

type TargetMode = "listed" | "manual";

function validControlRequestId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
      value,
    )
  );
}

function hasConfirmedControlRequest(
  snapshot: OperatorSnapshot,
  requestId: string,
) {
  return (
    snapshot.complete === true &&
    !snapshot.truncated &&
    snapshot.confirmed_control_request_id === requestId
  );
}

function validProfileId(value: string) {
  return /^[0-9a-f]{16}$/.test(value);
}

async function readJson(response: Response) {
  return readOperatorJson(response, "JobSift control request failed.");
}
function runLabel(run: Run) {
  if (run.status !== "completed") return run.status;
  return run.conclusion ?? "completed";
}

function shortSha(value: string) {
  return value.slice(0, 8);
}

function deliveryOperationHelp(operation: string) {
  if (operation === "pause") return "Stop new deliveries for this client until you resume them.";
  if (operation === "resume") return "Allow this client to receive deliveries again.";
  if (operation === "set-quota") return "Change the maximum number of jobs this client can receive per day.";
  if (operation === "set-mode") return "Review keeps jobs waiting for approval. Auto sends eligible jobs automatically.";
  if (operation === "set-timezone") return "Change the timezone used for this client's daily quota window.";
  if (operation === "run-now") return "Run this client's delivery now using the jobs already in shared inventory.";
  if (operation === "release-batch") return "Publish an approved review batch to the client's registered Sheet.";
  if (operation === "discard-batch") return "Delete an unpublished review batch.";
  if (operation === "list") return "Show the delivery profiles JobSift currently knows about.";
  return "Check this client's current delivery state.";
}

function deliveryOperationButton(operation: string) {
  if (operation === "pause") return "Pause delivery";
  if (operation === "resume") return "Resume delivery";
  if (operation === "set-quota") return "Save daily limit";
  if (operation === "set-mode") return "Save delivery mode";
  if (operation === "set-timezone") return "Save timezone";
  if (operation === "run-now") return "Send jobs now";
  if (operation === "release-batch") return "Release batch";
  if (operation === "discard-batch") return "Discard batch";
  if (operation === "list") return "List clients";
  return "Check status";
}

function deliveryConfirmation(operation: string, profileLabel: string) {
  if (operation === "pause") {
    return "Pause deliveries to " + profileLabel + "? New jobs will wait until you resume deliveries. Already sent jobs stay in the Sheet.";
  }
  if (operation === "release-batch") {
    return "Send the current reviewed batch for " + profileLabel + "? JobSift will check the jobs and Sheet again before delivery.";
  }
  if (operation === "discard-batch") {
    return "Discard the waiting batch for " + profileLabel + "? Jobs in this unpublished batch will not be sent.";
  }
  return null;
}

function countLabel(value: number | null | undefined) {
  return value === null || value === undefined ? "—" : value.toLocaleString();
}

function FunnelSummary({ funnel }: { funnel: FunnelSnapshot }) {
  const stages = [
    ["Fresh ≤24h", funnel.overall.fresh_0_24h],
    ["Role title", funnel.overall.title_matched_0_24h],
    ["US market", funnel.overall.target_market_survived_0_24h],
    ["Remote", funnel.overall.remote_survived_0_24h],
    ["All brief rules", funnel.overall.other_rules_survived_0_24h],
    ["Confirmed", funnel.overall.confirmed_matches_0_24h],
    ["Needs review", funnel.overall.needs_review_matches_0_24h],
  ] as const;
  return (
    <div className="operator-funnel" aria-label="Latest client match funnel">
      {stages.map(([label, value]) => (
        <div className="operator-metric" key={label}>
          <strong>{countLabel(value)}</strong>
          <span>{label}</span>
        </div>
      ))}
    </div>
  );
}

function WorkflowRuns({
  title,
  workflow,
}: {
  title: string;
  workflow: WorkflowStatus | undefined;
}) {
  return (
    <section className="section-block">
      <div className="control-heading">
        <div>
          <h2>{title}</h2>
          <p className="metadata">
            {workflow ? `Workflow: ${workflow.state}` : "Workflow status unavailable."}
          </p>
        </div>
        {workflow ? <ExternalLink href={workflow.url}>Open in GitHub</ExternalLink> : null}
      </div>
      {workflow?.runs.length ? (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Run</th>
                <th scope="col">Trigger</th>
                <th scope="col">Result</th>
                <th scope="col">Started</th>
                <th scope="col">Commit</th>
              </tr>
            </thead>
            <tbody>
              {workflow.runs.map((run) => (
                <tr key={run.id}>
                  <td>
                    <ExternalLink href={run.url}>#{run.run_number}</ExternalLink>
                  </td>
                  <td>{run.event}</td>
                  <td>{runLabel(run)}</td>
                  <td>{new Date(run.created_at).toLocaleString()}</td>
                  <td>
                    <code>{shortSha(run.head_sha)}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p>No recent workflow runs were reported.</p>
      )}
    </section>
  );
}

export function OperationsControl({
  profiles,
  catalogueIncomplete = false,
}: {
  profiles: ManagedProfile[];
  catalogueIncomplete?: boolean;
}) {
  const [status, setStatus] = useState<ControlStatus | null>(null);
  const { ask, confirmationDialog } = useOperatorConfirmation();
  const [statusError, setStatusError] = useState("");
  const [message, setMessage] = useState("");
  const [messageTone, setMessageTone] = useState<"error" | "warning" | "info" | "success">("info");
  const [failedCommand, setFailedCommand] = useState<RememberedFailedOperation | null>(() => typeof window === "undefined" ? null : readRememberedFailure());
  const [busy, setBusy] = useState(false);
  const [sheetTargetMode, setSheetTargetMode] = useState<TargetMode>(
    profiles.length ? "listed" : "manual",
  );
  const [deliveryTargetMode, setDeliveryTargetMode] = useState<TargetMode>(
    profiles.length ? "listed" : "manual",
  );
  const [deliveryOperation, setDeliveryOperation] = useState("status");
  const [sheetProfileOverride, setSheetProfileOverride] = useState("");
  const [deliveryProfileOverride, setDeliveryProfileOverride] = useState("");
  const [awaitingFreshState, setAwaitingFreshState] = useState(false);
  const pendingControlRequestId = useRef<string | null>(null);
  const statePollTimer = useRef<number | null>(null);

  const latestSnapshot = status?.operator_snapshot;
  const stateVerified =
    status !== null &&
    !statusError &&
    !awaitingFreshState &&
    latestSnapshot?.complete === true &&
    latestSnapshot.truncated !== true;
  const pendingBatches = stateVerified
    ? latestSnapshot?.profiles.filter((profile) => Boolean(profile.batch_id)) ?? []
    : [];
  const latestProfile = stateVerified ? latestSnapshot?.profiles[0] : undefined;
  const verifiedSnapshot = stateVerified;
  const reportedClients = verifiedSnapshot ? latestSnapshot?.profiles.length ?? null : null;
  const sentToday =
    verifiedSnapshot &&
    latestSnapshot &&
    latestSnapshot.profiles.length > 0 &&
    latestSnapshot.profiles.every((profile) => typeof profile.delivered_today === "number")
      ? latestSnapshot.profiles.reduce((total, profile) => total + (profile.delivered_today ?? 0), 0)
      : null;
  const batchesNeedingAttention = verifiedSnapshot ? pendingBatches.length : null;
  const [showManualControls, setShowManualControls] = useState(false);
  const [showLastActivity, setShowLastActivity] = useState(false);

  // The overview describes only verified, reported facts. It never treats an
  // enabled schedule as proof that jobs were found or delivered successfully.
  const nextStep = (() => {
    if (!status && !statusError) {
      return { tone: "neutral", title: "Checking JobSift…", detail: "Reading the last reported state. No sourcing run is being started.", action: "none", label: "" };
    }
    if (statusError) {
      return { tone: "caution", title: "JobSift status is unavailable", detail: "I cannot safely tell you what has been delivered. Try checking again; no jobs will be sent.", action: "refresh", label: "Check again" };
    }
    if (!status?.control_ready) {
      return { tone: "caution", title: "Operator controls need attention", detail: "JobSift cannot safely accept commands. Review the connection details before doing anything else.", action: "controls", label: "View connection details" };
    }
    if (!verifiedSnapshot) {
      return { tone: "caution", title: "Waiting for a verified update", detail: "Client information is incomplete or a command is still being confirmed. Delivery controls stay locked.", action: "refresh", label: "Check status" };
    }
    if (pendingBatches.some((profile) => profile.recovery_required)) {
      return { tone: "caution", title: "A delivery needs safe recovery", detail: "JobSift reported an unfinished Sheet delivery. Inspect it before retrying so jobs are not sent twice.", action: "review", label: "Inspect delivery" };
    }
    if (failedCommand) {
      return {
        tone: "caution",
        title: "Your requested operation failed",
        detail: "JobSift confirmed that your command failed. A newer unrelated successful run does not clear this failure. Check the failed operation and the client state before trying again.",
        action: "failed-command",
        label: "View failed operation",
      };
    }
    if (latestSnapshot?.run?.status === "completed" && latestSnapshot.run.conclusion === "failure") {
      return { tone: "caution", title: "The last reported operation failed", detail: "Open the recorded activity to see what failed before deciding whether to try again.", action: "activity", label: "View last activity" };
    }
    if (latestSnapshot?.profiles.some((profile) => profile.sheet_status !== "ready")) {
      return { tone: "caution", title: "A client's Google Sheet needs attention", detail: "At least one client's Sheet is not confirmed ready. Check that connection before expecting new deliveries.", action: "link", label: "Check client Sheets", href: "/clients" };
    }
    if (latestSnapshot?.profiles.some((profile) => profile.profile_status === "paused")) {
      return { tone: "attention", title: "A client is paused", detail: "At least one client cannot receive new deliveries yet. Check which client is paused before reviewing or sending more jobs.", action: "link", label: "View paused clients", href: "/clients" };
    }
    if (pendingBatches.length) {
      return { tone: "attention", title: "Jobs are waiting for your decision", detail: "Review the prepared jobs below. Only your approval can release a review-mode batch.", action: "review", label: "Review waiting jobs" };
    }
    if (latestSnapshot?.profiles.length === 0) {
      return { tone: "neutral", title: "No client delivery state reported yet", detail: "Set up a client or check its status. JobSift will not invent results.", action: "link", label: "Open clients", href: "/clients" };
    }
    if (status.inventory.state !== "active") {
      return { tone: "caution", title: "Automatic sourcing is not confirmed active", detail: "The reported sourcing workflow is not active. Check the schedule before expecting new jobs.", action: "controls", label: "View sourcing controls" };
    }
    return { tone: "neutral", title: "No approval is needed right now", detail: "No pending batches were reported and scheduled sourcing is enabled. This does not guarantee that new jobs were found or sent.", action: "link", label: "View clients", href: "/clients" };
  })();

  function profileLabel(profileId: string) {
    const snapshot = latestSnapshot?.profiles.find(
      (item) => item.profile_id === profileId,
    );
    if (
      snapshot?.operator_managed &&
      snapshot.client_name &&
      snapshot.destination_name
    ) {
      return `${snapshot.client_name} — ${snapshot.destination_name}`;
    }
    const profile = profiles.find((item) => item.profile_id === profileId);
    return profile ? `${profile.client_name} — ${profile.destination_name}` : null;
  }

  const loadStatus = useCallback(async () => {
    try {
      const pendingId = pendingControlRequestId.current;
      const remembered = readRememberedFailure();
      const requestId = pendingId ?? remembered?.requestId ?? null;
      const statusUrl = requestId
        ? `/api/control/status?control_request_id=${encodeURIComponent(requestId)}`
        : "/api/control/status";
      const response = await fetch(statusUrl, { cache: "no-store" });
      const body = (await readJson(response)) as { data: ControlStatus };
      setStatus(body.data);
      setStatusError("");
      const confirmedRequestId = pendingControlRequestId.current;
      if (
        confirmedRequestId &&
        hasConfirmedControlRequest(
          body.data.operator_snapshot,
          confirmedRequestId,
        )
      ) {
        pendingControlRequestId.current = null;
        setAwaitingFreshState(false);
        const run = body.data.operator_snapshot.confirmed_run;
        if (run?.status === "completed" && run.conclusion === "success") {
          setFailedCommand(null);
          clearRememberedFailure();
          setMessageTone("success");
          setMessage("JobSift confirmed the successful operation. You can continue.");
        } else if (run?.status === "completed" && run.conclusion === "failure") {
          const failure = { requestId: confirmedRequestId, runNumber: run.run_number, url: run.url };
          setFailedCommand(failure);
          rememberFailedOperation(failure);
          setMessageTone("error");
          setMessage("JobSift reported a failed operation. Check the recorded activity and current client state before trying again.");
        } else {
          setMessageTone("warning");
          setMessage("The latest state was received, but the operation result is not confirmed. Check activity before repeating any delivery.");
        }
      } else if (remembered && hasConfirmedControlRequest(body.data.operator_snapshot, remembered.requestId)) {
        // An unrelated newer workflow is never evidence that this command succeeded.
        const correlated = body.data.operator_snapshot.confirmed_run;
        if (correlated?.status === "completed" && correlated.conclusion === "failure") {
          const failure = { requestId: remembered.requestId, runNumber: correlated.run_number, url: correlated.url };
          setFailedCommand(failure);
          rememberFailedOperation(failure);
        }
        // If historical correlation is missing or inconclusive, keep the warning.
        // Only a newly requested command's separately confirmed success clears it.
      }
    } catch (error) {
      setStatusError(error instanceof Error ? error.message : "Could not load control status.");
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    const remembered = readRememberedFailure();
    const query = remembered
      ? "?control_request_id=" + encodeURIComponent(remembered.requestId)
      : "";
    void fetch("/api/control/status" + query, { cache: "no-store" })
      .then(readJson)
      .then((body) => {
        if (cancelled) return;
        const data = (body as { data: ControlStatus }).data;
        setStatus(data);
        setStatusError("");
        if (remembered && hasConfirmedControlRequest(data.operator_snapshot, remembered.requestId)) {
          const run = data.operator_snapshot.confirmed_run;
          if (run?.status === "completed" && run.conclusion === "failure") {
            const failure = { requestId: remembered.requestId, runNumber: run.run_number, url: run.url };
            setFailedCommand(failure);
            rememberFailedOperation(failure);
          }
          // Neither a missing correlation nor another run's success clears this warning.
        }
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setStatusError(error instanceof Error ? error.message : "Could not load control status.");
      });
    return () => {
      cancelled = true;
      if (statePollTimer.current !== null) {
        window.clearTimeout(statePollTimer.current);
      }
    };
  }, []);

  function pollForFreshState(attempt = 0) {
    if (statePollTimer.current !== null) {
      window.clearTimeout(statePollTimer.current);
    }
    const delay = attempt < 8 ? 1500 : 5000;
    statePollTimer.current = window.setTimeout(async () => {
      await loadStatus();
      if (pendingControlRequestId.current && attempt < 60) {
        pollForFreshState(attempt + 1);
      }
    }, delay);
  }

  async function send(
    payload: Record<string, string>,
    options: { waitForState?: boolean } = {},
  ) {
    const needsVerifiedState =
      payload.command === "inventory-refresh" ||
      (payload.command === "client-control" && payload.operation !== "list");
    const waitForState = needsVerifiedState || options.waitForState === true;
    if (needsVerifiedState && !stateVerified) {
      setMessageTone("error");
      setMessage(
        "Current client state is not verified. Refresh status and wait for the active JobSift operation to finish before changing delivery.",
      );
      return;
    }
    setBusy(true);
    setMessageTone("info");
    setMessage("");
    try {
      const response = await fetch("/api/control/dispatch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const result = (await readJson(response)) as {
        data?: { control_request_id?: unknown };
      };
      if (waitForState) {
        setAwaitingFreshState(true);
        const requestId = result.data?.control_request_id;
        if (!validControlRequestId(requestId)) {
          pendingControlRequestId.current = null;
          setMessageTone("warning");
          setMessage(
            "Command accepted, but JobSift could not correlate its confirmation. Controls stay locked until you reload after the workflow finishes.",
          );
        } else {
          pendingControlRequestId.current = requestId.toLowerCase();
          setMessage("Command accepted. Waiting for JobSift to confirm the new state…");
          pollForFreshState();
        }
      } else {
        setMessage("Command accepted by GitHub Actions.");
      }
    } catch (error) {
      setMessageTone("error");
      setMessage(error instanceof Error ? error.message : "Command failed.");
    } finally {
      setBusy(false);
    }
  }

  async function actOnProfile(
    profile: ProfileSnapshot,
    operation: "run-now" | "pause" | "resume" | "sheet-check",
  ) {
    const label = profileLabel(profile.profile_id);
    if (!label) {
      setMessageTone("error");
      setMessage("Client name is unavailable, so JobSift will not run this client action.");
      return;
    }
    if (
      operation === "pause" &&
      !(await ask({ title: "Pause client deliveries?", description: `New deliveries for ${label} will stop until you resume them. Existing Sheet entries will not be removed.`, confirmLabel: "Pause deliveries", tone: "danger" }))
    ) {
      return;
    }
    await send({
      command: "client-control",
      operation,
      profile_id: profile.profile_id,
      profile_capability: profile.control_capability ?? "",
      daily_quota: String(profile.daily_quota ?? 100),
      delivery_mode: profile.delivery_mode ?? "review",
      timezone: "Africa/Lagos",
      batch_id: "",
    });
  }

  async function actOnPending(profile: ProfileSnapshot, operation: "release-batch" | "discard-batch") {
    if (!profile.batch_id) return;
    const label = profileLabel(profile.profile_id);
    if (!label) {
      setMessageTone("error");
      setMessage("Client name is unavailable, so JobSift will not allow an irreversible batch action.");
      return;
    }
    const verb = operation === "release-batch" ? "Release" : "Discard";
    const effect =
      operation === "release-batch"
        ? "This sends the reviewed jobs to the client's Sheet."
        : "This removes the unpublished batch without sending it.";
    if (!(await ask({ title: `${verb} jobs for ${label}?`, description: effect, confirmLabel: operation === "release-batch" ? "Send to Sheet" : "Discard prepared jobs", tone: operation === "discard-batch" ? "danger" : "primary" }))) return;
    await send({
      command: "client-control",
      operation,
      profile_id: profile.profile_id,
      profile_capability: profile.control_capability ?? "",
      daily_quota: "100",
      delivery_mode: "review",
      timezone: "Africa/Lagos",
      batch_id: profile.batch_id,
    });
  }

  async function submitInventory(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    await send({
      command: "inventory-refresh",
      workday_targets: String(data.get("workday_targets") ?? "1"),
      workday_detail_concurrency: String(data.get("workday_detail_concurrency") ?? "4"),
      yield_extra_budget: String(data.get("yield_extra_budget") ?? "100"),
    });
  }

  async function submitDelivery(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const operation = String(data.get("operation") ?? "status");
    const selectedProfileId = String(data.get("profile_id") ?? "").trim();
    const overrideProfileId = String(
      data.get("profile_id_override") ?? "",
    ).trim();
    if (
      operation !== "list" &&
      deliveryTargetMode === "manual" &&
      !validProfileId(overrideProfileId)
    ) {
      setMessageTone("error");
      setMessage("Profile ID must be exactly 16 lowercase hexadecimal characters.");
      return;
    }
    const profileId =
      operation === "list"
        ? ""
        : deliveryTargetMode === "manual"
          ? overrideProfileId
          : selectedProfileId;
    if (operation !== "list" && !validProfileId(profileId)) {
      setMessageTone("error");
      setMessage("Choose a listed delivery profile or enter a valid profile ID.");
      return;
    }
    const batchId = String(data.get("batch_id") ?? "").trim();
    const profileSelect = event.currentTarget.elements.namedItem("profile_id") as HTMLSelectElement | null;
    const profileLabel =
      deliveryTargetMode === "manual"
        ? "Manual delivery profile"
        : profileSelect?.selectedOptions[0]?.textContent?.trim() ?? profileId;
    const confirmation = deliveryConfirmation(operation, profileLabel);
    if (confirmation && !(await ask({title: "Confirm delivery action", description: confirmation, confirmLabel: deliveryOperationButton(operation), tone: operation === "discard-batch" || operation === "pause" ? "danger" : "primary"}))) return;
    await send({
      command: "client-control",
      operation,
      profile_id: profileId,
      daily_quota: String(data.get("daily_quota") ?? "100"),
      delivery_mode: String(data.get("delivery_mode") ?? "review"),
      timezone: String(data.get("timezone") ?? "Africa/Lagos"),
      batch_id: batchId,
    });
  }

  async function submitSheetControl(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const submitter = (event.nativeEvent as SubmitEvent).submitter as HTMLButtonElement | null;
    const operation = submitter?.value ?? "sheet-check";
    const selectedProfileId = String(
      data.get("sheet_profile_id") ?? "",
    ).trim();
    const overrideProfileId = String(
      data.get("sheet_profile_id_override") ?? "",
    ).trim();
    if (sheetTargetMode === "manual" && !validProfileId(overrideProfileId)) {
      setMessageTone("error");
      setMessage("Profile ID must be exactly 16 lowercase hexadecimal characters.");
      return;
    }
    const profileId =
      sheetTargetMode === "manual" ? overrideProfileId : selectedProfileId;
    if (!validProfileId(profileId)) {
      setMessageTone("error");
      setMessage("Choose a listed client Sheet or enter a valid profile ID.");
      return;
    }
    const profileSelect = event.currentTarget.elements.namedItem(
      "sheet_profile_id",
    ) as HTMLSelectElement | null;
    const profileLabel =
      sheetTargetMode === "manual"
        ? "Manual delivery profile"
        : profileSelect?.selectedOptions[0]?.textContent?.trim() ?? profileId;
    if (
      operation === "sheet-disable" &&
      !(await ask({ title: "Disable this Google Sheet?", description: `This pauses ${profileLabel} and blocks new deliveries until the Sheet is verified, re-enabled and the client is resumed. No rows are deleted.`, confirmLabel: "Disable Sheet", tone: "danger" }))
    ) {
      return;
    }
    await send({
      command: "client-control",
      operation,
      profile_id: profileId,
      daily_quota: "100",
      delivery_mode: "review",
      timezone: "Africa/Lagos",
      batch_id: "",
    });
  }

  return (
    <>
      {confirmationDialog}
      <section
        className="section-block operator-driving-home"
        data-urgency={nextStep.tone}
        aria-labelledby="driving-home-title"
      >
        <div className="operator-hero-heading">
          <span className="operator-hero-emblem">
            <UiIcon name={nextStep.tone === "caution" ? "alert" : nextStep.tone === "attention" ? "bolt" : "sparkles"} size={27} />
          </span>
          <p className="operator-eyebrow">
            {nextStep.tone === "caution"
              ? "NEEDS A CHECK"
              : nextStep.tone === "attention"
                ? "NEEDS YOUR ATTENTION"
                : "TODAY AT A GLANCE"}
          </p>
          <span className="hero-state-badge">
            <UiIcon name={nextStep.tone === "caution" ? "alert" : "shield"} size={15}/>
            {nextStep.tone === "caution" ? "Action needed" : nextStep.tone === "attention" ? "Your decision" : "Status overview"}
          </span>
        </div>
        <h2 id="driving-home-title" aria-live="polite">{nextStep.title}</h2>
        <p className="operator-driving-description">{nextStep.detail}</p>
        <div className="operator-driving-actions">
          {nextStep.action === "refresh" ? (
            <button type="button" disabled={busy} onClick={() => void loadStatus()}>
              {nextStep.label} <UiIcon name="refresh" size={17} />
            </button>
          ) : nextStep.action === "controls" ? (
            <a
              className="operator-main-link"
              href="#system-status"
              onClick={() => {
                setShowManualControls(true);
                window.requestAnimationFrame(() => {
                  const details = document.getElementById("system-status");
                  details?.scrollIntoView({ block: "start" });
                  details?.focus();
                });
              }}
            >
              {nextStep.label} <UiIcon name="arrow-right" size={17} />
            </a>
          ) : nextStep.action === "failed-command" ? (
            <ExternalLink className="operator-main-link" href={failedCommand?.url}>
              {nextStep.label}
            </ExternalLink>
          ) : nextStep.action === "activity" ? (
            <a className="operator-main-link" href="#last-activity" onClick={() => setShowLastActivity(true)}>
              {nextStep.label} <UiIcon name="arrow-right" size={17} />
            </a>
          ) : nextStep.action === "review" ? (
            <a className="operator-main-link" href="#review-queue">{nextStep.label} <UiIcon name="arrow-right" size={17} /></a>
          ) : nextStep.action === "link" && "href" in nextStep ? (
            <a className="operator-main-link" href={nextStep.href}>{nextStep.label} <UiIcon name="arrow-right" size={17} /></a>
          ) : null}
          {nextStep.action !== "refresh" ? (
            <button type="button" className="secondary-action" disabled={busy} onClick={() => void loadStatus()}>
              <UiIcon name="refresh" size={17} /> Refresh status
            </button>
          ) : null}
        </div>
        <dl className="operator-driving-metrics" aria-label="Verified client summary">
          <div><dt><UiIcon name="users" size={17} /> Delivery setups</dt><dd>{countLabel(reportedClients)}</dd></div>
          <div><dt><UiIcon name="send" size={17} /> Jobs sent today</dt><dd>{countLabel(sentToday)}</dd></div>
          <div><dt><UiIcon name="review" size={17} /> Reviews to handle</dt><dd>{countLabel(batchesNeedingAttention)}</dd></div>
        </dl>
        <p className="metadata">
          {verifiedSnapshot && latestSnapshot?.observed_at && !Number.isNaN(Date.parse(latestSnapshot.observed_at))
            ? `Last verified update: ${new Date(latestSnapshot.observed_at).toLocaleString()}.`
            : "Last verified update: not available."}
          {" "}A dash means JobSift cannot confirm that figure.
        </p>
        <OperatorFeedback error={statusError} message={message} messageTone={messageTone} onRetry={() => void loadStatus()} />
      </section>

      <section className="section-block operator-command-center" id="review-queue" aria-labelledby="operator-now-title">
        <div className="control-heading">
          <div>
            <h2 id="operator-now-title"><UiIcon name="bolt" size={23} /> Your next steps</h2>
            <p>Only the decisions that actually need you appear here.</p>
          </div>
        </div>

        {latestSnapshot?.complete === false ? (
          <div className="operator-state-recovery">
            <OperatorNotice tone="error" title="Client state could not be verified">
            <div>
              <strong>Client state could not be verified.</strong>{" "}
              {latestSnapshot.state_error ??
                "JobSift has locked state-dependent controls until a fresh authoritative snapshot is available."}
            </div>
            </OperatorNotice>
            <details className="operations-advanced">
              <summary>Advanced recovery (may use Neon)</summary>
              <p>Only try this when database usage is available. It requests a new authoritative client snapshot.</p>
              <button
                type="button"
                disabled={busy || awaitingFreshState || !status?.control_ready}
                onClick={() =>
                  void send(
                    {
                      command: "client-control",
                      operation: "list",
                      profile_id: "",
                      daily_quota: "100",
                      delivery_mode: "review",
                      timezone: "Africa/Lagos",
                      batch_id: "",
                    },
                    { waitForState: true },
                  )
                }
              >
                {awaitingFreshState ? "Syncing…" : "Request client snapshot"}
              </button>
            </details>
          </div>
        ) : latestSnapshot?.truncated ? (
          <div className="notice" role="status">
            <strong>Client state is incomplete.</strong> JobSift has locked client actions until
            the full state can be verified.
          </div>
        ) : null}

        {latestSnapshot?.run?.status === "completed" && latestSnapshot.run.conclusion === "failure" ? (
        <details
          className="operator-optional"
          id="last-activity"
          open={showLastActivity}
          onToggle={(event) => setShowLastActivity(event.currentTarget.open)}
        >
          <summary>Last recorded activity</summary>
        {latestSnapshot?.run ? (
          <div className="operator-run-strip">
            <div>
              <span className="metadata">Latest state update</span>
              <strong>#{latestSnapshot.run.run_number}</strong>
              {latestSnapshot.run.url ? (
                <ExternalLink href={latestSnapshot.run.url}>Open full activity details</ExternalLink>
              ) : (
                <span className="metadata">Full activity link unavailable</span>
              )}
            </div>
            <div>
              <span className="metadata">Result</span>
              <strong>
                {latestSnapshot.run.status === "completed"
                  ? latestSnapshot.run.conclusion ?? "completed"
                  : latestSnapshot.run.status}
              </strong>
              {latestSnapshot.run.kind ? (
                <span className="metadata">
                  {latestSnapshot.run.kind === "inventory"
                    ? "Sourcing"
                    : latestSnapshot.run.kind === "delivery"
                      ? "Delivery"
                      : latestSnapshot.run.kind === "provision"
                        ? "Client onboarding"
                        : "Client setup"}
                </span>
              ) : null}
            </div>
            <div>
              <span className="metadata">Finished</span>
              <strong>{new Date(latestSnapshot.run.updated_at).toLocaleString()}</strong>
            </div>
          </div>
        ) : (
          <p className="metadata">No production sourcing run has been reported yet.</p>
        )}

        </details>

        ) : null}

        {pendingBatches.length ? (
          <div className="operator-attention">
            <div>
              <p className="operator-eyebrow">
                {pendingBatches.some((profile) => profile.recovery_required)
                  ? "Delivery needs attention"
                  : "Needs your decision"}
              </p>
              <h3>
                {pendingBatches.some((profile) => profile.recovery_required)
                  ? "A Sheet delivery needs safe recovery"
                  : pendingBatches.length === 1
                    ? `${countLabel(pendingBatches[0].selected_count)} ${pendingBatches[0].selected_count === 1 ? "job is" : "jobs are"} waiting for approval`
                    : `${pendingBatches.length} review batches are waiting`}
              </h3>
              <p>
                {pendingBatches.some((profile) => profile.recovery_required)
                  ? "JobSift detected an unfinished delivery journal. Do not start another batch; use the safe retry below."
                  : "JobSift will not prepare another batch for this client until you release or discard the pending batch."}
              </p>
            </div>
            {pendingBatches.map((profile) => (
              <div className="operator-batch-card" key={profile.batch_id ?? profile.profile_id}>
                <div>
                  <strong>{profileLabel(profile.profile_id) ?? "Client name unavailable"}</strong>
                  <p className="metadata">
                    {countLabel(profile.selected_count)} selected · {countLabel(profile.shortfall)} short
                    of the requested limit
                  </p>
                </div>
                {profile.pending_items.length ? (
                  <div className="operator-review-list" aria-label="Jobs waiting for approval">
                    {profile.pending_items.map((item) => (
                      <article className="operator-review-item" key={item.ordinal}>
                        <div>
                          <strong>{item.title}</strong>
                          <p className="metadata">
                            {item.company} · {item.platform}
                          </p>
                        </div>
                        {item.link ? (
                          <ExternalLink href={item.link}>Open job</ExternalLink>
                        ) : (
                          <span className="metadata">Job link unavailable</span>
                        )}
                      </article>
                    ))}
                    {profile.pending_items_truncated ? (
                      <p className="metadata">
                        Showing the first {profile.pending_items.length} of{" "}
                        {countLabel(profile.selected_count)} jobs in this batch.
                      </p>
                    ) : null}
                  </div>
                ) : null}
                <div className="operator-decision-actions">
                  <button
                    type="button"
                    disabled={
                      busy ||
                      !status?.control_ready ||
                      !stateVerified ||
                      !profileLabel(profile.profile_id)
                    }
                    onClick={() => void actOnPending(profile, "release-batch")}
                  >
                    {profile.recovery_required
                      ? "Retry safe delivery"
                      : `Release ${countLabel(profile.selected_count)} ${profile.selected_count === 1 ? "job" : "jobs"}`}
                  </button>
                  <button
                    type="button"
                    className="secondary-action"
                    disabled={
                      busy ||
                      !status?.control_ready ||
                      !stateVerified ||
                      !profileLabel(profile.profile_id) ||
                      profile.recovery_required
                    }
                    onClick={() => void actOnPending(profile, "discard-batch")}
                  >
                    Discard
                  </button>
                </div>
                {profile.recovery_required ? (
                  <p className="notice">
                    Discard is locked because this batch has a delivery journal. Safe retry will
                    reconcile the Sheet before doing anything else.
                  </p>
                ) : !profileLabel(profile.profile_id) ? (
                  <p className="notice">
                    Batch actions are locked until this production profile can be matched to a client name.
                  </p>
                ) : null}
                <details>
                  <summary>Why only {countLabel(profile.selected_count)}?</summary>
                  <div className="operator-mini-grid">
                    <span>Matched <strong>{countLabel(profile.match_eligible_postings)}</strong></span>
                    <span>Needs review <strong>{countLabel(profile.needs_review_postings)}</strong></span>
                    <span>Stale before delivery <strong>{countLabel(profile.stale_posting_suppressed_groups)}</strong></span>
                    <span>Employer cap <strong>{countLabel(profile.company_cap_suppressed_groups)}</strong></span>
                  </div>
                </details>
              </div>
            ))}
          </div>
        ) : null}

        {latestSnapshot?.profiles.length ? (
          <details className="operator-optional">
            <summary>Manage clients ({latestSnapshot.profiles.length})</summary>
            <div className="operator-client-list" aria-label="Client delivery controls">
            <div className="control-heading">
              <div>
                <h3>Clients</h3>
                <p className="metadata">Normal daily controls without profile IDs.</p>
              </div>
            </div>
            {latestSnapshot.profiles.map((profile) => {
              const label = profileLabel(profile.profile_id);
              const locked =
                !label || busy || !status?.control_ready || !stateVerified;
              return (
                <article className="operator-client-card" key={profile.profile_id}>
                  <div className="operator-client-summary">
                    <div>
                      <strong>{label ?? "Client name unavailable"}</strong>
                      <p className="metadata">
                        {profile.profile_status ?? "Unknown"} · {profile.delivery_mode ?? "Unknown"} mode
                      </p>
                    </div>
                    <strong>
                      {countLabel(profile.delivered_today)}/{countLabel(profile.daily_quota)} sent today
                    </strong>
                  </div>
                  <div className="operator-mini-grid">
                    <span>Sheet <strong>{profile.sheet_status ?? "Unknown"}</strong></span>
                    <span>
                      Pending{" "}
                      <strong>{profile.batch_id ? countLabel(profile.selected_count) : "0"}</strong>
                    </span>
                  </div>
                  <div className="operator-decision-actions">
                    <button
                      type="button"
                      disabled={locked || profile.profile_status !== "active" || Boolean(profile.batch_id)}
                      onClick={() => void actOnProfile(profile, "run-now")}
                    >
                      Find matches for client
                    </button>
                    {profile.profile_status === "paused" ? (
                      <button
                        type="button"
                        className="secondary-action"
                        disabled={locked}
                        onClick={() => void actOnProfile(profile, "resume")}
                      >
                        Resume
                      </button>
                    ) : (
                      <button
                        type="button"
                        className="secondary-action"
                        disabled={locked}
                        onClick={() => void actOnProfile(profile, "pause")}
                      >
                        Pause
                      </button>
                    )}
                    <button
                      type="button"
                      className="secondary-action"
                      disabled={locked}
                      onClick={() => void actOnProfile(profile, "sheet-check")}
                    >
                      Check Sheet
                    </button>
                  </div>
                  {!label ? (
                    <p className="notice">
                      Client controls are locked until JobSift has a trustworthy display name.
                    </p>
                  ) : null}
                </article>
              );
            })}
            </div>
          </details>
        ) : null}

        {latestProfile?.client_funnel ? (
          <details className="operator-optional">
            <summary>Why fewer jobs matched</summary>
            <div className="operator-diagnostics">
            <div className="control-heading">
              <div>
                <h3>Latest client funnel</h3>
                <p className="metadata">
                  These are measured backend counts, not estimates.
                </p>
              </div>
            </div>
            <FunnelSummary funnel={latestProfile.client_funnel} />
            <div className="operator-age-grid">
              <span>0–24h matches <strong>{countLabel(latestProfile.client_funnel.age_buckets.age_0_24h)}</strong></span>
              <span>24–48h matches <strong>{countLabel(latestProfile.client_funnel.age_buckets.age_24_48h)}</strong></span>
              <span>48–72h matches <strong>{countLabel(latestProfile.client_funnel.age_buckets.age_48_72h)}</strong></span>
            </div>
            </div>
          </details>
        ) : null}
      </section>

      <details
        className="operator-manual-tools"
        id="manual-controls"
        open={showManualControls}
        onToggle={(event) => setShowManualControls(event.currentTarget.open)}
      >
        <summary>Manual controls and technical details</summary>
        <p className="metadata">Use these only when something needs attention. Manual sourcing can use your database and GitHub Actions limits.</p>
      <section className="section-block operations-section" id="find-jobs">
        <div className="control-heading">
          <div>
            <h2>Find Jobs</h2>
            <p>
              This is an exceptional manual action, not the normal daily workflow.
              It can use Neon and GitHub Actions capacity. JobSift still enforces
              freshness, matching and deduplication.
            </p>
          </div>
          <span className="operations-state">
            Automatic sourcing: {!status ? "Unknown" : status.inventory.state === "active" ? "Enabled" : "Not active"}
          </span>
        </div>

        <form className="operations-primary-action" onSubmit={submitInventory}>
          <input type="hidden" name="workday_targets" value="1" />
          <input type="hidden" name="workday_detail_concurrency" value="4" />
          <input type="hidden" name="yield_extra_budget" value="100" />
          <button
            type="submit"
            disabled={busy || !status?.control_ready || !stateVerified}
          >
            {busy ? "Starting…" : "Run sourcing now"}
          </button>
          <p className="metadata">
            Small preset, but not quota-free: it can use Neon and GitHub Actions. It does not change the scheduled crawl cursor.
          </p>
        </form>

        <details className="operations-advanced">
          <summary>Advanced sourcing controls</summary>
          <p className="metadata">
            Use these only when testing crawl capacity or changing the automatic schedule.
          </p>

          <h3>Automatic sourcing schedule</h3>
          <div className="control-actions">
            <button
              type="button"
              disabled={busy || !status?.control_ready || status?.inventory.state !== "active"}
              onClick={() => {
                void ask({
                  title: "Pause automatic sourcing?",
                  description: "JobSift will stop scheduled sourcing runs until you resume them. Existing jobs and client deliveries will not be deleted.",
                  confirmLabel: "Pause sourcing",
                  tone: "danger",
                }).then((confirmed) => {
                  if (confirmed) void send({ command: "inventory-schedule-pause" });
                });
              }}
            >
              Pause automatic sourcing
            </button>
            <button
              type="button"
              disabled={busy || !status?.control_ready || status?.inventory.state === "active"}
              onClick={() => void send({ command: "inventory-schedule-resume" })}
            >
              Resume automatic sourcing
            </button>
          </div>

          <h3>Custom sourcing run</h3>
          <form className="control-form" onSubmit={submitInventory}>
            <label>
              Workday targets
              <select name="workday_targets" defaultValue="1" disabled={busy || !status?.control_ready || !stateVerified}>
                {["1", "5", "10", "20", "25"].map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Workday detail concurrency
              <select
                name="workday_detail_concurrency"
                defaultValue="4"
                disabled={busy || !status?.control_ready || !stateVerified}
              >
                {["4", "6", "8"].map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Yield-aware bonus targets
              <select
                name="yield_extra_budget"
                defaultValue="100"
                disabled={busy || !status?.control_ready || !stateVerified}
              >
                {["0", "25", "50", "75", "100"].map((value) => (
                  <option key={value} value={value}>
                    {value === "0" ? "0 — fairness only" : value}
                  </option>
                ))}
              </select>
            </label>
            <button type="submit" disabled={busy || !status?.control_ready || !stateVerified}>
              {busy ? "Starting…" : "Run custom sourcing"}
            </button>
          </form>
        </details>
      </section>

      <section className="section-block operations-section" id="clients-sheets">
        <h2>Clients &amp; Sheets</h2>
        <p>
          Choose a client by name, then check their Sheet or control delivery. You normally
          do not need a profile ID.
        </p>

        {profiles.length === 0 ? (
          <div className="notice">
            No client profiles are listed yet. Advanced profile-ID targeting is available below.
          </div>
        ) : catalogueIncomplete ? (
          <div className="notice">
            Some client catalogue data could not be loaded. The listed clients may be incomplete.
          </div>
        ) : null}

        <div className="operations-client-block">
          <h3>Google Sheet</h3>
          <p className="metadata">
            Check whether the registered Sheet is usable, or disable/re-enable it safely.
          </p>
          <form className="control-form" onSubmit={submitSheetControl}>
            {sheetTargetMode === "listed" ? (
              <label>
                Client Sheet
                <select
                  name="sheet_profile_id"
                  defaultValue={profiles[0]?.profile_id ?? ""}
                  disabled={
                    busy ||
                    !status?.control_ready ||
                    !stateVerified ||
                    profiles.length === 0
                  }
                >
                  {profiles.length === 0 ? (
                    <option value="">No listed clients</option>
                  ) : (
                    profiles.map((profile) => (
                      <option key={profile.profile_id} value={profile.profile_id}>
                        {profile.client_name} — {profile.destination_name}
                      </option>
                    ))
                  )}
                </select>
              </label>
            ) : (
              <label>
                Profile ID
                <input
                  name="sheet_profile_id_override"
                  value={sheetProfileOverride}
                  onChange={(event) => setSheetProfileOverride(event.target.value)}
                  autoComplete="off"
                  inputMode="text"
                  pattern="[0-9a-f]{16}"
                  placeholder="16-character profile ID"
                  disabled={busy || !status?.control_ready || !stateVerified}
                />
              </label>
            )}
            <button
              type="submit"
              value="sheet-check"
              disabled={
                busy ||
                !status?.control_ready ||
                !stateVerified ||
                (sheetTargetMode === "listed"
                  ? profiles.length === 0
                  : !validProfileId(sheetProfileOverride.trim()))
              }
            >
              Check Sheet
            </button>
            <button
              type="submit"
              value="sheet-disable"
              disabled={
                busy ||
                !status?.control_ready ||
                !stateVerified ||
                (sheetTargetMode === "listed"
                  ? profiles.length === 0
                  : !validProfileId(sheetProfileOverride.trim()))
              }
            >
              Disable Sheet
            </button>
            <button
              type="submit"
              value="sheet-enable"
              disabled={
                busy ||
                !status?.control_ready ||
                !stateVerified ||
                (sheetTargetMode === "listed"
                  ? profiles.length === 0
                  : !validProfileId(sheetProfileOverride.trim()))
              }
            >
              Re-enable Sheet
            </button>

            <details className="operations-inline-advanced">
              <summary>Advanced target</summary>
              <label>
                Target by
                <select
                  value={sheetTargetMode}
                  onChange={(event) => {
                    const mode = event.target.value as TargetMode;
                    setSheetTargetMode(mode);
                    if (mode === "listed") setSheetProfileOverride("");
                  }}
                  disabled={busy || !status?.control_ready || !stateVerified}
                >
                  <option value="listed" disabled={profiles.length === 0}>
                    Client name
                  </option>
                  <option value="manual">Profile ID</option>
                </select>
              </label>
            </details>
          </form>
        </div>

        <div className="operations-client-block">
          <h3>Delivery</h3>
          <form className="control-form control-form-wide" onSubmit={submitDelivery}>
            <label>
              What do you want to do?
              <select
                name="operation"
                value={deliveryOperation}
                onChange={(event) => setDeliveryOperation(event.target.value)}
                disabled={busy || !status?.control_ready || !stateVerified}
              >
                <option value="status">Check delivery status</option>
                <option value="run-now">Send jobs now</option>
                <option value="pause">Pause delivery</option>
                <option value="resume">Resume delivery</option>
                <option value="set-quota">Change daily limit</option>
                <option value="set-mode">Change review/auto mode</option>
                <option value="release-batch">Release review batch</option>
                <option value="discard-batch">Discard review batch</option>
                <option value="list">List all clients</option>
                <option value="set-timezone">Change timezone</option>
              </select>
            </label>

            {deliveryOperation !== "list" && deliveryTargetMode === "listed" ? (
              <label>
                Delivery profile
                <select
                  name="profile_id"
                  defaultValue={profiles[0]?.profile_id ?? ""}
                  disabled={busy || !status?.control_ready || profiles.length === 0}
                >
                  {profiles.length === 0 ? (
                    <option value="">No listed clients</option>
                  ) : (
                    profiles.map((profile) => (
                      <option key={profile.profile_id} value={profile.profile_id}>
                        {profile.client_name} — {profile.destination_name}
                      </option>
                    ))
                  )}
                </select>
              </label>
            ) : deliveryOperation !== "list" ? (
              <label>
                Profile ID
                <input
                  name="profile_id_override"
                  value={deliveryProfileOverride}
                  onChange={(event) => setDeliveryProfileOverride(event.target.value)}
                  autoComplete="off"
                  inputMode="text"
                  pattern="[0-9a-f]{16}"
                  placeholder="16-character profile ID"
                  disabled={busy || !status?.control_ready || !stateVerified}
                />
              </label>
            ) : null}

            {deliveryOperation === "set-quota" ? (
              <label>
                Daily job limit
                <input
                  name="daily_quota"
                  type="number"
                  min="1"
                  max="2000"
                  defaultValue="100"
                  disabled={busy || !status?.control_ready || !stateVerified}
                />
              </label>
            ) : null}

            {deliveryOperation === "set-mode" ? (
              <label>
                Delivery mode
                <select
                  name="delivery_mode"
                  defaultValue="review"
                  disabled={busy || !status?.control_ready || !stateVerified}
                >
                  <option value="review">Review before sending</option>
                  <option value="auto">Send automatically</option>
                </select>
              </label>
            ) : null}

            {deliveryOperation === "set-timezone" ? (
              <label>
                Timezone
                <input
                  name="timezone"
                  defaultValue="Africa/Lagos"
                  autoComplete="off"
                  disabled={busy || !status?.control_ready || !stateVerified}
                />
              </label>
            ) : null}

            {deliveryOperation === "release-batch" || deliveryOperation === "discard-batch" ? (
              <label>
                Review batch ID
                <input
                  name="batch_id"
                  autoComplete="off"
                  placeholder="Batch ID"
                  disabled={busy || !status?.control_ready || !stateVerified}
                />
              </label>
            ) : null}

            <p className="operations-help">{deliveryOperationHelp(deliveryOperation)}</p>

            <button
              type="submit"
              disabled={
                busy ||
                !status?.control_ready ||
                !stateVerified ||
                (deliveryOperation !== "list" &&
                  (deliveryTargetMode === "listed"
                    ? profiles.length === 0
                    : !validProfileId(deliveryProfileOverride.trim())))
              }
            >
              {busy ? "Sending…" : deliveryOperationButton(deliveryOperation)}
            </button>

            <details className="operations-inline-advanced">
              <summary>Advanced target</summary>
              <label>
                Target client by
                <select
                  value={deliveryTargetMode}
                  onChange={(event) => {
                    const mode = event.target.value as TargetMode;
                    setDeliveryTargetMode(mode);
                    if (mode === "listed") setDeliveryProfileOverride("");
                  }}
                  disabled={
                    busy ||
                    !status?.control_ready ||
                    !stateVerified ||
                    deliveryOperation === "list"
                  }
                >
                  <option value="listed" disabled={profiles.length === 0}>
                    Client name
                  </option>
                  <option value="manual">Profile ID</option>
                </select>
              </label>
            </details>
          </form>
        </div>
      </section>

      <section className="section-block operations-section" id="system-status" tabIndex={-1}>
        <h2>System Status</h2>
        <p>
          Use this section to confirm that production automation is running. You do not
          need to change anything here during normal operation.
        </p>
        <dl className="facts">
          <dt>Production commands</dt>
          <dd>{!status || statusError ? "Unknown" : status.control_ready ? "Ready" : "Unavailable"}</dd>
          <dt>Automatic sourcing</dt>
          <dd>{!status || statusError ? "Unknown" : status.inventory.state === "active" ? "Enabled" : "Not active"}</dd>
          <dt>Yield-aware scheduling</dt>
          <dd>Configured: 72-hour evidence window and 100 bonus targets; runtime not verified here</dd>
          <dt>Freshness protection</dt>
          <dd>Only jobs at most 24 hours old can improve source yield</dd>
        </dl>

        <details className="operations-advanced">
          <summary>How source scheduling works</summary>
          <p>
            Oldest-due fairness stays protected first. Productive employer boards can get
            bonus crawl slots from spare capacity. Workday remains excluded from bonus
            targeting and uses its guarded ramp separately.
          </p>
        </details>
      </section>

      <WorkflowRuns title="Sourcing activity" workflow={status?.inventory} />
      <WorkflowRuns title="Client delivery activity" workflow={status?.delivery} />
      </details>
    </>
  );
}