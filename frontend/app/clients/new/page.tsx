import { ClientOnboardingWizard } from "@/components/client-onboarding-wizard";
import { UiIcon } from "@/components/ui-icon";
import { operatorMode } from "@/lib/api/client";
import Link from "next/link";

export const metadata = { title: "Add client" };

export default function NewClientPage() {
  const provisioningReady =
    !operatorMode ||
    Boolean(
      process.env.JOBSIFT_GITHUB_TOKEN?.trim() &&
      process.env.JOBSIFT_PROVISIONING_KEY?.trim(),
    );

  return (
    <div className="section-content">
      <header className="page-head">
        <p className="page-eyebrow">CLIENT SETUP</p>
        <h1>Add a client</h1>
        <p className="scope-line">Tell JobSift what to find and where approved jobs should go.</p>
        <Link href="/clients" className="operator-text-link">
          <UiIcon name="arrow-right" size={16} /> Back to clients
        </Link>
      </header>
      {provisioningReady ? (
        <ClientOnboardingWizard />
      ) : (
        <section className="section-block" role="status" aria-live="polite">
          <h2>Client setup is not ready yet</h2>
          <p>
            JobSift cannot securely create a client right now. No client or Google
            Sheet has been changed. The one-time setup connection needs to be
            completed before you try again.
          </p>
          <Link href="/clients" className="client-primary-action">
            Return to clients
          </Link>
        </section>
      )}
    </div>
  );
}
