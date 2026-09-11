"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import type {
  ApiResponse,
  Client,
  Decision,
  JobGroupDetail,
  JobGroupSummary,
  ListResponse,
} from "@/lib/contracts/service";
import type { JobSiftApi, JobsQuery } from "@/lib/api/interface";
import { api } from "@/lib/api/client";
import {
  dateText,
  decisionLabel,
  isTyping,
  metricText,
  outcomeText,
  rowStep,
} from "@/lib/display";
import { JobDetail } from "./job-detail";
import { MatchStatus } from "./status";
import { usePreferences } from "./preferences";
const decisions = Object.keys(decisionLabel) as Decision[];
export function JobsWorkspace({
  client,
  transport = api,
}: {
  client: Client;
  transport?: JobSiftApi;
}) {
  const [result, setResult] = useState<ListResponse<JobGroupSummary> | null>(
    null,
  );
  const [detail, setDetail] = useState<ApiResponse<JobGroupDetail> | null>(
    null,
  );
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [filters, setFilters] = useState(false);
  const [search, setSearch] = useState("");
  const [decision, setDecision] = useState("default");
  const [source, setSource] = useState("");
  const [query, setQuery] = useState<JobsQuery>({
    client_id: client.client_id,
    destination_id: client.destinations[0].destination_id,
  });
  const originScroll = useRef(0);
  const searchRef = useRef<HTMLInputElement>(null);
  const origin = useRef<HTMLAnchorElement | null>(null);
  const filterButton = useRef<HTMLButtonElement>(null);
  const filterDialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    if (filters) filterDialog.current?.showModal();
  }, [filters]);
  const serial = useRef(0);
  const list = useRef<HTMLDivElement>(null);
  const preferences = usePreferences();
  const closeDetail = useCallback(() => {
    setDetail(null);
    const url = new URL(window.location.href);
    url.searchParams.delete("job");
    window.history.replaceState(window.history.state, "", url);
    requestAnimationFrame(() => {
      window.scrollTo({ top: originScroll.current });
      origin.current?.focus({ preventScroll: true });
    });
  }, []);
  const load = useCallback(
    async (next: JobsQuery, restoreGroup?: string | null) => {
      const request = ++serial.current;
      setBusy(true);
      setError("");
      try {
        if (restoreGroup && !next.snapshot_id) {
          setResult(null);
          setDetail(null);
          throw new Error("Snapshot unavailable for this detail link. Refresh Jobs to load a new result snapshot.");
        }
        const response = await transport.listJobs(next);
        if (request !== serial.current) return;
        if (!response.page.snapshot_id || response.meta.snapshot_id !== response.page.snapshot_id || (next.snapshot_id && next.snapshot_id !== response.page.snapshot_id)) {
          setResult(null);
          setDetail(null);
          throw new Error("Snapshot unavailable for these results. Refresh Jobs to load a new result snapshot.");
        }
        setResult(response);
        const savedQuery = { ...next, snapshot_id: response.page.snapshot_id };
        setQuery(savedQuery);
        window.history.replaceState(
          { ...window.history.state, jobsQuery: savedQuery },
          "",
        );
        setDetail(null);
        if (restoreGroup) {
          const restored = await transport.getJob(
            client.client_id,
            restoreGroup,
            response.page.snapshot_id,
          );
          if (request === serial.current) setDetail(restored);
        }
      } catch (reason) {
        if (request === serial.current)
          setError(
            reason instanceof Error ? reason.message : "Could not load jobs.",
          );
      } finally {
        if (request === serial.current) setBusy(false);
      }
    },
    [transport, client.client_id],
  );
  useEffect(() => {
    const requests = serial;
    const restore = (event?: PopStateEvent) => {
      const params = new URLSearchParams(window.location.search);
      const q = params.get("q") ?? "";
      const d = params.get("decision") ?? "default";
      const s = params.get("source") ?? "";
      setSearch(q);
      setDecision(d);
      setSource(s);
      void load(
        (event?.state?.jobsQuery as JobsQuery | undefined) ?? {
          client_id: client.client_id,
          destination_id: client.destinations[0].destination_id,
          q,
          ...(params.get("snapshot_id") ? { snapshot_id: params.get("snapshot_id")! } : {}),
          ...(d === "all"
            ? { decision: decisions }
            : d !== "default"
              ? { decision: [d as Decision] }
              : {}),
          ...(s ? { source: [s] } : {}),
        },
        params.get("job"),
      );
    };
    restore();
    window.addEventListener("popstate", restore);
    return () => {
      requests.current++;
      window.removeEventListener("popstate", restore);
    };
  }, [client, load]);
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (
        e.key === "/" &&
        preferences.shortcuts &&
        !e.metaKey &&
        !e.ctrlKey &&
        !e.altKey &&
        !e.isComposing &&
        !isTyping(e.target) &&
        !document.querySelector("dialog[open]") &&
        searchRef.current?.getClientRects().length
      ) {
        e.preventDefault();
        searchRef.current.focus();
      }
    };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, [preferences.shortcuts]);
  const apply = (clear = false) => {
    const q = clear ? "" : search;
    const d = clear ? "default" : decision;
    const s = clear ? "" : source;
    if (clear) {
      setSearch("");
      setDecision("default");
      setSource("");
    }
    const next: JobsQuery = {
      client_id: client.client_id,
      destination_id: client.destinations[0].destination_id,
      q,
      ...(d === "all"
        ? { decision: decisions }
        : d !== "default"
          ? { decision: [d as Decision] }
          : {}),
      ...(s ? { source: [s] } : {}),
    };
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (d !== "default") params.set("decision", d);
    if (s) params.set("source", s);
    window.history.pushState(
      null,
      "",
      `/jobs${params.size ? `?${params}` : ""}`,
    );
    void load(next);
  };
  const inspect = async (id: string, element: HTMLAnchorElement) => {
    if (!result) return;
    origin.current = element;
    originScroll.current = window.scrollY;
    setError("");
    const request = ++serial.current;
    try {
      const next = await transport.getJob(
        client.client_id,
        id,
        result.page.snapshot_id,
      );
      if (request !== serial.current) return;
      setDetail(next);
      const url = new URL(window.location.href);
      url.searchParams.set("job", id);
      url.searchParams.set("snapshot_id", result.page.snapshot_id);
      window.history.pushState({ ...window.history.state }, "", url);
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Could not inspect job.",
      );
    }
  };
  const rowLink = (job: JobGroupSummary) => (
    <a
      className="job-link"
      href={`/jobs?${new URLSearchParams({
        ...(query.q ? { q: query.q } : {}),
        ...(query.decision
          ? {
              decision:
                query.decision.length === decisions.length
                  ? "all"
                  : query.decision[0],
            }
          : {}),
        ...(query.source?.[0] ? { source: query.source[0] } : {}),
        job: job.delivery_group_id,
        snapshot_id: result!.page.snapshot_id,
      })}`}
      title={job.representative_posting?.title}
      aria-label={`Inspect ${job.representative_posting?.title ?? "group"} at ${job.representative_posting?.company ?? "unknown company"}`}
      onClick={(e) => {
        if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || isTyping(e.target)) return;
        e.preventDefault();
        void inspect(job.delivery_group_id, e.currentTarget);
      }}
    >
      {job.representative_posting?.title ?? "Representative not reported"}
    </a>
  );
  return (
    <div className="jobs-page">
      <header className="page-head">
        <h1>Jobs</h1>
        <div className="scope-line">
          {client.display_name} · {client.destinations[0].display_name} ·
          Fictional snapshot · Times in UTC
        </div>
      </header>
      <div className="toolbar">
        <form
          className="search-form"
          onSubmit={(e) => {
            e.preventDefault();
            apply();
          }}
        >
          <label htmlFor="jobs-search">
            <span className="sr-only">Search these jobs</span>
            <input
              ref={searchRef}
              id="jobs-search"
              type="search"
              maxLength={200}
              placeholder="Search role, company or location"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </label>
          <button type="submit" disabled={busy}>
            Search
          </button>
        </form>
        <button
          ref={filterButton}
          aria-expanded={filters}
          aria-controls="job-filters"
          onClick={() => setFilters(!filters)}
        >
          Filters
        </button>
        <button onClick={() => apply(true)}>Clear filters</button>
        <span className="filter-summary">
          {query.decision
            ? query.decision.length === 4
              ? "All decisions"
              : query.decision.map((d) => decisionLabel[d]).join(", ")
            : "Strong match + Possible"}
          {query.source?.length ? ` · ${query.source.join(", ")}` : ""}
          {query.q ? ` · “${query.q}”` : ""}
        </span>
      </div>
      {filters && (
        <dialog
          ref={filterDialog}
          className="filter-dialog"
          aria-label="Job filters"
          onCancel={() => {
            setFilters(false);
            requestAnimationFrame(() => filterButton.current?.focus());
          }}
        >
          <div className="dialog-head">
            <h2>Job filters</h2>
            <button
              onClick={() => {
                setFilters(false);
                requestAnimationFrame(() => filterButton.current?.focus());
              }}
            >
              Close filters
            </button>
          </div>
          <form
            id="job-filters"
            className="filter-box"
            onSubmit={(e) => {
              e.preventDefault();
              apply();
              setFilters(false);
              requestAnimationFrame(() => filterButton.current?.focus());
            }}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.stopPropagation();
                setFilters(false);
                requestAnimationFrame(() => filterButton.current?.focus());
              }
            }}
          >
            <label>
              Match decision
              <select
                value={decision}
                onChange={(e) => setDecision(e.target.value)}
              >
                <option value="default">Strong match + Possible</option>
                <option value="all">All decisions</option>
                {decisions.map((d) => (
                  <option key={d} value={d}>
                    {decisionLabel[d]}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Source
              <select
                value={source}
                onChange={(e) => setSource(e.target.value)}
              >
                <option value="">All sources</option>
                <option value="greenhouse">Greenhouse</option>
                <option value="ashby">Ashby</option>
                <option value="lever">Lever</option>
              </select>
            </label>
            <button type="submit">Apply filters</button>
          </form>
        </dialog>
      )}
      <div className="notice">
        {result
          ? `${result.meta.completeness === "partial" ? "Partial" : result.meta.completeness} fixture coverage · ${result.meta.source_failures.length} recorded source failure(s).`
          : "Loading fixture coverage…"}{" "}
        <Link href="/runs">Inspect run</Link>
      </div>
      {error && (
        <div role="alert" className="error">
          {error} <button onClick={() => apply(true)}>Refresh Jobs</button>
        </div>
      )}
      <div role="status" className="sr-only">
        {busy
          ? "Loading jobs…"
          : result
            ? `${result.data.length} groups shown. ${detail ? "Job detail open." : ""}`
            : ""}
      </div>
      <div className={`workbench ${detail ? "is-open" : ""}`}>
        <div
          ref={list}
          className="list-region"
          role="region"
          aria-label="Jobs list"
          aria-busy={busy}
          onKeyDown={(e) => {
            if (e.key === "Escape" && detail && !isTyping(e.target)) {
              e.preventDefault();
              closeDetail();
              return;
            }
            if (
              !preferences.shortcuts ||
              !["j", "k"].includes(e.key) ||
              isTyping(e.target) ||
              e.metaKey ||
              e.ctrlKey ||
              e.altKey ||
              e.nativeEvent.isComposing
            )
              return;
            const links = Array.from(
              list.current?.querySelectorAll<HTMLAnchorElement>(".job-link") ??
                [],
            ).filter((link) => link.getClientRects().length > 0);
            const index = links.indexOf(
              document.activeElement as HTMLAnchorElement,
            );
            if (index < 0) return;
            e.preventDefault();
            links[rowStep(e.key, index, links.length)]?.focus();
          }}
        >
          {!result ? (
            <p>{error ? "No jobs loaded." : "Loading jobs…"}</p>
          ) : result.data.length === 0 ? (
            <div className="empty">
              <h2>No groups match these filters</h2>
              <p>
                Search covers fictional role, company and location evidence.
              </p>
              <button onClick={() => apply(true)}>Clear filters</button>
            </div>
          ) : (
            <>
              <table className="jobs-table">
                <caption className="sr-only">
                  Fictional job groups, strongest match first. Additional
                  source, outcome and time evidence is available in each detail.
                </caption>
                <thead>
                  <tr>
                    <th className="role-col" scope="col">
                      Role
                    </th>
                    <th className="company-col" scope="col">
                      Company
                    </th>
                    <th className="location-col" scope="col">
                      Location / mode
                    </th>
                    <th className="match-col" scope="col">
                      Match
                    </th>
                    <th className="source-col" scope="col">
                      Source
                    </th>
                    <th className="seen-col" scope="col">
                      First seen
                    </th>
                    <th className="outcome-col" scope="col">
                      Outcome
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {result.data.map((job) => {
                    const p = job.representative_posting;
                    return (
                      <tr
                        key={job.delivery_group_id}
                        className={
                          detail?.data.delivery_group_id ===
                          job.delivery_group_id
                            ? "inspected"
                            : ""
                        }
                      >
                        <td className="role-col">{rowLink(job)}</td>
                        <td className="company-col">
                          <div className="cell-text" title={p?.company}>
                            {p?.company ?? "Not reported"}
                          </div>
                        </td>
                        <td className="location-col">
                          <div className="cell-text">
                            {p?.location_text ?? "Unknown"} ·{" "}
                            {p?.remote_status ?? "unknown"}
                          </div>
                        </td>
                        <td className="match-col">
                          <MatchStatus decision={job.match?.decision} />
                        </td>
                        <td className="source-col">
                          <div className="cell-text">
                            {p?.source ?? "Not reported"}
                          </div>
                        </td>
                        <td className="seen-col">
                          {dateText(p?.first_seen_at ?? null)}
                        </td>
                        <td className="outcome-col">
                          {outcomeText(job.outcome_summary)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <div className="mobile-jobs">
                {result.data.map((job) => (
                  <article
                    className={`mobile-job ${detail?.data.delivery_group_id === job.delivery_group_id ? "inspected" : ""}`}
                    key={job.delivery_group_id}
                  >
                    {rowLink(job)}
                    <div className="mobile-meta">
                      <span>{job.representative_posting?.company}</span>
                      <MatchStatus decision={job.match?.decision} />
                    </div>
                    <p className="metadata">
                      {job.representative_posting?.location_text ??
                        "Location unknown"}{" "}
                      · {job.representative_posting?.remote_status ?? "unknown"}{" "}
                      · Outcome: {outcomeText(job.outcome_summary)}
                    </p>
                  </article>
                ))}
              </div>
            </>
          )}
          {result && (
            <div className="pagination">
              <span>
                {result.data.length} shown
                {result.page.known_total.availability === "reported"
                  ? ` · ${metricText(result.page.known_total)} groups in this scope`
                  : " · Total not reported"}
              </span>
              <div className="pager-actions">
                <button
                  disabled={!result.page.previous_cursor || busy}
                  onClick={() =>
                    void load({
                      ...query,
                      cursor: result.page.previous_cursor!,
                    })
                  }
                >
                  Previous
                </button>
                <button
                  disabled={!result.page.next_cursor || busy}
                  onClick={() =>
                    void load({ ...query, cursor: result.page.next_cursor! })
                  }
                >
                  Next
                </button>
              </div>
            </div>
          )}
        </div>
        {detail && <JobDetail job={detail.data} onClose={closeDetail} />}
      </div>
    </div>
  );
}
