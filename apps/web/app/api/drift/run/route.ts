/**
 * POST /api/drift/run — proxy to FastAPI POST /api/drift/run for the
 * authenticated user (single-user enqueue path).
 *
 * The cron path uses /api/internal/forward-drift; this route is the
 * UI-facing affordance ("Run drift scan now" button).
 *
 * Refs: PLAN.md Sprint 9 chunk 9.6.
 */

import { auth } from "@clerk/nextjs/server"
import { NextRequest } from "next/server"
import { z } from "zod"

const apiBase = () => process.env.API_URL ?? "http://localhost:8000"

// typescript-reviewer H-2: drift run takes no caller-supplied fields
// (the user_ids field is ignored on the user path), but still validate
// to reject pollution.
const RunBody = z.object({}).strict()

export async function POST(req: NextRequest) {
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
  let validatedBody: string
  try {
    const raw = await req.text()
    const parsed = RunBody.safeParse(raw ? JSON.parse(raw) : {})
    if (!parsed.success) {
      return new Response(
        JSON.stringify({ detail: "Body must be empty object {}" }),
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
    const upstream = await fetch(`${apiBase()}/api/drift/run`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
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
