import Link from "next/link";
import { notFound } from "next/navigation";
import { api } from "@/lib/api/client";
import { factText, metricText, outcomeLabels } from "@/lib/display";
import { PresentationSettings } from "@/components/preferences";
const titles: Record<string, string> = {
  dashboard: "Dashboard",
  review: "Review",
  briefs: "Search Briefs",
  runs: "Runs",
  history: "History",
  diagnostics: "Diagnostics",
  settings: "Settings",
};
export function generateStaticParams() {
  return Object.keys(titles).map((section) => ({ section }));
}
export async function generateMetadata({
  params,
}: {
  params: Promise<{ section: string }>;
}) {
  const { section } = await params;
  return { title: titles[section] ?? "Not found" };
}
export default async function SectionPage({
  params,
}: {
  params: Promise<{ section: string }>;
}) {
  const { section } = await params;
  if (!titles[section]) notFound();
  const session = await api.getSession();
  const clientId = session.data.client_scopes[0].client_id;
  let content: React.ReactNode;
  if (section === "settings")
    content = (
      <section className="section-block reading">
        <h2>Presentation</h2>
        <PresentationSettings />
      </section>
    );
  else if (section === "briefs") {
    const { data: brief } = await api.getBrief(clientId);
    content = (
      <>
        <section className="section-block">
          <h2>SearchBrief {brief.revision_label}</h2>
          <dl className="facts">
            <dt>Client identity</dt>
            <dd>
              <code>{brief.client_id}</code>
            </dd>
            <dt>Brief revision</dt>
            <dd>
              <code>{brief.brief_revision_id}</code> · sourcing revision{" "}
              {brief.revision_label}
            </dd>
            <dt>Schema version</dt>
            <dd>
              <code>{brief.schema_version}</code>
            </dd>
            <dt>Active binding</dt>
            <dd>{factText(brief.binding)}</dd>
          </dl>
        </section>
        <section className="section-block">
          <h2>Sourcing rules</h2>
          <dl className="facts">
            <dt>Target market · Required</dt>
            <dd>
              {brief.rules.target_market.countries.join(", ")} · Unknown
              evidence: {brief.rules.target_market.unknown_policy}
            </dd>
            <dt>Work mode · Required</dt>
            <dd>
              {brief.rules.work_mode.modes.join(", ")} · Unknown evidence:{" "}
              {brief.rules.work_mode.unknown_policy}
            </dd>
            <dt>Role vocabulary</dt>
            <dd>{brief.rules.target_roles.join(", ")}</dd>
            <dt>Preferred terms</dt>
            <dd>{brief.rules.preferred_terms.join(", ")}</dd>
            <dt>Excluded titles</dt>
            <dd>{brief.rules.excluded_titles.join(", ")}</dd>
            <dt>Ignored dimensions</dt>
            <dd>
              Employment type: {brief.rules.employment_type.intent}; work
              eligibility: {brief.rules.work_eligibility.intent}
            </dd>
          </dl>
        </section>
        <details>
          <summary>Revision provenance</summary>
          <p>
            Fictional registered artifact. Created at:{" "}
            {brief.created_at ?? "Not recorded"}.
          </p>
          <code>{brief.content_sha256}</code>
        </details>
        <p className="metadata">
          Revision creation and activation are not implemented.
        </p>
      </>
    );
  } else if (section === "runs") {
    const response = await api.getRun(clientId);
    const run = response.data;
    const labels: Record<string, string> = {
      received: "Acquired",
      matched: "Strong / Possible",
      rejected: "Not delivery-eligible",
      exported: "CSV rows appended",
      normalized: "Normalized",
      requests: "Provider requests",
    };
    content = (
      <>
        <section className="section-block">
          <h2>
            {run.status === "partial"
              ? "Partial — retained results with a source failure"
              : run.status === "success"
                ? "Success"
                : "Failure"}
          </h2>
          <p>
            Recorded source results and failure evidence are retained below.
          </p>
          <dl className="facts">
            <dt>Run</dt>
            <dd>
              <code>{run.run_id}</code>
            </dd>
            <dt>Plan / revision</dt>
            <dd>
              {run.plan_id} / {run.brief_revision_id ?? "Not reported"}
            </dd>
            <dt>Started / completed</dt>
            <dd>
              {run.started_at} / {run.completed_at}
            </dd>
            <dt>Completeness</dt>
            <dd>{response.meta.completeness} · fictional evidence</dd>
          </dl>
          <table className="data-table">
            <caption className="sr-only">
              Run accounting from available fixture results
            </caption>
            <thead>
              <tr>
                <th scope="col">Stage</th>
                <th scope="col" className="number">
                  Count
                </th>
                <th scope="col">Unit</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(run.metrics).map(([key, metric]) => (
                <tr key={key}>
                  <th scope="row">{labels[key]}</th>
                  <td className="number">{metricText(metric)}</td>
                  <td>{metric.unit}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="metadata">
            Not delivery-eligible includes both Rejected and Needs review. CSV
            rows are not a fresh-group count.
          </p>
        </section>
        <section className="section-block">
          <h2>Source targets</h2>
          {run.targets.map((target) => (
            <div className="member" key={target.target_identity}>
              <h3>
                {target.source} · {target.status}
              </h3>
              <p>{target.explanation}</p>
              <code>{target.target_identity}</code>
            </div>
          ))}
        </section>
        <p className="metadata">
          Run execution, retry and cancellation are not implemented.
        </p>
      </>
    );
  } else if (section === "diagnostics") {
    const { data } = await api.getDiagnostics(clientId);
    content = (
      <section className="section-block">
        <h2>{data.evidence_label}</h2>
        <p>
          These counts illustrate the published baseline semantics. They are not
          live jobs, new exports, or daily capacity.
        </p>
        <table className="data-table">
          <caption className="sr-only">
            Frozen baseline example: separate units, not an inventory total
          </caption>
          <thead>
            <tr>
              <th scope="col">Measure</th>
              <th scope="col" className="number">
                Count
              </th>
              <th scope="col">Unit</th>
            </tr>
          </thead>
          <tbody>
            {data.rows.map((row) => (
              <tr key={row.key}>
                <th scope="row">{row.label}</th>
                <td className="number">{metricText(row.metric)}</td>
                <td>{row.metric.unit}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {data.rows.map((row) => (
          <p key={row.key} className="metadata">
            {row.label}:{" "}
            {row.denominator
              ? `${metricText(row.denominator)} ${row.denominator.unit} in the frozen evaluation denominator.`
              : "No shared posting denominator for equivalent groups."}{" "}
            Contributing records unavailable in this fixture.
          </p>
        ))}
        <details>
          <summary>Evidence scope</summary>
          <p>
            <code>{data.cohort_id}</code> ·{" "}
            <code>{data.brief_revision_id}</code>
          </p>
          <p>
            The frozen 56-group equivalent baseline and 62 eligible postings
            describe different units. This frontend does not calculate either
            value from job rows.
          </p>
        </details>
      </section>
    );
  } else if (section === "history") {
    const { data } = await api.getHistory(clientId);
    content = (
      <section className="section-block">
        <h2>Operator memory</h2>
        <p>
          Imported history and recorded delivery are separate evidence. Not
          applied does not mean unsurfaced.
        </p>
        {data.map((entry) => (
          <article className="member" key={entry.history_entry_id}>
            <h3>
              {entry.title} · {entry.company}
            </h3>
            <p>
              {entry.event_type === "imported_history"
                ? "Imported history"
                : "Previously delivered"}{" "}
              · {outcomeLabels[entry.operator_status]}
            </p>
            <p className="metadata">
              {entry.recorded_at} ·{" "}
              {entry.destination_id ?? "Client-scoped history; no destination"}
            </p>
            <p>{entry.evidence_ref}</p>
          </article>
        ))}
      </section>
    );
  } else if (section === "review")
    content = (
      <section className="section-block reading">
        <h2>Inspect uncertain evidence</h2>
        <p>
          The fictional Needs review records retain their matcher decisions and
          reasons. They are not delivery-eligible.
        </p>
        <Link href="/jobs?decision=needs_review">
          Inspect Needs review postings through their example groups
        </Link>
        <p className="metadata">
          This slice provides evidence inspection in Jobs. A dedicated decision
          queue and persisted review dispositions are not implemented.
        </p>
      </section>
    );
  else
    content = (
      <>
        <section className="section-block">
          <h2>Attention</h2>
          <p>Partial source coverage is recorded in the example run.</p>
          <div className="section-links">
            <Link href="/runs">Inspect partial run</Link>
            <Link href="/jobs?decision=needs_review">
              Inspect Needs review evidence
            </Link>
          </div>
        </section>
        <section className="section-block">
          <h2>Waiting matches</h2>
          <p>
            Inspect the fictional Strong match and Possible groups in the Jobs
            work surface.
          </p>
          <Link href="/jobs">Open Jobs</Link>
        </section>
        <section className="section-block">
          <h2>Activity and capacity evidence</h2>
          <p>
            This is a read-only frontend slice. No live health or capacity is
            inferred.
          </p>
          <div className="section-links">
            <Link href="/runs">Latest example run</Link>
            <Link href="/diagnostics">Frozen baseline example</Link>
          </div>
        </section>
      </>
    );
  return (
    <div className="section-content">
      <header className="page-head">
        <h1>{titles[section]}</h1>
        <p className="scope-line">
          Example client · Read-only development evidence
        </p>
      </header>
      {content}
    </div>
  );
}
