"use client";
export default function ErrorPage({ reset }: { reset: () => void }) {
  return (
    <>
      <h1>Could not load this view</h1>
      <p role="alert">
        The frontend encountered an error. This does not indicate a sourcing run
        failure.
      </p>
      <button onClick={reset}>Reload view</button>
    </>
  );
}
