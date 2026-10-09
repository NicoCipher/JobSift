import Link from "next/link";
import { liveMode, operatorMode } from "@/lib/api/client";
export default function NotFound() {
  return (
    <>
      <h1>Page not found</h1>
      <p>This route is not part of the operator workbench.</p>
      <Link href={operatorMode ? "/operations" : liveMode ? "/clients" : "/jobs"}>
        Return to {operatorMode ? "Operations" : liveMode ? "Clients" : "Jobs"}
      </Link>
    </>
  );
}
