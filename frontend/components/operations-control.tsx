"use client";

import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

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

type ProfileSnapshot = {
  action: string;
  profile_id: string;
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
  client_funnel: FunnelSnapshot | null;
};

type OperatorSnapshot = {
  run: {
    id: number;
    run_number: number;
    status: string;
    conclusion: string | null;
    created_at: string;
    updated_at: string;
    url: string;
  } | null;
  profiles: ProfileSnapshot[];
};

type ControlStatus = {
  control_ready: boolean;
  inventory: WorkflowStatus;
  delivery: WorkflowStatus;
  operator_snapshot: OperatorSnapshot;
};

type ApiError = { error?: { message?: string } };

export type ManagedProfile = {
  profile_id: string;
  client_id: string;
  client_name: string;
  destination_id: string;
  destination_name: string;
};

type TargetMode = "listed" | "manual";

function validProfileId(value: string) {
  return /^[0-9a-f]{16}$/.test(value);
}

async function readJson(response: Response) {
  const body = (await response.json()) as ApiError & { data?: unknown };
  if (!response.ok) throw new Error(body.error?.message ?? "JobSift control request failed.");
  return body;
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

function deliveryConfirmation(
  operation: string,
  profileId: string,
  profileLabel: string,
  batchId: string,
) {
  const target = `${profileLabel} (${profileId})`;
  if (operation === "pause") {
    return `Pause ${target}? New deliveries for this profile will stop until you resume it.`;
  }
  if (operation === "release-batch") {
    return `Release batch ${batchId} for ${target}? This publishes the reviewed batch to the client's registered Sheet and counts it toward today's quota.`;
  }
  if (operation === "discard-batch") {
    return `Discard batch ${batchId} for ${target}? This permanently removes the unpublished prepared batch.`;
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
        {workflow ? (
          <a href={workflow.url} target="_blank" rel="noreferrer">
            Open in GitHub
          </a>
        ) : null}
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
                    <a href={run.url} target="_blank" rel="noreferrer">
                      #{run.run_number}
                    </a>
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
  const [statusError, setStatusError] = useState("");
  const [message, setMessage] = useState("");
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

  const latestSnapshot = status?.operator_snapshot;
  const pendingBatches =
    latestSnapshot?.profiles.filter(
      (profile) => profile.action === "awaiting_release" && Boolean(profile.batch_id),
    ) ?? [];
  const latestProfile = latestSnapshot?.profiles[0];

  function profileLabel(profileId: string) {
    const profile = profiles.find((item) => item.profile_id === profileId);
    return profile
      ? `${profile.client_name} — ${profile.destination_name}`
      : "Delivery profile";
  }

  const loadStatus = useCallback(async () => {
    try {
      const response = await fetch("/api/control/status", { cache: "no-store" });
      const body = (await readJson(response)) as { data: ControlStatus };
      setStatus(body.data);
      setStatusError("");
    } catch (error) {
      setStatusError(error instanceof Error ? error.message : "Could not load control status.");
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void fetch("/api/control/status", { cache: "no-store" })
      .then(readJson)
      .then((body) => {
        if (cancelled) return;
        setStatus((body as { data: ControlStatus }).data);
        setStatusError("");
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setStatusError(error instanceof Error ? error.message : "Could not load control status.");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function send(payload: Record<string, string>) {
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch("/api/control/dispatch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      await readJson(response);
      setMessage("Command accepted by GitHub Actions.");
      window.setTimeout(() => void loadStatus(), 1200);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Command failed.");
    } finally {
      setBusy(false);
    }
  }

  async function actOnPending(profile: ProfileSnapshot, operation: "release-batch" | "discard-batch") {
    if (!profile.batch_id) return;
    const label = profileLabel(profile.profile_id);
    const verb = operation === "release-batch" ? "Release" : "Discard";
    const effect =
      operation === "release-batch"
        ? "This sends the reviewed jobs to the client's Sheet."
        : "This removes the unpublished batch without sending it.";
    if (!window.confirm(`${verb} this batch for ${label}? ${effect}`)) return;
    await send({
      command: "client-control",
      operation,
      profile_id: profile.profile_id,
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
      setMessage("Choose a listed delivery profile or enter a valid profile ID.");
      return;
    }
    const batchId = String(data.get("batch_id") ?? "").trim();
    const profileSelect = event.currentTarget.elements.namedItem("profile_id") as HTMLSelectElement | null;
    const profileLabel =
      deliveryTargetMode === "manual"
        ? "Manual delivery profile"
        : profileSelect?.selectedOptions[0]?.textContent?.trim() ?? profileId;
    const confirmation = deliveryConfirmation(
      operation,
      profileId,
      profileLabel,
      batchId,
    );
    if (confirmation && !window.confirm(confirmation)) return;
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
      setMessage("Profile ID must be exactly 16 lowercase hexadecimal characters.");
      return;
    }
    const profileId =
      sheetTargetMode === "manual" ? overrideProfileId : selectedProfileId;
    if (!validProfileId(profileId)) {
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
      !window.confirm(
        `Disable ${profileLabel} (${profileId})? This pauses the profile and blocks new deliveries until the Sheet is verified, re-enabled, and the profile is resumed.`,
      )
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
      <section className="section-block reading operations-intro">
        <h2>What do you want to do?</h2>
        <p>
          Use the three sections below for normal JobSift operation. Technical tuning is
          still available under Advanced, but you do not need it for day-to-day use.
        </p>
        <nav className="operations-jump" aria-label="Operations sections">
          <a href="#find-jobs">Find Jobs</a>
          <a href="#clients-sheets">Clients &amp; Sheets</a>
          <a href="#system-status">System Status</a>
        </nav>
        {!status?.control_ready ? (
          <div className="notice">
            Production controls are currently unavailable. The server-only
            <code> JOBSIFT_GITHUB_TOKEN</code> must be configured before commands can run.
          </div>
        ) : (
          <div className="control-ready">Production controls are ready</div>
        )}
        {statusError ? <div className="error">{statusError}</div> : null}
        {message ? <p className="control-message">{message}</p> : null}
      </section>

      <section className="section-block operator-command-center" aria-labelledby="operator-now-title">
        <div className="control-heading">
          <div>
            <h2 id="operator-now-title">Right now</h2>
            <p>See what JobSift is waiting on before you run another command.</p>
          </div>
          <button type="button" disabled={busy} onClick={() => void loadStatus()}>
            Refresh status
          </button>
        </div>

        {latestSnapshot?.run ? (
          <div className="operator-run-strip">
            <div>
              <span className="metadata">Latest sourcing run</span>
              <strong>#{latestSnapshot.run.run_number}</strong>
            </div>
            <div>
              <span className="metadata">Result</span>
              <strong>
                {latestSnapshot.run.status === "completed"
                  ? latestSnapshot.run.conclusion ?? "completed"
                  : latestSnapshot.run.status}
              </strong>
            </div>
            <div>
              <span className="metadata">Finished</span>
              <strong>{new Date(latestSnapshot.run.updated_at).toLocaleString()}</strong>
            </div>
          </div>
        ) : (
          <p className="metadata">No production sourcing run has been reported yet.</p>
        )}

        {pendingBatches.length ? (
          <div className="operator-attention">
            <div>
              <p className="operator-eyebrow">Needs your decision</p>
              <h3>
                {pendingBatches.length === 1
                  ? `${countLabel(pendingBatches[0].selected_count)} job is waiting for approval`
                  : `${pendingBatches.length} review batches are waiting`}
              </h3>
              <p>
                JobSift will not prepare another batch for this client until you release or
                discard the pending batch.
              </p>
            </div>
            {pendingBatches.map((profile) => (
              <div className="operator-batch-card" key={profile.batch_id ?? profile.profile_id}>
                <div>
                  <strong>{profileLabel(profile.profile_id)}</strong>
                  <p className="metadata">
                    {countLabel(profile.selected_count)} selected · {countLabel(profile.shortfall)} short
                    of the requested limit
                  </p>
                </div>
                <div className="operator-decision-actions">
                  <button
                    type="button"
                    disabled={busy || !status?.control_ready}
                    onClick={() => void actOnPending(profile, "release-batch")}
                  >
                    Release {countLabel(profile.selected_count)} job
                  </button>
                  <button
                    type="button"
                    className="secondary-action"
                    disabled={busy || !status?.control_ready}
                    onClick={() => void actOnPending(profile, "discard-batch")}
                  >
                    Discard
                  </button>
                </div>
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
        ) : latestProfile ? (
          <div className="operator-clear">
            <strong>No review batch is blocking this client.</strong>
            <span>JobSift is free to prepare the next delivery batch.</span>
          </div>
        ) : null}

        {latestProfile?.client_funnel ? (
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
        ) : pendingBatches.length ? (
          <div className="notice">
            The latest client funnel is not available because the pending review batch stopped
            a new client evaluation. Handle the batch above, then run sourcing again.
          </div>
        ) : null}
      </section>

      <section className="section-block operations-section" id="find-jobs">
        <div className="control-heading">
          <div>
            <h2>Find Jobs</h2>
            <p>
              Start a production sourcing run now. JobSift keeps the same freshness,
              matching, dedupe, source-verification, and shared-inventory rules.
            </p>
          </div>
          <span className="operations-state">
            Automatic sourcing: {status?.inventory.state === "active" ? "On" : "Off"}
          </span>
        </div>

        <form className="operations-primary-action" onSubmit={submitInventory}>
          <input type="hidden" name="workday_targets" value="1" />
          <input type="hidden" name="workday_detail_concurrency" value="4" />
          <input type="hidden" name="yield_extra_budget" value="100" />
          <button type="submit" disabled={busy || !status?.control_ready}>
            {busy ? "Starting…" : "Run sourcing now"}
          </button>
          <p className="metadata">
            Safe default run. It does not change the scheduled crawl cursor.
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
                if (window.confirm("Pause scheduled inventory refreshes?")) {
                  void send({ command: "inventory-schedule-pause" });
                }
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
              <select name="workday_targets" defaultValue="1" disabled={busy || !status?.control_ready}>
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
                disabled={busy || !status?.control_ready}
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
                disabled={busy || !status?.control_ready}
              >
                {["0", "25", "50", "75", "100"].map((value) => (
                  <option key={value} value={value}>
                    {value === "0" ? "0 — fairness only" : value}
                  </option>
                ))}
              </select>
            </label>
            <button type="submit" disabled={busy || !status?.control_ready}>
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
                  disabled={busy || !status?.control_ready}
                />
              </label>
            )}
            <button
              type="submit"
              value="sheet-check"
              disabled={
                busy ||
                !status?.control_ready ||
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
                  disabled={busy || !status?.control_ready}
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
                disabled={busy || !status?.control_ready}
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
                  disabled={busy || !status?.control_ready}
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
                  max="5000"
                  defaultValue="100"
                  disabled={busy || !status?.control_ready}
                />
              </label>
            ) : null}

            {deliveryOperation === "set-mode" ? (
              <label>
                Delivery mode
                <select
                  name="delivery_mode"
                  defaultValue="review"
                  disabled={busy || !status?.control_ready}
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
                  disabled={busy || !status?.control_ready}
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
                  disabled={busy || !status?.control_ready}
                />
              </label>
            ) : null}

            <p className="operations-help">{deliveryOperationHelp(deliveryOperation)}</p>

            <button
              type="submit"
              disabled={
                busy ||
                !status?.control_ready ||
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
                  disabled={busy || !status?.control_ready || deliveryOperation === "list"}
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

      <section className="section-block operations-section" id="system-status">
        <h2>System Status</h2>
        <p>
          Use this section to confirm that production automation is running. You do not
          need to change anything here during normal operation.
        </p>
        <dl className="facts">
          <dt>Production commands</dt>
          <dd>{status?.control_ready ? "Ready" : "Unavailable"}</dd>
          <dt>Automatic sourcing</dt>
          <dd>{status?.inventory.state === "active" ? "On" : "Off"}</dd>
          <dt>Yield-aware scheduling</dt>
          <dd>On · 72-hour evidence window · 100 scheduled bonus targets</dd>
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
    </>
  );
}