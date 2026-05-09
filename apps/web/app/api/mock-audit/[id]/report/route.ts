/**
 * GET /api/mock-audit/:id/report — Markdown gap report proxy.
 *
 * Upstream returns 302 → pre-signed R2 URL when R2 is configured, or
 * streams Markdown bytes directly when running on local-fs storage.
 * We follow the redirect and forward the body so the browser sees a
 * normal text/markdown response.
 *
 * Refs: PLAN.md Sprint 8 chunk 8.7.
 */

import { auth } from "@clerk/nextjs/server"
import { NextRequest } from "next/server"
import { z } from "zod"

const apiBase = () => process.env.API_URL ?? "http://localhost:8000"
const IdSchema = z.string().regex(/^[0-9a-f\-]{1,64}$/, "Invalid run ID")

export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { userId, getToken } = await auth()
  if (!userId) {
    return new Response(JSON.stringify({ detail: "Unauthorized" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    })
  }
  const token = await getToken()
  if (!token) {
    return new Response(JSON.stringify({ detail: "Session expired" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    })
  }
  const { id } = await params
  const idResult = IdSchema.safeParse(id)
  if (!idResult.success) {
    return new Response(JSON.stringify({ detail: "Invalid run ID" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    })
  }
  try {
    // Use redirect: "manual" so we never forward the Clerk bearer token to
    // a Cloudflare R2 pre-signed URL when FastAPI returns a 302 (the R2
    // URL is self-authorising and an Authorization header on it would
    // both leak the token and be rejected by R2). On a redirect we hand
    // the Location back to the browser to follow without our headers.
    const upstream = await fetch(
      `${apiBase()}/api/mock-audit/${idResult.data}/report`,
      {
        headers: { Authorization: `Bearer ${token}` },
        signal: req.signal,
        redirect: "manual",
      }
    )
    if ([301, 302, 307, 308].includes(upstream.status)) {
      const location = upstream.headers.get("Location")
      if (location) return Response.redirect(location, 302)
      return new Response(JSON.stringify({ detail: "Redirect missing Location" }), {
        status: 502,
        headers: { "Content-Type": "application/json" },
      })
    }
    const ct = upstream.headers.get("Content-Type") ?? "text/markdown"
    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "Content-Type": ct,
        "Content-Disposition":
          upstream.headers.get("Content-Disposition") ??
          `attachment; filename="mock-audit-${idResult.data}.md"`,
      },
    })
  } catch (err) {
    if (err instanceof Error && err.name === "AbortError") {
      return new Response(null, { status: 499 })
    }
    return new Response(JSON.stringify({ detail: "Upstream error" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    })
  }
}
