import type { Metadata } from 'next';
import type { ReactNode } from 'react';
import './globals.css';

export const metadata: Metadata = {
  title: 'Coherence Fund — LP Portal',
  description:
    'Limited-partner statements, capital-call notices, and distribution notices for the Coherence Fund.',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-slate-50 text-slate-900 antialiased">
        <main className="mx-auto max-w-4xl px-6 py-12">{children}</main>
      </body>
    </html>
  );
}
