import { redirect } from "next/navigation";
import { cookies } from "next/headers";
import { ownerAuthConfigured, ownerAuthRequired, OWNER_COOKIE, verifyOwnerSession } from "../../lib/owner-auth";

// Dynamic and no-store: never cache an authenticated operator response.
export const dynamic = "force-dynamic";
export const metadata = { title: "JobSift Owner Sign-In" };

export default async function OwnerLogin({ searchParams }: {
  searchParams: Promise<{ error?: string }>;
}) {
  if (!ownerAuthRequired()) redirect("/operations");
  const jar = await cookies();
  const { error } = await searchParams;
  // Auth verification happens in the guard on /operations. An authenticated
  // operator visiting this page follows that guard instead of seeing the form.
  const configured = ownerAuthConfigured();
  void jar; void OWNER_COOKIE; void verifyOwnerSession;
  return (
    <main className="section-content">
      <h1>Owner sign-in</h1>
      <p>Only the JobSift operator can access sourcing and client delivery controls.</p>
      {!configured ? (
        <p role="alert">Owner access is not configured. Set the server-only owner authentication variables before deploying this change.</p>
      ) : (
        <form method="POST" action="/api/owner/session">
          <label htmlFor="owner-access-key">Owner access key</label>
          <input id="owner-access-key" name="access_key" type="password"
            autoComplete="off" required minLength={43} maxLength={43}
            aria-describedby="owner-key-help" />
          <p id="owner-key-help">Use the private access key from your password manager.</p>
          {error === "invalid" ? <p role="alert">Invalid access key. Try again.</p> : null}
          <button type="submit">Unlock JobSift</button>
        </form>
      )}
    </main>
  );
}
