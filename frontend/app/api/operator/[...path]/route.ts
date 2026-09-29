import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

async function read(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const base = process.env.JOBSIFT_SERVICE_URL;
  if (!base || !/^http:\/\/127\.0\.0\.1:\d+$/.test(base)) {
    return NextResponse.json({ error: { code: "EVIDENCE_UNAVAILABLE", message: "Local operator service is not configured." } }, { status: 503 });
  }
  const { path } = await context.params;
  if (!path.length || path.some((part) => !part || part === "." || part === ".." || /[\\/\u0000-\u001f\u007f]/.test(part))) {
    return NextResponse.json({ error: { code: "VALIDATION_ERROR", message: "Invalid resource path." } }, { status: 400 });
  }
  const upstream = new URL(`/api/v1/${path.map(encodeURIComponent).join("/")}`, base);
  upstream.search = request.nextUrl.search;
  try {
    const response = await fetch(upstream, {
      method: "GET",
      cache: "no-store",
      signal: AbortSignal.timeout(10000),
      headers: { Host: new URL(base).host },
    });
    return new NextResponse(response.body, {
      status: response.status,
      headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
    });
  } catch {
    return NextResponse.json({ error: { code: "EVIDENCE_UNAVAILABLE", message: "Local operator service is unavailable." } }, { status: 503, headers: { "Cache-Control": "no-store" } });
  }
}

export { read as GET };
