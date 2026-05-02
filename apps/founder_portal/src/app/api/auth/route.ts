import { NextResponse, type NextRequest } from "next/server";
import { BackendApiError, createServerApiClient } from "@/lib/api_client";
import { createSupabaseServerClient, getServerAccessToken, getSupabaseUrl } from "@/lib/supabase";

export const runtime = "nodejs";

function authPageRedirect(): string {
  // Supabase hosts the email-link / OAuth UI; we redirect there from the
  // sign-in button on the landing page. The redirect target preserves the
  // origin so callbacks land back on the portal.
  const url = new URL("/auth/v1/authorize", getSupabaseUrl());
  url.searchParams.set("provider", "email");
  return url.toString();
}

async function readJson<T>(request: NextRequest): Promise<T | null> {
  try {
    return (await request.json()) as T;
  } catch {
    return null;
  }
}

async function readFormAction(request: NextRequest): Promise<string | null> {
  try {
    const form = await request.formData();
    const action = form.get("action");
    return typeof action === "string" ? action : null;
  } catch {
    return null;
  }
}

function backendErrorJson(err: unknown, fallbackStatus = 500) {
  if (err instanceof BackendApiError) {
    return NextResponse.json({ error: err.message, code: err.code }, { status: err.status });
  }
  return NextResponse.json(
    { error: err instanceof Error ? err.message : "request failed" },
    { status: fallbackStatus },
  );
}

export async function POST(request: NextRequest) {
  const contentType = request.headers.get("content-type") ?? "";

  if (contentType.includes("application/json")) {
    const body = await readJson<{ action?: string; payload?: unknown } & Record<string, unknown>>(
      request,
    );
    const action = body?.action;
    if (!body || !action) {
      return NextResponse.json({ error: "Unknown action" }, { status: 400 });
    }
    const accessToken = await getServerAccessToken();
    if (!accessToken) {
      return NextResponse.json({ error: "Not authenticated" }, { status: 401 });
    }

    if (action === "create_application") {
      try {
        const client = await createServerApiClient(accessToken);
        const result = await client.createApplication(
          body.payload as Parameters<typeof client.createApplication>[0],
        );
        return NextResponse.json({ application_id: result.data.application_id });
      } catch (err) {
        return backendErrorJson(err);
      }
    }

    if (action === "upload_initiate") {
      const application_id = String(body.application_id ?? "");
      if (!application_id) {
        return NextResponse.json({ error: "application_id required" }, { status: 400 });
      }
      try {
        const client = await createServerApiClient(accessToken);
        const result = await client.initiateUpload(application_id, {
          filename: String(body.filename ?? ""),
          content_type: String(body.content_type ?? ""),
          size_bytes: Number(body.size_bytes ?? 0),
          kind: (body.kind === "supporting" ? "supporting" : "deck") as "deck" | "supporting",
        });
        return NextResponse.json(result.data);
      } catch (err) {
        return backendErrorJson(err);
      }
    }

    if (action === "upload_complete") {
      const application_id = String(body.application_id ?? "");
      const upload_id = String(body.upload_id ?? "");
      if (!application_id || !upload_id) {
        return NextResponse.json(
          { error: "application_id and upload_id required" },
          { status: 400 },
        );
      }
      try {
        const client = await createServerApiClient(accessToken);
        const result = await client.completeUpload(application_id, upload_id);
        return NextResponse.json(result.data);
      } catch (err) {
        return backendErrorJson(err);
      }
    }

    return NextResponse.json({ error: "Unknown action" }, { status: 400 });
  }

  const action = await readFormAction(request);
  if (action === "signin") {
    return NextResponse.redirect(authPageRedirect(), { status: 302 });
  }
  if (action === "signout") {
    const supabase = createSupabaseServerClient();
    await supabase.auth.signOut();
    return NextResponse.redirect(new URL("/", request.url), { status: 302 });
  }
  return NextResponse.json({ error: "Unknown action" }, { status: 400 });
}

export async function GET(request: NextRequest) {
  const action = request.nextUrl.searchParams.get("action");
  if (action === "application_status") {
    const applicationId = request.nextUrl.searchParams.get("application_id");
    if (!applicationId) {
      return NextResponse.json({ error: "application_id required" }, { status: 400 });
    }
    const accessToken = await getServerAccessToken();
    if (!accessToken) {
      return NextResponse.json({ error: "Not authenticated" }, { status: 401 });
    }
    try {
      const client = await createServerApiClient(accessToken);
      const app = await client.getApplication(applicationId);
      return NextResponse.json(app.data);
    } catch (err) {
      return backendErrorJson(err);
    }
  }
  if (action === "artifact_url") {
    const applicationId = request.nextUrl.searchParams.get("application_id");
    if (!applicationId) {
      return NextResponse.json({ error: "application_id required" }, { status: 400 });
    }
    const accessToken = await getServerAccessToken();
    if (!accessToken) {
      return NextResponse.json({ error: "Not authenticated" }, { status: 401 });
    }
    try {
      const client = await createServerApiClient(accessToken);
      const artifact = await client.getDecisionArtifact(applicationId);
      return NextResponse.json({
        artifact_id: artifact.data.artifact_id,
        payload: artifact.data.payload,
      });
    } catch (err) {
      return backendErrorJson(err);
    }
  }
  return NextResponse.redirect(authPageRedirect(), { status: 302 });
}
