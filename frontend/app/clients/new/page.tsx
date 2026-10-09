import { ClientOnboardingWizard } from "@/components/client-onboarding-wizard";
import Link from "next/link";
import { UiIcon } from "@/components/ui-icon";

export const metadata = { title: "Add client" };

export default function NewClientPage() {
  return (
    <div className="section-content">
      <header className="page-head"><p className="page-eyebrow">CLIENT SETUP</p><h1>Add a client</h1><p className="scope-line">Tell JobSift what to find and where approved jobs should go.</p><Link href="/clients" className="operator-text-link"><UiIcon name="arrow-right" size={16}/> Back to clients</Link></header>
      <ClientOnboardingWizard />
    </div>
  );
}
