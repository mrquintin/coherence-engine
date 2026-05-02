import { ApplicationStatusView } from "@/components/application_status_view";

export const dynamic = "force-dynamic";

interface PageProps {
  params: { id: string };
}

export default function ApplicationStatusPage({ params }: PageProps) {
  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <p className="text-sm font-medium uppercase tracking-wide text-slate-500">
          Application status
        </p>
        <h1 className="text-3xl font-semibold tracking-tight" data-testid="application-id">
          {params.id}
        </h1>
        <p className="text-sm text-slate-600">
          This page polls every 5 seconds. The decision artifact appears once the pipeline issues a
          verdict.
        </p>
      </header>
      <ApplicationStatusView applicationId={params.id} />
    </div>
  );
}
