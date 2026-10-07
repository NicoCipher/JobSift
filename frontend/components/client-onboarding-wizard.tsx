"use client";

import Link from "next/link";
import { FormEvent, useMemo, useRef, useState } from "react";

type SheetInspection = {
  tabs: string[];
  selected_tab: string;
  headers: string[];
  proposed_mapping: Record<string, string>;
  missing_required_fields: string[];
};

type CreatedClient = {
  client_name: string;
  destination_name: string;
  delivery_mode: string;
  daily_limit: number | null;
  sheet_status: string;
  brief_revision: number | null;
};

type PendingAction = {
  control_request_id: string;
};

type ApiError = { error?: { message?: string } };

const mappingFields = [
  { key: "Job Title", label: "Job title", required: true },
  { key: "Company Name", label: "Company", required: true },
  { key: "Job Link", label: "Application link", required: true },
  { key: "Job Platform", label: "Source", required: false },
  { key: "Job Description", label: "Description", required: false },
] as const;

function splitTerms(value: string) {
  return value
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

async function readJson(response: Response) {
  const body = (await response.json()) as ApiError & { data?: unknown };
  if (!response.ok) {
    throw new Error(body.error?.message ?? "JobSift request failed.");
  }
  return body;
}

function validRequestId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
      value,
    )
  );
}

