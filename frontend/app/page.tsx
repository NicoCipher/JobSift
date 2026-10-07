import { redirect } from "next/navigation";
import { liveMode } from "@/lib/api/client";

export default function Home() {
  redirect(liveMode ? "/clients" : "/jobs");
}
