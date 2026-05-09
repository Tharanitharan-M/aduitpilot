/**
 * GET /api/mock-audit/:id — proxy to FastAPI for run + findings detail.
 *
 * Refs: PLAN.md Sprint 8 chunk 8.6.
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
    const upstream = await fetch(
      `${apiBase()}/api/mock-audit/${idResult.data}`,
      {
        headers: { Authorization: `Bearer ${token}` },
        signal: req.signal,
      }
    )
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
