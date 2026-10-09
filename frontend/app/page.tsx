import { redirect } from "next/navigation";
import { liveMode, operatorMode } from "@/lib/api/client";

export default function Home() {
  redirect(operatorMode ? "/operations" : liveMode ? "/clients" : "/jobs");
}
