/**
 * GET /api/scan-runs        — proxy list
 * POST /api/scan-runs       — proxy re-run (Sprint 9 chunk 9.10/9.11)
 *
 * Refs: PLAN.md Sprint 9 chunks 9.10, 9.11.
 */

import { auth } from "@clerk/nextjs/server"
import { NextRequest } from "next/server"
import { z } from "zod"

const apiBase = () => process.env.API_URL ?? "http://localhost:8000"

// typescript-reviewer H-2: validate at the BFF boundary.
const RerunBody = z.object({
  source: z.literal("rerun"),
  parent: z.string().min(1).max(64),
  params_override: z.record(z.unknown()).optional().default({}),
})

async function authHeaders(): Promise<HeadersInit | null> {
  const { userId, getToken } = await auth()
  if (!userId) return null
  const token = await getToken()
  if (!token) return null
  return { Authorization: `Bearer ${token}` }
}

export async function GET(req: NextRequest) {
  const headers = await authHeaders()
  if (headers === null) {
    return new Response(JSON.stringify({ detail: "Unauthorized" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    })
  }
  try {
    const upstream = await fetch(`${apiBase()}/api/scan-runs`, {
      headers,
      signal: req.signal,
    })
    const ct = upstream.headers.get("Content-Type") ?? "application/json"
    return new Response(upstream.body, {
      status: upstream.status,
      headers: { "Content-Type": ct },
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

export async function POST(req: NextRequest) {
  const headers = await authHeaders()
  if (headers === null) {
    return new Response(JSON.stringify({ detail: "Unauthorized" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    })
  }
  let validatedBody: string
  try {
    const raw = await req.text()
    const parsed = RerunBody.safeParse(raw ? JSON.parse(raw) : {})
    if (!parsed.success) {
      return new Response(
        JSON.stringify({ detail: "Invalid request body", issues: parsed.error.issues }),
        { status: 422, headers: { "Content-Type": "application/json" } },
      )
    }
    validatedBody = JSON.stringify(parsed.data)
  } catch {
    return new Response(JSON.stringify({ detail: "Malformed JSON" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    })
  }
  try {
    const upstream = await fetch(`${apiBase()}/api/scan-runs`, {
      method: "POST",
      headers: { ...headers, "Content-Type": "application/json" },
      body: validatedBody,
      signal: req.signal,
    })
    const ct = upstream.headers.get("Content-Type") ?? "application/json"
    return new Response(upstream.body, {
      status: upstream.status,
      headers: { "Content-Type": ct },
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
