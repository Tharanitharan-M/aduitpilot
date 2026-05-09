/**
 * GET /api/drift/events — proxy to FastAPI GET /api/drift/events.
 *
 * Refs: PLAN.md Sprint 9 chunks 9.5, 9.8, 9.9.
 */

import { auth } from "@clerk/nextjs/server"
import { NextRequest } from "next/server"

const apiBase = () => process.env.API_URL ?? "http://localhost:8000"

export async function GET(req: NextRequest) {
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
  // typescript-reviewer H-3 — extract only known query params instead
  // of forwarding the full search string verbatim.
  const allowedStatuses = new Set(["open", "dismissed", "resolved"])
  const status = req.nextUrl.searchParams.get("status")
  const limit = req.nextUrl.searchParams.get("limit")
  const params = new URLSearchParams()
  if (status && allowedStatuses.has(status)) params.set("status", status)
  if (limit && /^\d{1,4}$/.test(limit)) params.set("limit", limit)
  const search = params.toString() ? `?${params.toString()}` : ""
  try {
    const upstream = await fetch(`${apiBase()}/api/drift/events${search}`, {
      headers: { Authorization: `Bearer ${token}` },
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