export function ClientOnboardingWizard() {
  const [step, setStep] = useState(1);
  const [clientName, setClientName] = useState("");
  const [roles, setRoles] = useState("");
  const [country, setCountry] = useState("US");
  const [workModes, setWorkModes] = useState<string[]>(["remote"]);
  const [exclusions, setExclusions] = useState("");
  const [preferred, setPreferred] = useState("");
  const [freshness, setFreshness] = useState("24");
  const [dailyLimit, setDailyLimit] = useState("100");
  const [deliveryMode, setDeliveryMode] = useState<"review" | "auto">("review");
  const [sheetUrl, setSheetUrl] = useState("");
  const [sheetTab, setSheetTab] = useState("Jobs");
  const [inspection, setInspection] = useState<SheetInspection | null>(null);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [created, setCreated] = useState<CreatedClient | null>(null);
  const pollTimer = useRef<number | null>(null);

  const roleTitles = useMemo(() => splitTerms(roles), [roles]);
  const excludedTerms = useMemo(() => splitTerms(exclusions), [exclusions]);
  const preferredTerms = useMemo(() => splitTerms(preferred), [preferred]);

  const requiredMapped = mappingFields
    .filter((field) => field.required)
    .every((field) => Boolean(mapping[field.key]));

  const canContinue =
    (step === 1 && clientName.trim().length > 0) ||
    (step === 2 &&
      roleTitles.length > 0 &&
      /^[A-Z]{2}$/.test(country.trim().toUpperCase()) &&
      workModes.length > 0) ||
    (step === 3 &&
      Number.isInteger(Number(dailyLimit)) &&
      Number(dailyLimit) >= 1 &&
      Number(dailyLimit) <= 5000) ||
    (step === 4 && inspection !== null && requiredMapped);

  function toggleMode(mode: string) {
    setWorkModes((current) =>
      current.includes(mode)
        ? current.filter((value) => value !== mode)
        : [...current, mode],
    );
  }

  async function pollAction(
    controlRequestId: string,
    onReady: (value: Record<string, unknown>) => void,
    attempt = 0,
  ) {
    try {
      const response = await fetch(
        "/api/onboarding?control_request_id=" +
          encodeURIComponent(controlRequestId),
        { cache: "no-store" },
      );
      const body = (await readJson(response)) as {
        data?: {
          state?: unknown;
          result?: unknown;
          message?: unknown;
        };
      };
      const state = typeof body.data?.state === "string" ? body.data.state : "";
      if (state === "ready" && body.data?.result && typeof body.data.result === "object") {
        setBusy(false);
        setMessage("");
        onReady(body.data.result as Record<string, unknown>);
        return;
      }
      if (state === "failed") {
        setBusy(false);
        setMessage(
          typeof body.data?.message === "string"
            ? body.data.message
            : "JobSift could not complete this step.",
        );
        return;
      }
      if (attempt >= 60) {
        setBusy(false);
        setMessage("This action did not finish. Try again from the current step.");
        return;
      }
      const delay = attempt < 8 ? 1500 : 5000;
      pollTimer.current = window.setTimeout(
        () => void pollAction(controlRequestId, onReady, attempt + 1),
        delay,
      );
    } catch (error) {
      setBusy(false);
      setMessage(error instanceof Error ? error.message : "Could not read onboarding status.");
    }
  }

  async function dispatch(
    operation: "inspect_sheet" | "create_client",
    payload: Record<string, unknown>,
    onReady: (value: Record<string, unknown>) => void,
  ) {
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch("/api/onboarding", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ operation, payload }),
      });
      const body = (await readJson(response)) as { data?: PendingAction };
      const requestId = body.data?.control_request_id;
      if (!validRequestId(requestId)) {
        throw new Error("JobSift could not correlate this onboarding action.");
      }
      await pollAction(requestId, onReady);
    } catch (error) {
      setBusy(false);
      setMessage(error instanceof Error ? error.message : "Could not start onboarding.");
    }
  }

  async function inspectSheet(event: FormEvent) {
    event.preventDefault();
    if (!sheetUrl.trim() || !sheetTab.trim()) {
      setMessage("Paste the Google Sheet URL and enter the tab name.");
      return;
    }
    setInspection(null);
    setMapping({});
    await dispatch(
      "inspect_sheet",
      {
        sheet_url: sheetUrl.trim(),
        tab: sheetTab.trim(),
      },
      (value) => {
        const inspected = value as unknown as SheetInspection;
        setInspection(inspected);
        setMapping(inspected.proposed_mapping ?? {});
      },
    );
  }

  async function createClient() {
    if (!requiredMapped || !inspection) return;
    await dispatch(
      "create_client",
      {
        client_name: clientName.trim(),
        criteria: {
          role_titles: roleTitles,
          country: country.trim().toUpperCase(),
          work_modes: workModes,
          exclusions: excludedTerms,
          preferred_terms: preferredTerms,
          freshness_hours: Number(freshness),
        },
        daily_limit: Number(dailyLimit),
        delivery_mode: deliveryMode,
        sheet: {
          url: sheetUrl.trim(),
          tab: sheetTab.trim(),
          column_mapping: mapping,
        },
      },
      (value) => setCreated(value as unknown as CreatedClient),
    );
  }

  if (created) {
    return (
      <section className="section-block onboarding-complete">
        <div className="completion-mark" aria-hidden="true">✓</div>
        <h2>{created.client_name} is ready</h2>
        <p>
          JobSift can now match the shared inventory against this client and route
          eligible jobs through {created.delivery_mode === "auto" ? "Auto" : "Review"} delivery.
        </p>
        <div className="onboarding-summary">
          <div><span>Daily limit</span><strong>{created.daily_limit ?? "—"}</strong></div>
          <div><span>Sheet</span><strong>{created.sheet_status === "ready" ? "Connected" : "Needs attention"}</strong></div>
          <div><span>Criteria revision</span><strong>{created.brief_revision ?? "—"}</strong></div>
        </div>
        <div className="onboarding-actions">
          <Link href="/clients">Open client</Link>
        </div>
      </section>
    );
  }

  return (
    <div className="onboarding-wizard">
      <section className="section-block onboarding-head">
        <div>
          <p className="eyebrow">Add client · Step {step} of 5</p>
          <h2>
            {step === 1
              ? "Who is the client?"
              : step === 2
                ? "What jobs should JobSift find?"
                : step === 3
                  ? "How should jobs be delivered?"
                  : step === 4
                    ? "Connect the client Google Sheet"
                    : "Confirm this client"}
          </h2>
        </div>
        <Link href="/clients">Cancel</Link>
      </section>

      <ol className="onboarding-steps" aria-label="Client setup progress">
        {["Client", "Criteria", "Delivery", "Google Sheet", "Confirm"].map((label, index) => (
          <li
            key={label}
            className={index + 1 === step ? "current" : index + 1 < step ? "done" : ""}
          >
            <span>{index + 1}</span>{label}
          </li>
        ))}
      </ol>

      {message ? <div className="error" role="alert">{message}</div> : null}

      {step === 1 ? (
        <section className="section-block onboarding-panel">
          <label>
            Client name
            <input
              autoFocus
              value={clientName}
              onChange={(event) => setClientName(event.target.value)}
              placeholder="Example: Acme Software Search"
              maxLength={120}
            />
          </label>
          <p className="metadata">Use the name you recognize. JobSift generates internal IDs itself.</p>
        </section>
      ) : null}

      {step === 2 ? (
        <section className="section-block onboarding-panel">
          <label>
            Role titles
            <textarea
              value={roles}
              onChange={(event) => setRoles(event.target.value)}
              placeholder={"Software Engineer\nBackend Developer"}
              rows={4}
            />
            <span className="metadata">One per line or separated by commas.</span>
          </label>

          <div className="onboarding-two-col">
            <label>
              Target country
              <input
                value={country}
                onChange={(event) => setCountry(event.target.value.toUpperCase())}
                maxLength={2}
                aria-describedby="country-help"
              />
              <span id="country-help" className="metadata">Two-letter country code, e.g. US.</span>
            </label>
            <label>
              Freshness
              <select value={freshness} onChange={(event) => setFreshness(event.target.value)}>
                <option value="6">Last 6 hours</option>
                <option value="12">Last 12 hours</option>
                <option value="24">Last 24 hours</option>
              </select>
            </label>
          </div>

          <fieldset>
            <legend>Work mode</legend>
            <div className="choice-row">
              {[
                ["remote", "Remote"],
                ["hybrid", "Hybrid"],
                ["onsite", "On-site"],
              ].map(([value, label]) => (
                <label className="check-choice" key={value}>
                  <input
                    type="checkbox"
                    checked={workModes.includes(value)}
                    onChange={() => toggleMode(value)}
                  />
                  {label}
                </label>
              ))}
            </div>
          </fieldset>

          <label>
            Exclude
            <textarea
              value={exclusions}
              onChange={(event) => setExclusions(event.target.value)}
              placeholder="Senior Director, clearance required"
              rows={3}
            />
            <span className="metadata">Words or phrases that should count against a match.</span>
          </label>
          <label>
            Preferred terms <span className="metadata">(optional)</span>
            <textarea
              value={preferred}
              onChange={(event) => setPreferred(event.target.value)}
              placeholder="Python, backend, distributed systems"
              rows={3}
            />
          </label>
        </section>
      ) : null}

      {step === 3 ? (
        <section className="section-block onboarding-panel">
          <label>
            Daily limit
            <input
              type="number"
              min={1}
              max={5000}
              value={dailyLimit}
              onChange={(event) => setDailyLimit(event.target.value)}
            />
          </label>
          <fieldset>
            <legend>Delivery mode</legend>
            <label className="radio-choice">
              <input
                type="radio"
                name="delivery-mode"
                checked={deliveryMode === "review"}
                onChange={() => setDeliveryMode("review")}
              />
              <span><strong>Review first</strong><small>You see the jobs before anything is sent.</small></span>
            </label>
            <label className="radio-choice">
              <input
                type="radio"
                name="delivery-mode"
                checked={deliveryMode === "auto"}
                onChange={() => setDeliveryMode("auto")}
              />
              <span><strong>Auto</strong><small>Eligible jobs can be sent automatically after backend safety checks.</small></span>
            </label>
          </fieldset>
        </section>
      ) : null}

      {step === 4 ? (
        <section className="section-block onboarding-panel">
          <form className="sheet-inspect-form" onSubmit={inspectSheet}>
            <label>
              Google Sheet URL
              <input
                type="url"
                value={sheetUrl}
                onChange={(event) => {
                  setSheetUrl(event.target.value);
                  setInspection(null);
                }}
                placeholder="https://docs.google.com/spreadsheets/d/..."
              />
            </label>
            <label>
              Tab
              <input
                value={sheetTab}
                onChange={(event) => {
                  setSheetTab(event.target.value);
                  setInspection(null);
                }}
                placeholder="Jobs"
              />
            </label>
            <button type="submit" disabled={busy}>
              {busy ? "Checking Sheet…" : "Inspect Sheet"}
            </button>
          </form>

          {inspection ? (
            <div className="sheet-inspection">
              <div className="control-ready">Sheet tab found · {inspection.headers.length} headers</div>
              <div className="header-chips" aria-label="Detected Sheet headers">
                {inspection.headers.map((header) => <span key={header}>{header}</span>)}
              </div>
              <h3>Column mapping</h3>
              <p className="metadata">JobSift proposed these mappings. Confirm the required three before continuing.</p>
              <div className="mapping-grid">
                {mappingFields.map((field) => (
                  <label key={field.key}>
                    {field.label}{field.required ? " *" : ""}
                    <select
                      value={mapping[field.key] ?? ""}
                      onChange={(event) =>
                        setMapping((current) => ({
                          ...current,
                          [field.key]: event.target.value,
                        }))
                      }
                    >
                      <option value="">Not mapped</option>
                      {inspection.headers.map((header) => (
                        <option key={header} value={header}>{header}</option>
                      ))}
                    </select>
                  </label>
                ))}
              </div>
              {!requiredMapped ? (
                <div className="notice">Map Job title, Company, and Application link to continue.</div>
              ) : null}
            </div>
          ) : null}
        </section>
      ) : null}

      {step === 5 ? (
        <section className="section-block onboarding-panel">
          <div className="confirm-client">
            <div><span>Client</span><strong>{clientName}</strong></div>
            <div><span>Roles</span><strong>{roleTitles.join(", ")}</strong></div>
            <div><span>Market</span><strong>{country.toUpperCase()} · {workModes.join(", ")}</strong></div>
            <div><span>Freshness</span><strong>Last {freshness} hours · unknown age rejected</strong></div>
            <div><span>Delivery</span><strong>{deliveryMode === "review" ? "Review first" : "Auto"} · up to {dailyLimit}/day</strong></div>
            <div><span>Sheet</span><strong>{sheetTab} · required columns mapped</strong></div>
          </div>
          <div className="notice">
            JobSift will still re-check freshness, quota, dedupe/history and Sheet state before every release.
          </div>
        </section>
      ) : null}

      <div className="onboarding-nav">
        <button
          type="button"
          className="secondary-action"
          disabled={busy || step === 1}
          onClick={() => {
            setMessage("");
            setStep((current) => Math.max(1, current - 1));
          }}
        >
          Back
        </button>
        {step < 5 ? (
          <button
            type="button"
            disabled={busy || !canContinue}
            onClick={() => {
              setMessage("");
              setStep((current) => Math.min(5, current + 1));
            }}
          >
            Continue
          </button>
        ) : (
          <button type="button" disabled={busy} onClick={() => void createClient()}>
            {busy ? "Creating client…" : "Create client"}
          </button>
        )}
      </div>
    </div>
  );
}
