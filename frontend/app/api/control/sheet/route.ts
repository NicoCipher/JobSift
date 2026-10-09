import { NextRequest, NextResponse } from "next/server";
import { ownerApiGuard } from "../../../../lib/owner-auth";
import { readSheetHandle } from "../../../../lib/sheet-handle";

export const dynamic = "force-dynamic";

function githubToken() {
  return process.env.JOBSIFT_GITHUB_TOKEN?.trim() ?? "";
}

export async function GET(request: NextRequest) {
  const ownerDenied = ownerApiGuard(request);
  if (ownerDenied) return ownerDenied;
  const handle = request.nextUrl.searchParams.get("handle") ?? "";
  const value = readSheetHandle(githubToken(), handle);
  if (!value) {
    return NextResponse.json(
      {
        error: {
          code: "INVALID_SHEET_HANDLE",
          message: "This Sheet link expired. Return to Clients and open it again.",
        },
      },
      { status: 403, headers: { "Cache-Control": "no-store" } },
    );
  }
  return NextResponse.redirect(value.sheet_url, {
    status: 303,
    headers: { "Cache-Control": "no-store" },
  });
}
