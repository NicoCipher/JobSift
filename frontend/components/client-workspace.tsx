"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ManagedProfile } from "@/components/operations-control";

type PendingItem = {
  ordinal: number;
  title: string;
  company: string;
  link: string | null;
  platform: string;
};

type FunnelSnapshot = {
  overall: Record<string, number>;
  age_buckets: Record<string, number>;
  delivery: Record<string, number>;
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
  pending_items: PendingItem[];
  pending_items_truncated: boolean;
  recovery_required: boolean;
  client_funnel: FunnelSnapshot | null;
  operator_managed: boolean;
  client_name: string | null;
  destination_name: string | null;
  sheet_url: string | null;
  control_capability: string | null;
};

type OperatorSnapshot = {
  complete: boolean;
  observed_at: string | null;
  confirmed_control_request_id: string | null;
  state_error: string | null;
  run: {
    run_number: number;
    status: string;
    conclusion: string | null;
    updated_at: string;
    kind?: "inventory" | "delivery" | "configure" | "provision";
  } | null;
  profiles: ProfileSnapshot[];
  truncated: boolean;
};

type ControlStatus = {
  control_ready: boolean;
  inventory: { state: string };
  operator_snapshot: OperatorSnapshot;
};

type ReviewItem = {
  ordinal: number;
  title: string;
  company: string;
  application_link: string | null;
  source: string;
  posted_at: string | null;
  age_hours: number | null;
  location: string | null;
  remote_status: string;
  decision: string;
  matched_reasons: string[];
  review_reasons: string[];
  evidence_verified: boolean;
  release_ready: boolean;
  warnings: string[];
};

type ReviewSnapshot = {
  observed_at: string;
  profile_id: string;
  batch_id: string;
  generation_id: string;
  batch_status: string;
  selected_count: number;
  requested_quota: number;
  freshness_limit_hours: number | null;
  safe_to_release: boolean;
  recovery_required: boolean;
  error: string | null;
  items: ReviewItem[];
};

type ReviewLoad = {
  state: "idle" | "queued" | "in_progress" | "ready" | "failed";
  requestId?: string;
  review?: ReviewSnapshot;
  message?: string;
};

type ApiError = { error?: { message?: string } };

function validControlRequestId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
      value,
    )
  );
}

async function readJson(response: Response) {
  const body = (await response.json()) as ApiError & { data?: unknown };
  if (!response.ok) {
    throw new Error(body.error?.message ?? "JobSift request failed.");
  }
  return body;
}

function count(value: number | null | undefined) {
  return value === null || value === undefined ? "—" : value.toLocaleString();
}

function clientAnchor(profile: ManagedProfile) {
  const value = `${profile.client_name}-${profile.destination_name}`
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
  return `client-${value || "workspace"}`;
}

function humanDecision(value: string) {
  if (value === "strong_match") return "Strong match";
  if (value === "possible_match") return "Possible match";
  if (value === "needs_review") return "Needs review";
  if (value === "reject") return "Rejected";
  return value.replaceAll("_", " ") || "Unknown";
}

function postingAge(item: ReviewItem) {
  if (item.age_hours === null) return "Age unknown";
  if (item.age_hours < 0) return "Posting time needs checking";
  if (item.age_hours < 1) return "Less than 1 hour old";
  if (item.age_hours < 24) return `${Math.floor(item.age_hours)}h old`;
  const days = Math.floor(item.age_hours / 24);
  const hours = Math.floor(item.age_hours % 24);
  return hours ? `${days}d ${hours}h old` : `${days}d old`;
}

function reasonText(reason: string) {
  const normalized = reason.replaceAll("_", " ").trim();
  return normalized ? normalized.charAt(0).toUpperCase() + normalized.slice(1) : "";
}

