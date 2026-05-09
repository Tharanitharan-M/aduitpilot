/**
 * PATCH /api/drift/events/{id} — proxy to FastAPI PATCH /api/drift/events/{id}.
 *
 * Refs: PLAN.md Sprint 9 chunks 9.8, 9.9.
 */

import { auth } from "@clerk/nextjs/server"
import { NextRequest } from "next/server"
import { z } from "zod"

const apiBase = () => process.env.API_URL ?? "http://localhost:8000"

// typescript-reviewer H-2: validate the body at the BFF boundary so
// malformed payloads never reach FastAPI.
const PatchBody = z.object({
  status: z.enum(["dismissed", "resolved"]),
  reason: z.string().max(2000).optional(),
})

export async function PATCH(req: NextRequest, { params }: { params: Promise<{ id: string }> }) {
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
  let validatedBody: string
  try {
    const raw = await req.text()
    const parsed = PatchBody.safeParse(raw ? JSON.parse(raw) : {})
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
    const upstream = await fetch(`${apiBase()}/api/drift/events/${encodeURIComponent(id)}`, {
      method: "PATCH",
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
