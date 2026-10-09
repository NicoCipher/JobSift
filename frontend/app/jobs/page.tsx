import { redirect } from "next/navigation";
import { JobsWorkspace } from "@/components/jobs-workspace";
import { PostingsWorkspace } from "@/components/postings-workspace";
import { api, liveMode, operatorMode } from "@/lib/api/client";

export const metadata = { title: "Jobs" };
export const dynamic = "force-dynamic";

export default async function JobsPage({
  searchParams,
}: {
  searchParams: Promise<{ view?: string }>;
}) {
  if (operatorMode) redirect("/operations");

  const session = await api.getSession();
  const client = await api.getClient(session.data.client_scopes[0].client_id);
  const { view } = await searchParams;
  return liveMode && view !== "groups" ? (
    <PostingsWorkspace client={client.data} />
  ) : (
    <JobsWorkspace client={client.data} />
  );
}
