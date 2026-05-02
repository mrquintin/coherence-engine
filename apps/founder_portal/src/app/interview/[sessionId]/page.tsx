import { InterviewUi } from "@/components/interview_ui";

export const dynamic = "force-dynamic";

interface PageProps {
  params: { sessionId: string };
}

export default function InterviewPage({ params }: PageProps) {
  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <p className="text-sm font-medium uppercase tracking-wide text-slate-500">
          Founder interview
        </p>
        <h1 className="text-3xl font-semibold tracking-tight">In-browser voice interview</h1>
        <p className="text-sm text-slate-600">
          Audio is captured locally in 5-second chunks and uploaded directly to object storage. We
          will stitch the chunks server-side after you end the session.
        </p>
      </header>
      <InterviewUi sessionId={params.sessionId} />
    </div>
  );
}
