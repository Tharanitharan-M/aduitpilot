/**
 * GET /api/scan-runs/diff?a=&b= — proxy to FastAPI scan_runs diff (chunk 9.12).
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
  // typescript-reviewer H-3: extract + validate the only two query
  // params we actually use. UUIDs only.
  const a = req.nextUrl.searchParams.get("a") ?? ""
  const b = req.nextUrl.searchParams.get("b") ?? ""
  const uuidLike = /^[0-9a-fA-F-]{1,64}$/
  if (!a || !b || !uuidLike.test(a) || !uuidLike.test(b)) {
    return new Response(JSON.stringify({ detail: "Invalid 'a' / 'b' query params" }), {
      status: 422,
      headers: { "Content-Type": "application/json" },
    })
  }
  const search = `?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`
  try {
    const upstream = await fetch(`${apiBase()}/api/scan-runs/diff${search}`, {
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
