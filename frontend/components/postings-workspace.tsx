"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import type { ApiResponse, Client, Decision, ListResponse, PostingDetail } from "@/lib/contracts/service";
import type { JobsQuery } from "@/lib/api/interface";
import { LiveJobSiftApi } from "@/lib/api/live-api";
import { dateText, decisionLabel, factText, metricText, outcomeText, safeExternalUrl } from "@/lib/display";
import { MatchStatus } from "./status";

const transport = new LiveJobSiftApi();
const decisions = Object.keys(decisionLabel) as Decision[];

export function PostingsWorkspace({ client }: { client: Client }) {
  const [result, setResult] = useState<ListResponse<PostingDetail> | null>(null);
  const [detail, setDetail] = useState<ApiResponse<PostingDetail> | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [search, setSearch] = useState("");
  const [decision, setDecision] = useState("default");
  const [source, setSource] = useState("");
  const serial = useRef(0);
  const [query, setQuery] = useState<JobsQuery>({ client_id: client.client_id, destination_id: "" });

  const load = useCallback(async (next: JobsQuery, postingId?: string | null) => {
    const request = ++serial.current;
    setBusy(true);
    setError("");
    try {
      if (postingId && !next.snapshot_id) throw new Error("Snapshot unavailable for this detail link. Refresh postings.");
      const response = await transport.listPostings(next);
      if (request !== serial.current) return;
      if (!response.page.snapshot_id || response.meta.snapshot_id !== response.page.snapshot_id || (next.snapshot_id && next.snapshot_id !== response.page.snapshot_id)) {
        throw new Error("Snapshot unavailable for these results. Refresh postings.");
      }
      setResult(response);
      const saved = { ...next, snapshot_id: response.page.snapshot_id };
      setQuery(saved);
      window.history.replaceState({ ...window.history.state, postingsQuery: saved }, "");
      setDetail(null);
      if (postingId) {
        const row = await transport.getPosting(client.client_id, postingId, response.page.snapshot_id);
        if (request === serial.current) setDetail(row);
      }
    } catch (reason) {
      if (request === serial.current) {
        setResult(null);
        setDetail(null);
        setError(reason instanceof Error ? reason.message : "Could not load postings.");
      }
    } finally {
      if (request === serial.current) setBusy(false);
    }
  }, [client.client_id]);

  useEffect(() => {
    const requests = serial;
    const restore = (event?: PopStateEvent) => {
      const params = new URLSearchParams(window.location.search);
      const q = params.get("q") ?? "";
      const d = params.get("decision") ?? "default";
      const s = params.get("source") ?? "";
      setSearch(q); setDecision(d); setSource(s);
      const saved = event?.state?.postingsQuery as JobsQuery | undefined;
      void load(saved ?? {
        client_id: client.client_id, destination_id: "", q,
        ...(params.get("snapshot_id") ? { snapshot_id: params.get("snapshot_id")! } : {}),
        ...(d === "all" ? { decision: decisions } : d !== "default" ? { decision: [d as Decision] } : {}),
        ...(s ? { source: [s] } : {}),
      }, params.get("posting"));
    };
    // Strict Mode replays the mount effect before this frame. Canceling its
    // first frame avoids allocating a second service snapshot in development.
    const initialFrame = requestAnimationFrame(() => restore());
    window.addEventListener("popstate", restore);
    return () => { cancelAnimationFrame(initialFrame); requests.current++; window.removeEventListener("popstate", restore); };
  }, [client.client_id, load]);

  const apply = (clear = false) => {
    const q = clear ? "" : search, d = clear ? "default" : decision, s = clear ? "" : source;
    if (clear) { setSearch(""); setDecision("default"); setSource(""); }
    const next: JobsQuery = {
      client_id: client.client_id, destination_id: "", q,
      ...(d === "all" ? { decision: decisions } : d !== "default" ? { decision: [d as Decision] } : {}),
      ...(s ? { source: [s] } : {}),
    };
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (d !== "default") params.set("decision", d);
    if (s) params.set("source", s);
    window.history.pushState(null, "", `/jobs${params.size ? `?${params}` : ""}`);
    void load(next);
  };

  const inspect = async (id: string) => {
    if (!result) return;
    const request = ++serial.current;
    setError("");
    try {
      const row = await transport.getPosting(client.client_id, id, result.page.snapshot_id);
      if (request !== serial.current) return;
      setDetail(row);
      const url = new URL(window.location.href);
      url.searchParams.set("posting", id);
      url.searchParams.set("snapshot_id", result.page.snapshot_id);
      window.history.pushState({ ...window.history.state }, "", url);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not inspect posting."); }
  };
  const close = () => {
    setDetail(null);
    const url = new URL(window.location.href);
    url.searchParams.delete("posting"); url.searchParams.delete("snapshot_id");
    window.history.replaceState(window.history.state, "", url);
  };
  const postingHref = (id: string) => {
    const params = new URLSearchParams();
    if (query.q) params.set("q", query.q);
    if (query.decision) params.set("decision", query.decision.length === decisions.length ? "all" : query.decision[0]);
    if (query.source?.[0]) params.set("source", query.source[0]);
    if (result) params.set("snapshot_id", result.page.snapshot_id);
    params.set("posting", id);
    return `/jobs?${params}`;
  };
  const p = detail?.data;
  const application = p && safeExternalUrl(p.application_destination.application_url);

  return <div className="jobs-page">
    <header className="page-head"><h1>Jobs</h1><p className="scope-line">{client.display_name} · Individual postings · Registered service evidence</p></header>
    <p className="metadata">Individual postings include jobs without a recorded group representative. <Link href="/jobs?view=groups">View delivery groups</Link></p>
    <div className="toolbar">
      <form className="search-form" onSubmit={e => { e.preventDefault(); apply(); }}>
        <label htmlFor="postings-search"><span className="sr-only">Search postings</span><input id="postings-search" type="search" maxLength={200} value={search} onChange={e => setSearch(e.target.value)} placeholder="Search role, company or location" /></label>
        <button type="submit" disabled={busy}>Search</button>
      </form>
      <label>Match <select value={decision} onChange={e => setDecision(e.target.value)}><option value="default">Strong + Possible</option><option value="all">All decisions</option>{decisions.map(d => <option key={d} value={d}>{decisionLabel[d]}</option>)}</select></label>
      <label>Source <select value={source} onChange={e => setSource(e.target.value)}><option value="">All sources</option>{["greenhouse", "ashby", "lever", "workday"].map(s => <option key={s} value={s}>{s}</option>)}</select></label>
      <button onClick={() => apply()} disabled={busy}>Apply filters</button><button onClick={() => apply(true)} disabled={busy}>Clear filters</button>
    </div>
    <p className="notice">{result ? `${result.meta.completeness} reported coverage · ${result.meta.source_failures.length} recorded source failure(s).` : "Loading service evidence…"}</p>
    {error && <div role="alert" className="error">{error} <button onClick={() => apply(true)}>Refresh postings</button></div>}
    <div className={`workbench ${p ? "is-open" : ""}`}>
      <div className="list-region" aria-busy={busy} role="region" aria-label="Postings list">
        {!result ? <p>{error ? "No postings loaded." : "Loading postings…"}</p> : result.data.length === 0 ? <div className="empty"><h2>No postings match these filters</h2><button onClick={() => apply(true)}>Clear filters</button></div> : <>
          <table className="jobs-table"><caption className="sr-only">Individual postings from registered service evidence</caption><thead><tr><th>Role</th><th>Company</th><th>Location / mode</th><th>Match</th><th>Source</th><th>First seen</th><th>Outcome</th></tr></thead><tbody>{result.data.map(row => <tr key={row.posting_id} className={p?.posting_id === row.posting_id ? "inspected" : ""}><td><a className="job-link" href={postingHref(row.posting_id)} onClick={e => { if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return; e.preventDefault(); void inspect(row.posting_id); }}>{row.title}</a></td><td>{row.company}</td><td>{row.location_text ?? "Unknown"} · {row.remote_status}</td><td><MatchStatus decision={row.match?.decision} /></td><td>{row.source}</td><td>{dateText(row.first_seen_at)}</td><td>{outcomeText(row.outcome_summary)}</td></tr>)}</tbody></table>
          <div className="mobile-jobs">{result.data.map(row => <article className="mobile-job" key={row.posting_id}><a className="job-link" href={postingHref(row.posting_id)} onClick={e => { if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return; e.preventDefault(); void inspect(row.posting_id); }}>{row.title}</a><div className="mobile-meta">{row.company} · <MatchStatus decision={row.match?.decision} /></div><p className="metadata">{row.location_text ?? "Unknown"} · {row.source}</p></article>)}</div>
        </>}
        {result && <div className="pagination"><span>{result.data.length} shown · {result.page.known_total.availability === "reported" ? `${metricText(result.page.known_total)} postings in this scope` : "Total not reported"}</span><div className="pager-actions"><button disabled={!result.page.previous_cursor || busy} onClick={() => void load({ ...query, cursor: result.page.previous_cursor! })}>Previous</button><button disabled={!result.page.next_cursor || busy} onClick={() => void load({ ...query, cursor: result.page.next_cursor! })}>Next</button></div></div>}
      </div>
      {p && <aside className="detail" aria-labelledby="posting-title"><div className="close-row"><span className="metadata">Individual posting · service evidence</span><button onClick={close}>Close detail</button></div><h2 id="posting-title">{p.title}</h2><p>{p.company} · {p.location_text ?? "Location unknown"} · {p.remote_status}</p><MatchStatus decision={p.match?.decision} /><section><h3>Why it matched</h3>{p.match?.matched_reasons.length ? <ul>{p.match.matched_reasons.map(reason => <li key={reason}>{reason}</li>)}</ul> : <p>{p.match ? "No matched reasons recorded." : "Match evidence not reported."}</p>}{p.match?.review_reasons.availability === "reported" && <p>Review: {p.match.review_reasons.value.join(", ")}</p>}{!!p.match?.rejection_reasons.length && <p>Rejection: {p.match.rejection_reasons.join(", ")}</p>}</section><section><h3>Description</h3><p>{p.description_text ?? "Description not reported."}</p></section><section><h3>Delivery and outcome</h3><p>Group: {p.delivery_group_id ?? "Not recorded"}</p><p>Previously delivered: {factText(p.delivery_state.previously_delivered)}</p><p>Outcome: {outcomeText(p.outcome_summary)}</p></section><section><h3>Application</h3>{application ? <a className="application" href={application} rel="noopener noreferrer" target="_blank">Open vacancy <span className="sr-only">(opens external site)</span></a> : <p>Application URL unavailable</p>}</section><details><summary>Provenance</summary><p>Posting ID: <code>{p.posting_id}</code></p><p>Source: {p.source} · {p.source_board_id}</p><p>First seen: {p.first_seen_at ?? "Not recorded"}</p><p>Brief revision: {p.match?.brief_revision_id ?? "Not reported"}</p></details></aside>}
    </div>
  </div>;
}
