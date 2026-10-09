import type { Metadata } from "next";
import { Shell } from "@/components/shell";
import { api, liveMode, operatorMode } from "@/lib/api/client";
import "@/styles/global.css";
import "@/styles/visual-refresh.css";

export const dynamic = "force-dynamic";
export const metadata: Metadata = {
  title: { default: "JobSift · Operator workbench", template: "%s · JobSift" },
  description: operatorMode
    ? "JobSift production operator workspace for client sourcing and delivery controls."
    : liveMode
      ? "Read-only JobSift operator frontend for registered local service evidence."
      : "Read-only JobSift operator frontend using visibly fictional development evidence.",
};

const preferenceScript = `(function(){try{var p=JSON.parse(localStorage.getItem('jobsift-presentation')||'{}');if(['light','dark','system'].includes(p.theme))document.documentElement.dataset.theme=p.theme;if(['default','compact','comfortable'].includes(p.density))document.documentElement.dataset.density=p.density}catch(e){}})()`;

export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const shellContext = operatorMode
    ? null
    : await (async () => {
        const session = await api.getSession();
        const client = await api.getClient(session.data.client_scopes[0].client_id);
        return { session: session.data, client: client.data };
      })();

  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: preferenceScript }} />
      </head>
      <body>
        <Shell
          client={shellContext?.client ?? null}
          session={shellContext?.session ?? null}
        >
          {children}
        </Shell>
      </body>
    </html>
  );
}
