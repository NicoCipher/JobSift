"use client";
import { useEffect, useRef } from "react";
import type { JobGroupDetail } from "@/lib/contracts/service";
import {
  applicationLabel,
  factText,
  isTyping,
  outcomeText,
  safeExternalUrl,
} from "@/lib/display";
import { MatchStatus } from "./status";
export function JobDetail({
  job,
  onClose,
}: {
  job: JobGroupDetail;
  onClose: () => void;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    heading.current?.focus();
  }, [job.delivery_group_id]);
  const p = job.representative_posting;
  const match = job.match;
  const destination = p?.application_destination;
  const url = destination ? safeExternalUrl(destination.application_url) : null;
  return (
    <aside
      className="detail"
      aria-labelledby="detail-title"
      onKeyDown={(e) => {
        if (e.key === "Escape" && !e.altKey && !e.ctrlKey && !e.metaKey && !isTyping(e.target)) {
          e.stopPropagation();
          onClose();
        }
      }}
    >
      <div className="close-row">
        <span className="metadata">Job detail · fictional evidence</span>
        <button onClick={onClose}>Close detail</button>
      </div>
      <h2 ref={heading} tabIndex={-1} id="detail-title">
        {p?.title ?? "Representative not reported"}
      </h2>
      <p>{p?.company ?? "Company not reported"}</p>
      <p>
        {p?.location_text ?? "Location unknown"} ·{" "}
        {p?.remote_status ?? "unknown"}
      </p>
      <MatchStatus decision={match?.decision} />
      <section>
        <h3>Why it matched</h3>
        {match?.matched_reasons.length ? (
          <ul>
            {match.matched_reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        ) : (
          <p>
            {match
              ? "No matched reasons recorded."
              : "Match evidence not reported."}
          </p>
        )}
      </section>
      {match?.decision === "needs_review" && (
        <section>
          <h3>Why it needs review</h3>
          {match.review_reasons.availability === "reported" ? (
            <ul>
              {match.review_reasons.value.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          ) : (
            <p>{factText(match.review_reasons)}</p>
          )}
        </section>
      )}
      {!!match?.rejection_reasons.length && (
        <section>
          <h3>Why it was rejected</h3>
          <ul>
            {match.rejection_reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </section>
      )}
      <section>
        <h3>
          {job.delivery_state.non_delivery_reasons.availability ===
            "reported" &&
          job.delivery_state.non_delivery_reasons.value.length > 0
            ? "Why it wasn’t delivered"
            : "Delivery evidence"}
        </h3>
        {job.delivery_state.non_delivery_reasons.availability === "reported" ? (
          <ul>
            {job.delivery_state.non_delivery_reasons.value.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        ) : (
          <p>
            Delivery reason: {factText(job.delivery_state.non_delivery_reasons)}
          </p>
        )}
        <p className="metadata">
          Destination: {job.delivery_state.destination_id ?? "Not selected"}
        </p>
      </section>
      <section>
        <h3>Application destination</h3>
        {destination && url ? (
          <>
            <a
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              className="application"
            >
              {applicationLabel(destination)}
              <span className="sr-only">
                {" "}
                (opens external example site in a new tab)
              </span>
            </a>
            <p className="metadata">{new URL(url).hostname} · fictional link</p>
          </>
        ) : (
          <p>Application URL unavailable</p>
        )}
      </section>
      <section>
        <h3>Operator outcome</h3>
        <p>{outcomeText(job.outcome_summary)}</p>
        <p className="metadata">
          Read-only evidence. Outcome editing is not implemented.
        </p>
      </section>
      <details>
        <summary>Provenance</summary>
        <dl>
          <dt>Source</dt>
          <dd>{p?.source ?? "Not reported"}</dd>
          <dt>Source target</dt>
          <dd>
            <code>{job.provenance.source_target}</code>
          </dd>
          <dt>Posting ID</dt>
          <dd>
            <code>{p?.posting_id ?? "Not reported"}</code>
          </dd>
          <dt>Group ID</dt>
          <dd>
            <code>{job.delivery_group_id}</code>
          </dd>
          <dt>First seen</dt>
          <dd>{job.provenance.first_seen_at ?? "Not recorded"}</dd>
          <dt>Last seen</dt>
          <dd>{job.provenance.last_seen_at ?? "Not recorded"}</dd>
          <dt>Brief revision</dt>
          <dd>{job.provenance.brief_revision_id ?? "Not reported"}</dd>
          <dt>Run</dt>
          <dd>{job.provenance.run_id ?? "Not reported"}</dd>
          <dt>Destination</dt>
          <dd>{job.delivery_state.destination_id ?? "Not selected"}</dd>
          <dt>Previously delivered</dt>
          <dd>
            {factText(job.delivery_state.previously_delivered, (v) =>
              v ? "Yes" : "No",
            )}
          </dd>
          <dt>Historical suppression</dt>
          <dd>
            {factText(job.delivery_state.historical_suppression, (v) =>
              v ? "Yes" : "No",
            )}
          </dd>
          <dt>Fresh for delivery</dt>
          <dd>
            {factText(job.delivery_state.fresh_for_delivery, (v) =>
              v ? "Yes" : "No",
            )}
          </dd>
          <dt>Evidence</dt>
          <dd>{job.provenance.evidence_ref}</dd>
        </dl>
      </details>
      <details>
        <summary>Group members ({job.members.length} shown)</summary>
        <p className="metadata">
          Member evidence: {job.member_completeness}. Each posting retains its
          own decision.
        </p>
        {job.members.map((member) => (
          <div className="member" key={member.posting_id}>
            <h3>{member.title}</h3>
            <p>
              {member.source} · <code>{member.posting_id}</code>
            </p>
            <MatchStatus decision={member.match?.decision} />
          </div>
        ))}
        <p>{factText(job.grouping_evidence, (value) => value.join(" "))}</p>
      </details>
      <details>
        <summary>Description</summary>
        <p>{job.description_text}</p>
      </details>
    </aside>
  );
}
