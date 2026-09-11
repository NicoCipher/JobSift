import { api } from "@/lib/api/client";
import { JobsWorkspace } from "@/components/jobs-workspace";
export const metadata = { title: "Jobs" };
export default async function JobsPage() {
  const session = await api.getSession();
  const client = await api.getClient(session.data.client_scopes[0].client_id);
  return <JobsWorkspace client={client.data} />;
}
