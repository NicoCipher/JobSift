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

export function OperationsControl() {
  const [status, setStatus] = useState<ControlStatus | null>(null);
  const [statusError, setStatusError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

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
    if (
      ["pause", "release-batch", "discard-batch"].includes(operation) &&
      !window.confirm(`Confirm ${operation.replace("-", " ")}?`)
    ) {
      return;
    }
    await send({
      command: "client-control",
      operation,
      profile_id: String(data.get("profile_id") ?? ""),
      daily_quota: String(data.get("daily_quota") ?? "100"),
      delivery_mode: String(data.get("delivery_mode") ?? "review"),
      timezone: String(data.get("timezone") ?? "Africa/Lagos"),
      batch_id: String(data.get("batch_id") ?? ""),
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
            Profile ID
            <input
              name="profile_id"
              autoComplete="off"
              placeholder="Opaque profile ID"
              disabled={busy || !status?.control_ready}
            />
          </label>
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
