import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const owner = "NicoCipher";
const repo = "JobSift";
const workflows = {
  inventory: "refresh-live-inventory.yml",
  delivery: "client-delivery-control.yml",
} as const;

type GithubRun = {
  id: number;
  event: string;
  status: string;
  conclusion: string | null;
  created_at: string;
  updated_at: string;
  html_url: string;
  run_number: number;
  head_sha: string;
};

type GithubWorkflow = {
  id: number;
  name: string;
  state: string;
  html_url: string;
};

async function github<T>(path: string, token: string): Promise<T> {
  const headers: Record<string, string> = {
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "JobSift-operator-control",
    Authorization: `Bearer ${token}`,
  };
  const response = await fetch(`https://api.github.com${path}`, {
    headers,
    cache: "no-store",
    signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) throw new Error("GitHub control status is unavailable.");
  return (await response.json()) as T;
}

async function workflowStatus(workflow: string, token: string) {
  const [definition, runs] = await Promise.all([
    github<GithubWorkflow>(
      `/repos/${owner}/${repo}/actions/workflows/${encodeURIComponent(workflow)}`,
      token,
    ),
    github<{ workflow_runs: GithubRun[] }>(
      `/repos/${owner}/${repo}/actions/workflows/${encodeURIComponent(workflow)}/runs?per_page=8`,
      token,
    ),
  ]);
  return {
    name: definition.name,
    state: definition.state,
    url: definition.html_url,
    runs: runs.workflow_runs.map((run) => ({
      id: run.id,
      run_number: run.run_number,
      event: run.event,
      status: run.status,
      conclusion: run.conclusion,
      created_at: run.created_at,
      updated_at: run.updated_at,
      url: run.html_url,
      head_sha: run.head_sha,
    })),
  };
}

export async function GET() {
  const token = process.env.JOBSIFT_GITHUB_TOKEN?.trim() ?? "";
  if (!token) {
    const unavailable = (name: string, workflow: string) => ({
      name,
      state: "unavailable",
      url: `https://github.com/${owner}/${repo}/actions/workflows/${workflow}`,
      runs: [],
    });
    return NextResponse.json(
      {
        data: {
          control_ready: false,
          inventory: unavailable("Refresh Live Job Inventory", workflows.inventory),
          delivery: unavailable("Client Delivery Control", workflows.delivery),
        },
      },
      { headers: { "Cache-Control": "no-store" } },
    );
  }
  try {
    const [inventory, delivery] = await Promise.all([
      workflowStatus(workflows.inventory, token),
      workflowStatus(workflows.delivery, token),
    ]);
    return NextResponse.json(
      {
        data: {
          control_ready: true,
          inventory,
          delivery,
        },
      },
      { headers: { "Cache-Control": "no-store" } },
    );
  } catch {
    return NextResponse.json(
      {
        error: {
          code: "CONTROL_STATUS_UNAVAILABLE",
          message: "Could not load JobSift workflow status.",
        },
      },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }
}
