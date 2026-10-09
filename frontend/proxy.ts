import { NextRequest, NextResponse } from "next/server";
import { isOwner, ownerAuthConfigured, ownerAuthRequired } from "./lib/owner-auth";

export function proxy(request: NextRequest) {
  if (!ownerAuthRequired()) return NextResponse.next();

  const path = request.nextUrl.pathname;
  // Only the owner login form and its own POST endpoint are unauthenticated.
  if (path === "/owner-login" || path === "/api/owner/session") {
    return NextResponse.next();
  }

  if (ownerAuthConfigured() && isOwner(request)) return NextResponse.next();
  if (path.startsWith("/api/")) {
    return NextResponse.json(
      { error: { code: ownerAuthConfigured() ? "OWNER_AUTH_REQUIRED" : "OWNER_AUTH_NOT_CONFIGURED",
        message: "Owner sign-in is required." } },
      { status: ownerAuthConfigured() ? 401 : 503, headers: { "Cache-Control": "no-store" } },
    );
  }

  const destination = new URL("/owner-login", request.url);
  if (!ownerAuthConfigured()) destination.searchParams.set("error", "setup");
  const response = NextResponse.redirect(destination);
  response.headers.set("Cache-Control", "no-store");
  return response;
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|robots.txt|sitemap.xml).*)"],
};
