import Link from "next/link";
export default function NotFound() {
  return (
    <>
      <h1>Page not found</h1>
      <p>This route is not part of the operator workbench.</p>
      <Link href="/jobs">Return to Jobs</Link>
    </>
  );
}
