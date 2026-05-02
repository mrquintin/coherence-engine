import type { Metadata } from "next";
import type { ReactNode } from "react";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Coherence Fund — Partner Dashboard",
  description:
    "Coherence Fund partner dashboard: pipeline pivot, decision artifacts, manual review overrides.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-slate-50 text-slate-900 antialiased">
        <header className="border-b border-slate-200 bg-white">
          <nav className="mx-auto flex max-w-6xl items-center justify-between px-6 py-4">
            <Link href="/pipeline" className="font-semibold tracking-tight">
              Coherence Fund · Partner
            </Link>
            <ul className="flex items-center gap-4 text-sm">
              <li>
                <Link href="/pipeline" className="text-slate-700 hover:text-slate-900">
                  Pipeline
                </Link>
              </li>
              <li>
                <Link href="/audit" className="text-slate-700 hover:text-slate-900">
                  Audit
                </Link>
              </li>
            </ul>
          </nav>
        </header>
        <main className="mx-auto max-w-6xl px-6 py-8">{children}</main>
      </body>
    </html>
  );
}
