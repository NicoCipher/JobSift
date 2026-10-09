import { NextRequest, NextResponse } from "next/server";
import {
  OWNER_COOKIE,
  newOwnerSession,
  ownerAuthConfigured,
  validOwnerAccessKey,
  validOwnerLoginOrigin,
} from "../../../../lib/owner-auth";

export const dynamic = "force-dynamic";

export async function POST(request: NextRequest) {
  if (!validOwnerLoginOrigin(request)) {
    return NextResponse.json(
      { error: { code: "FORBIDDEN", message: "Invalid owner sign-in origin." } },
      { status: 403, headers: { "Cache-Control": "no-store" } },
    );
  }
  if (!ownerAuthConfigured()) {
    return NextResponse.redirect(new URL("/owner-login?error=setup", request.url), 303);
  }
  const type = request.headers.get("content-type") ?? "";
  if (!type.startsWith("application/x-www-form-urlencoded") &&
      !type.startsWith("multipart/form-data")) {
    return NextResponse.json(
      { error: { code: "VALIDATION_ERROR", message: "Use the owner sign-in form." } },
      { status: 400, headers: { "Cache-Control": "no-store" } },
    );
  }
  let key: unknown;
  try {
    const form = await request.formData();
    key = form.get("access_key");
  } catch {
    key = null;
  }
  if (typeof key !== "string" || !validOwnerAccessKey(key)) {
    return NextResponse.redirect(new URL("/owner-login?error=invalid", request.url), 303);
  }
  const session = newOwnerSession();
  if (!session) {
    return NextResponse.redirect(new URL("/owner-login?error=setup", request.url), 303);
  }
  const response = NextResponse.redirect(new URL("/operations", request.url), 303);
  response.headers.set("Cache-Control", "no-store");
  response.cookies.set(OWNER_COOKIE, session, {
    httpOnly: true,
    secure: request.nextUrl.protocol === "https:",
    sameSite: "strict",
    path: "/",
    maxAge: 24 * 60 * 60,
  });
  return response;
}
