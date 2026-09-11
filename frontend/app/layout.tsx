import type { Metadata } from "next";
import { api } from "@/lib/api/client";
import { Shell } from "@/components/shell";
import "@/styles/global.css";
export const metadata: Metadata = {
  title: { default: "JobSift · Operator workbench", template: "%s · JobSift" },
  description:
    "Read-only JobSift operator frontend using visibly fictional development evidence.",
};
const preferenceScript = `(function(){try{var p=JSON.parse(localStorage.getItem('jobsift-presentation')||'{}');if(['light','dark','system'].includes(p.theme))document.documentElement.dataset.theme=p.theme;if(['default','compact','comfortable'].includes(p.density))document.documentElement.dataset.density=p.density}catch(e){}})()`;
export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const session = await api.getSession();
  const client = await api.getClient(session.data.client_scopes[0].client_id);
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: preferenceScript }} />
      </head>
      <body>
        <Shell client={client.data} session={session.data}>
          {children}
        </Shell>
      </body>
    </html>
  );
}
