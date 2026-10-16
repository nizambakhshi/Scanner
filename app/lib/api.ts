/**
 * Route-handler plumbing.
 *
 * Every JSON endpoint in this app is: (1) session-guarded, (2) served from the
 * same `ScannerSource` the pages use, so the API and the rendered page can never
 * disagree about a number, and (3) returns a *safe* error string — the database
 * message is truncated and a DSN can never appear in it, which matters because a
 * browser shows whatever a 500 body contains.
 */

import { NextResponse } from "next/server";
import { requireAdminApi, requireUserApi } from "./auth/session";
import { panelError } from "./source";
import type { SessionUser } from "./auth/users";

export type { SessionUser };
export type Guard = { user: SessionUser; denied: null } | { user: null; denied: NextResponse };

export function json(data: unknown, status = 200): NextResponse {
  return NextResponse.json(data, { status, headers: { "Cache-Control": "no-store" } });
}

export function fail(error: string, status = 400, extra: Record<string, unknown> = {}): NextResponse {
  return NextResponse.json({ error, ...extra }, { status, headers: { "Cache-Control": "no-store" } });
}

/** Best-effort client identity: behind Vercel this is the real visitor's address. */
export function clientIp(request: Request): string | null {
  const headers = new Headers(request.headers);
  const forwarded = headers.get("x-forwarded-for");
  if (forwarded) return forwarded.split(",")[0]?.trim() ?? null;
  return headers.get("x-real-ip");
}

export async function guard(): Promise<Guard> {
  const user: SessionUser | null = await requireUserApi();
  if (!user) return { user: null, denied: fail("Sign in to use this endpoint.", 401) };
  return { user, denied: null };
}

/** The same gate, admin-only: a plain user's cookie authenticates them, but a
 *  control endpoint answers 403. */
export async function adminGuard(): Promise<Guard> {
  const user: SessionUser | null = await requireAdminApi();
  if (!user) return { user: null, denied: fail("This endpoint is for the Super Admin.", 403) };
  return { user, denied: null };
}

/** Run a read, mapping any failure to a short safe message with a 500. */
export async function read<T>(operation: () => Promise<T>): Promise<NextResponse> {
  try {
    return json(await operation());
  } catch (error) {
    return fail(panelError(error), 500);
  }
}

export async function body(request: Request): Promise<Record<string, string>> {
  const type = request.headers.get("content-type") ?? "";
  const out: Record<string, string> = {};
  try {
    if (type.includes("application/json")) {
      const payload = (await request.json()) as Record<string, unknown>;
      for (const [key, value] of Object.entries(payload ?? {})) out[key] = typeof value === "string" ? value : String(value ?? "");
      return out;
    }
    const form = await request.formData();
    for (const [key, value] of form.entries()) out[key] = typeof value === "string" ? value : "";
    return out;
  } catch {
    return out;
  }
}
