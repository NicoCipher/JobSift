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

type ControlStatus = {
  control_ready: boolean;
  inventory: WorkflowStatus;
  delivery: WorkflowStatus;
};

type ApiError = { error?: { message?: string } };

export type ManagedProfile = {
  profile_id: string;
  client_id: string;
  client_name: string;
  destination_id: string;
  destination_name: string;
};

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

export function OperationsControl({ profiles }: { profiles: ManagedProfile[] }) {
  const [status, setStatus] = useState<ControlStatus | null>(null);
  const [statusError, setStatusError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [sheetProfileOverride, setSheetProfileOverride] = useState("");
  const [deliveryProfileOverride, setDeliveryProfileOverride] = useState("");

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

  async function submitInventory(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    await send({
      command: "inventory-refresh",
      workday_targets: String(data.get("workday_targets") ?? "1"),
      workday_detail_concurrency: String(data.get("workday_detail_concurrency") ?? "4"),
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
    if (overrideProfileId && !/^[0-9a-f]{16}$/.test(overrideProfileId)) {
      setMessage("Profile ID must be exactly 16 lowercase hexadecimal characters.");
      return;
    }
    const profileId = overrideProfileId || selectedProfileId;
    const batchId = String(data.get("batch_id") ?? "").trim();
    const profileSelect = event.currentTarget.elements.namedItem("profile_id") as HTMLSelectElement | null;
    const profileLabel = overrideProfileId
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
    if (overrideProfileId && !/^[0-9a-f]{16}$/.test(overrideProfileId)) {
      setMessage("Profile ID must be exactly 16 lowercase hexadecimal characters.");
      return;
    }
    const profileId = overrideProfileId || selectedProfileId;
    const profileSelect = event.currentTarget.elements.namedItem(
      "sheet_profile_id",
    ) as HTMLSelectElement | null;
    const profileLabel = overrideProfileId
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
      <section className="section-block reading">
        <h2>Production control plane</h2>
        <p>
          These controls dispatch the same serialized GitHub Actions workflows used by
          JobSift production. They do not write Turso directly and cannot change matching,
          freshness, dedupe, Workday index-first, or Sheet-write safety rules.
        </p>
        {!status?.control_ready ? (
          <div className="notice">
            Status is available, but production commands are disabled until the
            server-only <code>JOBSIFT_GITHUB_TOKEN</code> is configured.
          </div>
        ) : (
          <div className="control-ready">Production commands enabled</div>
        )}
        {statusError ? <div className="error">{statusError}</div> : null}
        {message ? <p className="control-message">{message}</p> : null}
      </section>

      <section className="section-block">
        <h2>Scheduled inventory</h2>
        <p>
          Pausing disables future GitHub schedule triggers without changing the durable
          logical-cohort cursor. Resuming lets the scheduler catch up oldest-first.
          A run already in progress is not cancelled.
        </p>
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
            Pause schedule
          </button>
          <button
            type="button"
            disabled={busy || !status?.control_ready || status?.inventory.state === "active"}
            onClick={() => void send({ command: "inventory-schedule-resume" })}
          >
            Resume schedule
          </button>
        </div>
      </section>

      <section className="section-block">
        <h2>Run inventory refresh</h2>
        <p>
          Manual runs do not advance the scheduled logical-cohort cursor. Use small target
          counts for guarded tests; scheduled production remains 25 Workday targets/hour.
        </p>
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
            Detail concurrency
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
          <button type="submit" disabled={busy || !status?.control_ready}>
            {busy ? "Sending…" : "Run refresh"}
          </button>
        </form>
      </section>

      <section className="section-block">
        <h2>Client Sheets</h2>
        <p>
          Control the already-registered Google Sheet behind a delivery profile without
          exposing its spreadsheet ID or tab in public GitHub Actions. Re-enabling verifies
          the exact stored worksheet identity and header first; the profile remains paused
          until you explicitly resume it.
        </p>
        {profiles.length === 0 ? (
          <div className="notice">
            No registered client Sheets are visible in the authorized operator scope.
          </div>
        ) : null}
        <form className="control-form" onSubmit={submitSheetControl}>
          <label>
            Client Sheet
            <select
              name="sheet_profile_id"
              defaultValue={profiles[0]?.profile_id ?? ""}
              disabled={busy || !status?.control_ready || profiles.length === 0}
            >
              {profiles.length === 0 ? (
                <option value="">No registered client Sheets</option>
              ) : (
                profiles.map((profile) => (
                  <option key={profile.profile_id} value={profile.profile_id}>
                    {profile.client_name} — {profile.destination_name}
                  </option>
                ))
              )}
            </select>
          </label>
          <details>
            <summary>Use a profile ID instead</summary>
            <label>
              Sheet profile ID
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
            <p className="metadata">
              Use this only when the real delivery profile is not listed above.
            </p>
          </details>
          <button
            type="submit"
            value="sheet-check"
            disabled={
              busy ||
              !status?.control_ready ||
              (profiles.length === 0 && !/^[0-9a-f]{16}$/.test(sheetProfileOverride.trim()))
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
              (profiles.length === 0 && !/^[0-9a-f]{16}$/.test(sheetProfileOverride.trim()))
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
              (profiles.length === 0 && !/^[0-9a-f]{16}$/.test(sheetProfileOverride.trim()))
            }
          >
            Re-enable Sheet
          </button>
        </form>
      </section>

      <section className="section-block">
        <h2>Client delivery control</h2>
        <p>
          Uses the existing delivery mutation queue. Profile IDs are the opaque control IDs
          already emitted by JobSift profile setup/status.
        </p>
        <form className="control-form control-form-wide" onSubmit={submitDelivery}>
          <label>
            Operation
            <select name="operation" defaultValue="status" disabled={busy || !status?.control_ready}>
              <option value="list">List profiles</option>
              <option value="status">Profile status</option>
              <option value="pause">Pause profile</option>
              <option value="resume">Resume profile</option>
              <option value="set-quota">Set daily quota</option>
              <option value="set-mode">Set delivery mode</option>
              <option value="set-timezone">Set timezone</option>
              <option value="run-now">Run delivery now</option>
              <option value="release-batch">Release review batch</option>
              <option value="discard-batch">Discard review batch</option>
            </select>
          </label>
          <label>
            Delivery profile
            <select
              name="profile_id"
              defaultValue={profiles[0]?.profile_id ?? ""}
              disabled={busy || !status?.control_ready || profiles.length === 0}
            >
              {profiles.length === 0 ? (
                <option value="">No registered client Sheets</option>
              ) : (
                profiles.map((profile) => (
                  <option key={profile.profile_id} value={profile.profile_id}>
                    {profile.client_name} — {profile.destination_name}
                  </option>
                ))
              )}
            </select>
          </label>
          <details>
            <summary>Use a profile ID instead</summary>
            <label>
              Delivery profile ID
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
            <p className="metadata">
              Use this only when the real delivery profile is not listed above.
            </p>
          </details>
          <label>
            Daily quota
            <input
              name="daily_quota"
              type="number"
              min="1"
              max="5000"
              defaultValue="100"
              disabled={busy || !status?.control_ready}
            />
          </label>
          <label>
            Delivery mode
            <select name="delivery_mode" defaultValue="review" disabled={busy || !status?.control_ready}>
              <option value="review">Review</option>
              <option value="auto">Auto</option>
            </select>
          </label>
          <label>
            Timezone
            <input
              name="timezone"
              defaultValue="Africa/Lagos"
              autoComplete="off"
              disabled={busy || !status?.control_ready}
            />
          </label>
          <label>
            Batch ID
            <input
              name="batch_id"
              autoComplete="off"
              placeholder="For release/discard"
              disabled={busy || !status?.control_ready}
            />
          </label>
          <button type="submit" disabled={busy || !status?.control_ready}>
            {busy ? "Sending…" : "Apply control"}
          </button>
        </form>
      </section>

      <WorkflowRuns title="Inventory refresh activity" workflow={status?.inventory} />
      <WorkflowRuns title="Client control activity" workflow={status?.delivery} />
    </>
  );
}
