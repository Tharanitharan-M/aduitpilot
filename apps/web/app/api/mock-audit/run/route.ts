/**
 * POST /api/mock-audit/run — proxy to FastAPI POST /api/mock-audit/run.
 *
 * Refs: PLAN.md Sprint 8 chunk 8.5.
 */

import { auth } from "@clerk/nextjs/server"
import { NextRequest } from "next/server"

const apiBase = () => process.env.API_URL ?? "http://localhost:8000"

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
  try {
    const body = await req.text()
    const upstream = await fetch(`${apiBase()}/api/mock-audit/run`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: body || "{}",
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
