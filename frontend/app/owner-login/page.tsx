import { redirect } from "next/navigation";
import { ownerAuthConfigured, ownerAuthRequired } from "../../lib/owner-auth";
import { OperatorNotice } from "@/components/operator-notice";
import { UiIcon } from "@/components/ui-icon";

// Dynamic and no-store: never cache an authenticated operator response.
export const dynamic = "force-dynamic";
export const metadata = { title: "JobSift Owner Sign-In" };

export default async function OwnerLogin({ searchParams }: {
  searchParams: Promise<{ error?: string }>;
}) {
  if (!ownerAuthRequired()) redirect("/operations");
  const { error } = await searchParams;
  const configured = ownerAuthConfigured();
  return (
    <div className="section-content owner-entry">
      <div className="owner-entry-icon"><UiIcon name="lock" size={30} /></div>
      <p className="page-eyebrow">PRIVATE OPERATOR ACCESS</p>
      <h1>Welcome back</h1>
      <p>Unlock your JobSift workspace to manage clients, review jobs and control deliveries.</p>
      {!configured ? (
        <OperatorNotice tone="error" title="Owner access is not configured"><p>The server-only owner credentials must be configured before sign-in can work. No private controls are available.</p></OperatorNotice>
      ) : (
        <form method="POST" action="/api/owner/session">
          <label htmlFor="owner-access-key">Owner access key</label>
          <input id="owner-access-key" name="access_key" type="password"
            autoComplete="current-password" required minLength={43} maxLength={43}
            aria-describedby="owner-key-help" aria-invalid={error === "invalid"} />
          <p id="owner-key-help">Use the private access key from your password manager.</p>
          {error === "invalid" ? <OperatorNotice tone="error" title="That access key did not work"><p>Check the key and try again. No access was granted.</p></OperatorNotice> : null}
          <button type="submit"><UiIcon name="lock" size={18} /> Unlock JobSift</button>
        </form>
      )}
    </div>
  );
}
