import { randomUUID } from "node:crypto";
import { NextRequest, NextResponse } from "next/server";
import { ownerApiGuard } from "../../../../lib/owner-auth";
import { validProvisioningKey } from "../../../../lib/provisioning-crypto";

export const dynamic = "force-dynamic";

const apiBase = "https://api.github.com/repos/NicoCipher/JobSift";
const workflowPath = "/actions/workflows/operator-client-provision.yml";
const requiredSecrets = [
  "JOBSIFT_PROVISIONING_KEY",
  "NEON_DATABASE_URL",
  "GOOGLE_SERVICE_ACCOUNT_JSON",
] as const;

type GithubProbe = {
  status: number | null;
  body: Record<string, unknown> | null;
};

function response(data: unknown, status = 200, traceId?: string) {
  const body = traceId && data && typeof data === "object" && !Array.isArray(data)
    ? { ...(data as Record<string, unknown>), meta: { trace_id: traceId } }
    : data;
  return NextResponse.json(body, {
    status,
    headers: {
      "Cache-Control": "no-store",
      ...(traceId ? { "X-JobSift-Trace-Id": traceId } : {}),
    },
  });
}

async function githubGet(path: string, token: string): Promise<GithubProbe> {
  try {
    const result = await fetch(apiBase + path, {
      method: "GET",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: "Bearer " + token,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "JobSift-operator-health",
      },
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
    });
    let body: Record<string, unknown> | null = null;
    if (result.ok) {
      try {
        const parsed = await result.json();
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
          body = parsed as Record<string, unknown>;
        }
      } catch {
        body = null;
      }
    }
    return { status: result.status, body };
  } catch {
    return { status: null, body: null };
  }
}

function statusLabel(status: number | null) {
  if (status === 200) return "available";
  if (status === 401 || status === 403) return "access_denied";
  if (status === 404) return "missing";
  return "unavailable";
}

export async function GET(request: NextRequest) {
  const traceId = randomUUID();
  const ownerDenied = ownerApiGuard(request);
  if (ownerDenied) {
    ownerDenied.headers.set("X-JobSift-Trace-Id", traceId);
    return ownerDenied;
  }

  const origin = request.headers.get("origin");
  const site = request.headers.get("sec-fetch-site");
  if ((origin && origin !== request.nextUrl.origin) || (site && site !== "same-origin")) {
    return response(
      { error: { code: "FORBIDDEN", message: "Cross-origin health checks are not allowed." } },
      403,
      traceId,
    );
  }

  const token = process.env.JOBSIFT_GITHUB_TOKEN?.trim() ?? "";
  const keyConfigured = validProvisioningKey(
    process.env.JOBSIFT_PROVISIONING_KEY?.trim() ?? "",
  );
  if (!token) {
    return response(
      {
        data: {
          ready: false,
          summary: "The GitHub connection is not configured on this deployment.",
          checks: {
            github_token: "missing",
            provisioning_key: keyConfigured ? "configured" : "invalid",
            actions_public_key: "not_checked",
            workflow: "not_checked",
            repository_secrets: {},
            secret_write_permission: "not_verified",
            database_connectivity: "not_checked",
            google_sheets_authentication: "not_checked",
          },
          checked_at: new Date().toISOString(),
        },
      },
      200,
      traceId,
    );
  }

  const [publicKey, workflow, ...secretProbes] = await Promise.all([
    githubGet("/actions/secrets/public-key", token),
    githubGet(workflowPath, token),
    ...requiredSecrets.map((name) => githubGet("/actions/secrets/" + name, token)),
  ]);

  const publicKeyBody = publicKey.body;
  const encodedPublicKey =
    typeof publicKeyBody?.key === "string" ? publicKeyBody.key : "";
  const decodedPublicKey = encodedPublicKey
    ? Buffer.from(encodedPublicKey, "base64")
    : Buffer.alloc(0);
  const publicKeyValid = Boolean(
    publicKey.status === 200 &&
    decodedPublicKey.length === 32 &&
    decodedPublicKey.toString("base64") === encodedPublicKey &&
    typeof publicKeyBody?.key_id === "string" &&
    publicKeyBody.key_id,
  );
  const workflowActive =
    workflow.status === 200 && workflow.body?.state === "active";
  const workflowStatus = workflowActive
    ? "active"
    : workflow.status === 200
      ? "inactive"
      : statusLabel(workflow.status);
  const publicKeyStatus = publicKeyValid
    ? "available"
    : publicKey.status === 200
      ? "invalid_response"
      : statusLabel(publicKey.status);
  const repositorySecrets = Object.fromEntries(
    requiredSecrets.map((name, index) => [
      name,
      {
        status: secretProbes[index].status === 200
          ? "present"
          : statusLabel(secretProbes[index].status),
        http_status: secretProbes[index].status,
      },
    ]),
  );
  const allSecretsPresent = secretProbes.every((probe) => probe.status === 200);
  const ready = keyConfigured && publicKeyValid && workflowActive && allSecretsPresent;

  let summary = "Backend preflight passed. The worker check will verify database connectivity and Google authentication.";
  if (!keyConfigured) {
    summary = "The secure provisioning key is missing or invalid in Vercel Production.";
  } else if (!publicKeyValid) {
    summary = "GitHub denied or failed the Actions secrets API check. Check the token's repository access and Secrets permissions.";
  } else if (!workflowActive) {
    summary = "The GitHub provisioning workflow is missing, inaccessible, or inactive.";
  } else if (!allSecretsPresent) {
    summary = "One or more required GitHub Actions secrets are missing or inaccessible.";
  }

  console.info("[JobSift onboarding] backend preflight", {
    trace_id: traceId,
    ready,
    github_public_key: publicKey.status,
    workflow: workflow.status,
    repository_secrets: secretProbes.map((probe) => probe.status),
  });

  return response(
    {
      data: {
        ready,
        summary,
        checks: {
          github_token: "configured",
          provisioning_key: keyConfigured ? "configured" : "invalid",
          actions_public_key: {
            status: publicKeyStatus,
            http_status: publicKey.status,
          },
          workflow: {
            status: workflowStatus,
            http_status: workflow.status,
          },
          repository_secrets: repositorySecrets,
          secret_write_permission: "not_verified",
          secret_value_match: "not_verifiable",
          database_connectivity: "not_checked",
          google_sheets_authentication: "not_checked",
        },
        checked_at: new Date().toISOString(),
      },
    },
    200,
    traceId,
  );
}
