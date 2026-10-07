import { ClientOnboardingWizard } from "@/components/client-onboarding-wizard";

export const metadata = { title: "Add client" };

export default function NewClientPage() {
  return (
    <div className="section-content">
      <h1>Add client</h1>
      <ClientOnboardingWizard />
    </div>
  );
}