export function ClientWorkspace({
  profiles,
  surface,
}: {
  profiles: ManagedProfile[];
  surface: "clients" | "review";
}) {
  const [status, setStatus] = useState<ControlStatus | null>(null);
  const [statusError, setStatusError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [awaitingFreshState, setAwaitingFreshState] = useState(false);
  const [reviews, setReviews] = useState<Record<string, ReviewLoad>>({});
  const [removed, setRemoved] = useState<Record<string, number[]>>({});
  const pendingControlRequestId = useRef<string | null>(null);
  const statePollTimer = useRef<number | null>(null);
  const reviewPollTimer = useRef<number | null>(null);
  const mutationContext = useRef<{
    profileId?: string;
    expectedSent?: number;
    beforeDelivered?: number;
    clientName?: string;
  } | null>(null);

  const latestSnapshot = status?.operator_snapshot;
  const stateVerified =
    status !== null &&
    !statusError &&
    !awaitingFreshState &&
    latestSnapshot?.complete !== false &&
    latestSnapshot?.truncated !== true;

  const configured = useMemo(() => {
    const catalogue = new Map(
      profiles.map((profile) => [profile.profile_id, profile] as const),
    );
    for (const snapshot of latestSnapshot?.profiles ?? []) {
      if (
        snapshot.operator_managed &&
        snapshot.client_name &&
        snapshot.destination_name &&
        !catalogue.has(snapshot.profile_id)
      ) {
        catalogue.set(snapshot.profile_id, {
          profile_id: snapshot.profile_id,
          client_name: snapshot.client_name,
          destination_name: snapshot.destination_name,
        });
      }
    }
    return [...catalogue.values()].map((profile) => ({
      config: profile,
      state:
        latestSnapshot?.profiles.find(
          (snapshot) => snapshot.profile_id === profile.profile_id,
        ) ?? null,
    }));
  }, [profiles, latestSnapshot]);

  const pending = configured.filter(
    ({ state }) => Boolean(state?.batch_id) && state?.delivery_mode === "review",
  );

  const loadStatus = useCallback(async () => {
    try {
      const requestId = pendingControlRequestId.current;
      const url = requestId
        ? `/api/control/status?control_request_id=${encodeURIComponent(requestId)}`
        : "/api/control/status";
      const response = await fetch(url, { cache: "no-store" });
      const body = (await readJson(response)) as { data: ControlStatus };
      setStatus(body.data);
      setStatusError("");
      if (
        requestId &&
        body.data.operator_snapshot.complete === true &&
        !body.data.operator_snapshot.truncated &&
        body.data.operator_snapshot.confirmed_control_request_id === requestId
      ) {
        pendingControlRequestId.current = null;
        setAwaitingFreshState(false);
        const context = mutationContext.current;
        mutationContext.current = null;
        if (context?.expectedSent && context.profileId) {
          const next = body.data.operator_snapshot.profiles.find(
            (profile) => profile.profile_id === context.profileId,
          );
          const delivered = next?.delivered_today ?? 0;
          const before = context.beforeDelivered ?? 0;
          if (!next?.batch_id && delivered >= before + context.expectedSent) {
            setMessage(
              `${context.expectedSent} job${context.expectedSent === 1 ? "" : "s"} sent to ${context.clientName ?? "the client"}'s Sheet.`,
            );
          } else {
            setMessage(
              "JobSift finished the command. Check the client state below before taking another action.",
            );
          }
        } else {
          setMessage("JobSift confirmed the updated client state.");
        }
      }
      return body.data;
    } catch (error) {
      setStatusError(
        error instanceof Error ? error.message : "Could not load current client state.",
      );
      return null;
    }
  }, []);

  useEffect(() => {
    void loadStatus();
    return () => {
      if (statePollTimer.current !== null) window.clearTimeout(statePollTimer.current);
      if (reviewPollTimer.current !== null) window.clearTimeout(reviewPollTimer.current);
    };
  }, [loadStatus]);

  function pollForFreshState(attempt = 0) {
    if (statePollTimer.current !== null) window.clearTimeout(statePollTimer.current);
    const delay = attempt < 8 ? 1500 : 5000;
    statePollTimer.current = window.setTimeout(async () => {
      await loadStatus();
      if (pendingControlRequestId.current && attempt < 60) {
        pollForFreshState(attempt + 1);
      }
    }, delay);
  }

  async function mutate(
    payload: Record<string, string>,
    context?: {
      profileId?: string;
      expectedSent?: number;
      beforeDelivered?: number;
      clientName?: string;
    },
  ) {
    if (!stateVerified) {
      setMessage(
        "Current client state is not verified. Refresh status and wait for the active JobSift operation to finish.",
      );
      return;
    }
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch("/api/control/dispatch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = (await readJson(response)) as {
        data?: { control_request_id?: unknown };
      };
      const requestId = body.data?.control_request_id;
      if (!validControlRequestId(requestId)) {
        setMessage(
          "Command accepted, but JobSift could not correlate the confirmation. Controls remain locked until you reload after it finishes.",
        );
        setAwaitingFreshState(true);
        return;
      }
      mutationContext.current = context ?? null;
      pendingControlRequestId.current = requestId.toLowerCase();
      setAwaitingFreshState(true);
      setMessage("JobSift is applying the change…");
      pollForFreshState();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "The JobSift command failed.");
    } finally {
      setBusy(false);
    }
  }

  async function clientAction(
    config: ManagedProfile,
    state: ProfileSnapshot,
    operation: "pause" | "resume" | "sheet-check" | "run-now",
  ) {
    if (
      operation === "pause" &&
      !window.confirm(`Pause ${config.client_name}? New deliveries will stop until you resume them.`)
    ) {
      return;
    }
    await mutate({
      command: "client-control",
      operation,
      profile_id: state.profile_id,
      profile_capability: state.control_capability ?? "",
      daily_quota: String(state.daily_quota ?? 100),
      delivery_mode: state.delivery_mode ?? "review",
      timezone: "Africa/Lagos",
      batch_id: "",
    });
  }

  async function findJobs() {
    await mutate({
      command: "inventory-refresh",
      workday_targets: "25",
      workday_detail_concurrency: "4",
      yield_extra_budget: "100",
    });
  }

  async function pollReview(batchId: string, requestId: string, attempt = 0) {
    try {
      const response = await fetch(
        `/api/control/review?control_request_id=${encodeURIComponent(requestId)}`,
        { cache: "no-store" },
      );
      const body = (await readJson(response)) as {
        data: {
          state: string;
          review?: ReviewSnapshot;
          message?: string;
        };
      };
      if (body.data.state === "ready" && body.data.review) {
        setReviews((current) => ({
          ...current,
          [batchId]: { state: "ready", requestId, review: body.data.review },
        }));
        return;
      }
      if (body.data.state === "failed") {
        setReviews((current) => ({
          ...current,
          [batchId]: {
            state: "failed",
            requestId,
            message:
              body.data.message ??
              "JobSift could not verify this review batch. Refresh client state.",
          },
        }));
        return;
      }
      if (attempt >= 60) {
        setReviews((current) => ({
          ...current,
          [batchId]: {
            state: "failed",
            requestId,
            message: "Review verification did not finish. Try again from the current client state.",
          },
        }));
        return;
      }
      setReviews((current) => ({
        ...current,
        [batchId]: {
          ...current[batchId],
          state: body.data.state === "queued" ? "queued" : "in_progress",
          requestId,
        },
      }));
      const delay = attempt < 8 ? 1500 : 5000;
      reviewPollTimer.current = window.setTimeout(
        () => void pollReview(batchId, requestId, attempt + 1),
        delay,
      );
    } catch (error) {
      setReviews((current) => ({
        ...current,
        [batchId]: {
          state: "failed",
          requestId,
          message:
            error instanceof Error
              ? error.message
              : "Could not load authoritative review evidence.",
        },
      }));
    }
  }

  async function startReview(state: ProfileSnapshot) {
    if (!state.batch_id || !stateVerified) return;
    const batchId = state.batch_id;
    setReviews((current) => ({
      ...current,
      [batchId]: { state: "queued" },
    }));
    try {
      const response = await fetch("/api/control/review", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          profile_id: state.profile_id,
          profile_capability: state.control_capability ?? "",
          batch_id: batchId,
        }),
      });
      const body = (await readJson(response)) as {
        data?: { control_request_id?: unknown };
      };
      const requestId = body.data?.control_request_id;
      if (!validControlRequestId(requestId)) {
        throw new Error("JobSift could not correlate this review read.");
      }
      setReviews((current) => ({
        ...current,
        [batchId]: { state: "queued", requestId: requestId.toLowerCase() },
      }));
      await pollReview(batchId, requestId.toLowerCase());
    } catch (error) {
      setReviews((current) => ({
        ...current,
        [batchId]: {
          state: "failed",
          message:
            error instanceof Error
              ? error.message
              : "Could not start authoritative review.",
        },
      }));
    }
  }

  async function sendSelection(
    config: ManagedProfile,
    state: ProfileSnapshot,
    review: ReviewSnapshot,
  ) {
    if (!state.batch_id || review.batch_id !== state.batch_id) return;
    const removedOrdinals = removed[state.batch_id] ?? [];
    const kept = review.items.filter((item) => !removedOrdinals.includes(item.ordinal));
    if (!kept.length) {
      setMessage("No jobs are selected. Use Discard batch if you want to send none.");
      return;
    }
    if (kept.some((item) => !item.release_ready)) {
      setMessage(
        "One or more kept jobs are no longer safe to release. Remove those jobs or refresh the review evidence.",
      );
      return;
    }
    const label = `${config.client_name}'s Sheet`;
    if (
      !window.confirm(
        `Send ${kept.length} job${kept.length === 1 ? "" : "s"} to ${label}? JobSift will re-check freshness, quota, dedupe and Sheet state before writing.`,
      )
    ) {
      return;
    }
    await mutate(
      {
        command: "client-control",
        operation: "release-selection",
        profile_id: state.profile_id,
        profile_capability: state.control_capability ?? "",
        daily_quota: String(state.daily_quota ?? 100),
        delivery_mode: state.delivery_mode ?? "review",
        timezone: "Africa/Lagos",
        batch_id: state.batch_id,
        removed_ordinals: removedOrdinals.join(","),
        expected_generation_id: review.generation_id,
      },
      {
        profileId: state.profile_id,
        expectedSent: kept.length,
        beforeDelivered: state.delivered_today ?? 0,
        clientName: config.client_name,
      },
    );
  }

  async function discardBatch(config: ManagedProfile, state: ProfileSnapshot) {
    if (!state.batch_id || state.recovery_required) return;
    if (
      !window.confirm(
        `Discard the waiting jobs for ${config.client_name}? Nothing in this batch will be sent.`,
      )
    ) {
      return;
    }
    await mutate({
      command: "client-control",
      operation: "discard-batch",
      profile_id: state.profile_id,
      profile_capability: state.control_capability ?? "",
      daily_quota: String(state.daily_quota ?? 100),
      delivery_mode: state.delivery_mode ?? "review",
      timezone: "Africa/Lagos",
      batch_id: state.batch_id,
    });
  }

  if (surface === "clients") {
    return (
      <div className="operator-workspace">
        <section className="section-block client-page-head">
          <div>
            <h2>Clients</h2>
            <p>
              See each client in plain language. Daily delivery safeguards still run on the
              backend.
            </p>
          </div>
          <div className="client-page-actions">
            <Link href="/clients/new" className="secondary-link">Add client</Link>
            <button
              type="button"
              disabled={busy || !status?.control_ready || !stateVerified}
              onClick={() => void findJobs()}
            >
              {busy ? "Starting…" : "Find jobs now"}
            </button>
          </div>
        </section>

        {!status?.control_ready ? (
          <div className="notice">
            Production controls are unavailable on this deployment.
          </div>
        ) : null}
        {statusError ? <div className="error">{statusError}</div> : null}
        {message ? <p className="control-message">{message}</p> : null}
        {!stateVerified && status ? (
          <div className="notice">
            Current client state is not fully verified. Actions stay locked until the
            active JobSift operation finishes.
          </div>
        ) : null}

        <section className="client-grid" aria-label="JobSift clients">
          {configured.length ? (
            configured.map(({ config, state }) => {
              const waiting = state?.selected_count ?? 0;
              const funnel = state?.client_funnel;
              const lastResult =
                funnel?.overall?.confirmed_matches_0_24h ??
                funnel?.overall?.other_rules_survived_0_24h;
              return (
                <article
                  className="client-card"
                  id={clientAnchor(config)}
                  key={config.profile_id}
                >
                  <div className="client-card-head">
                    <div>
                      <h3>{config.client_name}</h3>
                      <p>{config.destination_name}</p>
                    </div>
                    <span
                      className={
                        state?.profile_status === "active"
                          ? "client-state active"
                          : "client-state paused"
                      }
                    >
                      {state?.profile_status === "active" ? "Active" : state?.profile_status === "paused" ? "Paused" : "State unavailable"}
                    </span>
                  </div>

                  <div className="client-facts">
                    <div><span>Mode</span><strong>{state?.delivery_mode === "auto" ? "Auto" : state?.delivery_mode === "review" ? "Review" : "—"}</strong></div>
                    <div><span>Daily limit</span><strong>{count(state?.daily_quota)}</strong></div>
                    <div><span>Sent today</span><strong>{count(state?.delivered_today)}</strong></div>
                    <div><span>Waiting for review</span><strong>{waiting.toLocaleString()}</strong></div>
                    <div><span>Sheet</span><strong>{state?.sheet_status === "ready" ? "Connected" : state?.sheet_status === "disabled" ? "Needs attention" : "Unknown"}</strong></div>
                    <div><span>Last matching result</span><strong>{lastResult === undefined ? "Not available" : `${lastResult.toLocaleString()} jobs`}</strong></div>
                  </div>

                  {state?.recovery_required ? (
                    <div className="error">
                      Sheet delivery needs safe recovery before another batch can be reviewed.
                    </div>
                  ) : null}
                  {state?.sheet_status && state.sheet_status !== "ready" ? (
                    <div className="notice">This client cannot receive new jobs until the Sheet is ready.</div>
                  ) : null}

                  <div className="client-actions">
                    {waiting > 0 ? (
                      <Link href={`/review#${clientAnchor(config)}`}>
                        Review {waiting.toLocaleString()} job{waiting === 1 ? "" : "s"}
                      </Link>
                    ) : (
                      <span className="client-action-note">No jobs waiting</span>
                    )}
                    <button
                      type="button"
                      disabled={busy || !stateVerified || !state}
                      onClick={() =>
                        state
                          ? void clientAction(
                              config,
                              state,
                              state.profile_status === "active" ? "pause" : "resume",
                            )
                          : undefined
                      }
                    >
                      {state?.profile_status === "active" ? "Pause" : "Resume"}
                    </button>
                    <button
                      type="button"
                      disabled={busy || !stateVerified || !state}
                      onClick={() =>
                        state ? void clientAction(config, state, "sheet-check") : undefined
                      }
                    >
                      Check Sheet
                    </button>
                    {state?.sheet_url ? (
                      <a href={state.sheet_url} target="_blank" rel="noreferrer">
                        Open Sheet
                      </a>
                    ) : null}
                    <button
                      type="button"
                      disabled={busy || !stateVerified || !state || state.profile_status !== "active"}
                      onClick={() =>
                        state ? void clientAction(config, state, "run-now") : undefined
                      }
                    >
                      Check current inventory
                    </button>
                  </div>
                </article>
              );
            })
          ) : (
            <div className="empty">
              <h3>No operator clients are configured</h3>
              <p>Add a production client to the server-side operator catalogue before controls can be used.</p>
            </div>
          )}
        </section>
      </div>
    );
  }

  return (
    <div className="operator-workspace">
      <section className="section-block client-page-head">
        <div>
          <h2>Review jobs</h2>
          <p>
            Review the exact backend-prepared jobs before anything is written to a
            client Sheet.
          </p>
        </div>
        <button type="button" disabled={busy} onClick={() => void loadStatus()}>
          Refresh
        </button>
      </section>

      {statusError ? <div className="error">{statusError}</div> : null}
      {message ? <p className="control-message">{message}</p> : null}
      {!stateVerified && status ? (
        <div className="notice">
          Review actions are locked until JobSift verifies the latest client state.
        </div>
      ) : null}

      {pending.length ? (
        <div className="review-batches">
          {pending.map(({ config, state }) => {
            if (!state?.batch_id) return null;
            const batchId = state.batch_id;
            const load = reviews[batchId] ?? { state: "idle" as const };
            const review = load.review;
            const removedOrdinals = removed[batchId] ?? [];
            const keptCount = review
              ? review.items.filter((item) => !removedOrdinals.includes(item.ordinal)).length
              : 0;
            const keptUnsafe =
              review?.items.some(
                (item) =>
                  !removedOrdinals.includes(item.ordinal) && !item.release_ready,
              ) ?? true;
            return (
              <section
                className="review-batch"
                id={clientAnchor(config)}
                key={batchId}
                aria-labelledby={`review-${clientAnchor(config)}`}
              >
                <div className="review-batch-head">
                  <div>
                    <h3 id={`review-${clientAnchor(config)}`}>{config.client_name}</h3>
                    <p>
                      {state.selected_count?.toLocaleString() ?? "—"} job
                      {state.selected_count === 1 ? "" : "s"} waiting · {config.destination_name}
                    </p>
                  </div>
                  <Link href={`/clients#${clientAnchor(config)}`}>Back to client</Link>
                </div>

                {state.recovery_required ? (
                  <div className="error">
                    This batch has an uncertain Sheet write. Normal review/release is locked;
                    use the safe recovery action from Operations.
                  </div>
                ) : load.state === "idle" ? (
                  <button
                    type="button"
                    disabled={!stateVerified}
                    onClick={() => void startReview(state)}
                  >
                    Load jobs to review
                  </button>
                ) : load.state === "queued" || load.state === "in_progress" ? (
                  <div className="review-loading">Checking the prepared jobs and current safety rules…</div>
                ) : load.state === "failed" ? (
                  <div className="error">
                    {load.message ?? "Review evidence could not be loaded."}{" "}
                    <button
                      type="button"
                      disabled={!stateVerified}
                      onClick={() => void startReview(state)}
                    >
                      Try again
                    </button>
                  </div>
                ) : review ? (
                  <>
                    <div className="review-summary">
                      <div><span>Prepared</span><strong>{review.selected_count.toLocaleString()}</strong></div>
                      <div><span>Kept</span><strong>{keptCount.toLocaleString()}</strong></div>
                      <div><span>Removed</span><strong>{removedOrdinals.length.toLocaleString()}</strong></div>
                      <div><span>Freshness limit</span><strong>{review.freshness_limit_hours ? `${review.freshness_limit_hours}h` : "Not set"}</strong></div>
                    </div>

                    <div className="review-toolbar">
                      <button
                        type="button"
                        onClick={() =>
                          setRemoved((current) => ({
                            ...current,
                            [batchId]: review.items
                              .filter((item) => !item.release_ready)
                              .map((item) => item.ordinal),
                          }))
                        }
                      >
                        Approve all eligible
                      </button>
                      <span>
                        Every kept job is re-checked again immediately before Sheet delivery.
                      </span>
                    </div>

                    <div className="review-list">
                      {review.items.map((item) => {
                        const isRemoved = removedOrdinals.includes(item.ordinal);
                        return (
                          <article
                            className={`review-job${isRemoved ? " removed" : ""}`}
                            key={item.ordinal}
                          >
                            <div className="review-job-main">
                              <div>
                                <div className="review-job-title">
                                  <h4>{item.title || "Untitled job"}</h4>
                                  <span className={`status ${item.decision}`}>
                                    {humanDecision(item.decision)}
                                  </span>
                                </div>
                                <p className="review-company">{item.company || "Unknown company"}</p>
                                <div className="review-evidence">
                                  <span>{postingAge(item)}</span>
                                  <span>{item.source || "Unknown source"}</span>
                                  <span>{item.remote_status === "remote" ? "Remote" : item.remote_status === "hybrid" ? "Hybrid" : item.remote_status === "onsite" ? "On-site" : "Work mode unclear"}</span>
                                  {item.location ? <span>{item.location}</span> : null}
                                </div>
                              </div>
                              <div className="review-job-actions">
                                {item.application_link ? (
                                  <a href={item.application_link} target="_blank" rel="noreferrer">
                                    Open job
                                  </a>
                                ) : null}
                                <button
                                  type="button"
                                  aria-pressed={isRemoved}
                                  onClick={() =>
                                    setRemoved((current) => {
                                      const values = new Set(current[batchId] ?? []);
                                      if (values.has(item.ordinal)) values.delete(item.ordinal);
                                      else values.add(item.ordinal);
                                      return { ...current, [batchId]: [...values].sort((a, b) => a - b) };
                                    })
                                  }
                                >
                                  {isRemoved ? "Keep" : "Remove"}
                                </button>
                              </div>
                            </div>

                            {item.matched_reasons.length ? (
                              <div className="review-why">
                                <strong>Why it matched</strong>
                                <ul>
                                  {item.matched_reasons.map((reason) => (
                                    <li key={reason}>{reasonText(reason)}</li>
                                  ))}
                                </ul>
                              </div>
                            ) : null}
                            {item.review_reasons.length ? (
                              <div className="review-warning">
                                <strong>Needs attention</strong>
                                <ul>
                                  {item.review_reasons.map((reason) => (
                                    <li key={reason}>{reasonText(reason)}</li>
                                  ))}
                                </ul>
                              </div>
                            ) : null}
                            {item.warnings.length ? (
                              <div className="review-warning" role="status">
                                {item.warnings.map((warning) => (
                                  <p key={warning}>{warning}</p>
                                ))}
                              </div>
                            ) : null}
                            {isRemoved ? <div className="review-removed-label">Will not be sent</div> : null}
                          </article>
                        );
                      })}
                    </div>

                    <div className="review-send">
                      {keptUnsafe ? (
                        <div className="notice">
                          At least one kept job is no longer safe to release. Remove the warned
                          job before sending.
                        </div>
                      ) : null}
                      {state.profile_status !== "active" ? (
                        <div className="notice">
                          This client is paused. Resume the client before sending reviewed jobs.
                        </div>
                      ) : state.sheet_status !== "ready" ? (
                        <div className="notice">
                          The client Sheet is not ready. Fix the Sheet connection before sending.
                        </div>
                      ) : null}
                      <button
                        type="button"
                        disabled={
                          busy ||
                          !stateVerified ||
                          state.recovery_required ||
                          !keptCount ||
                          keptUnsafe ||
                          state.profile_status !== "active" ||
                          state.sheet_status !== "ready"
                        }
                        onClick={() => void sendSelection(config, state, review)}
                      >
                        Send {keptCount.toLocaleString()} job{keptCount === 1 ? "" : "s"} to {config.client_name}&apos;s Sheet
                      </button>
                      <button
                        type="button"
                        className="secondary-action"
                        disabled={busy || !stateVerified || state.recovery_required}
                        onClick={() => void discardBatch(config, state)}
                      >
                        Discard all
                      </button>
                    </div>
                  </>
                ) : null}
              </section>
            );
          })}
        </div>
      ) : (
        <section className="empty">
          <h3>No jobs are waiting for review</h3>
          <p>Run sourcing from Clients. Review-mode matches will appear here when a batch is prepared.</p>
          <Link href="/clients">Open Clients</Link>
        </section>
      )}
    </div>
  );
}
