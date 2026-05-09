/**
 * GET/POST /api/internal/forward-drift — Vercel Cron proxy for drift.scan.
 *
 * Why this proxy exists:
 *
 *   Vercel Cron does NOT support custom request headers, so we cannot
 *   set X-Cron-Token directly on the cron entry. Instead the cron call
 *   hits this proxy (Vercel injects an `Authorization: Bearer
 *   <CRON_SECRET>` header on its own per Vercel's documented contract),
 *   the proxy verifies the bearer, then issues a server-to-server POST
 *   to FastAPI POST /api/drift/run with X-Cron-Token attached.
 *
 *   The proxy NEVER forwards the user's session — the cron runs as a
 *   service identity and FastAPI fans out one drift.scan job per
 *   active user.
 *
 * vercel.json schedule lives at the monorepo `apps/web/vercel.json`:
 *
 *     { "crons": [{ "path": "/api/internal/forward-drift", "schedule": "0 *\/6 * * *" }] }
 *
 * Refs: PLAN.md Sprint 9 chunk 9.7; system-design 13.5, 13.7.
 */

import { timingSafeEqual } from "node:crypto"
import { NextRequest } from "next/server"

const apiBase = () => process.env.API_URL ?? "http://localhost:8000"

// SSRF guard (security-reviewer F3): the cron proxy forwards CRON_SECRET to
// `apiBase()`, so a misconfigured API_URL pointing at loopback/link-local /
// cloud-metadata addresses would leak the secret. Production must use https.
const FORBIDDEN_HOSTS: ReadonlySet<string> = new Set([
  "localhost",
  "127.0.0.1",
  "0.0.0.0",
  "[::1]",
  "::1",
  "169.254.169.254", // AWS / GCP / Azure metadata
  "metadata.google.internal",
])

function isAllowedApiUrl(value: string): boolean {
  let url: URL
  try {
    url = new URL(value)
  } catch {
    return false
  }
  if (process.env.NODE_ENV === "production" && url.protocol !== "https:") {
    return false
  }
  if (process.env.NODE_ENV !== "production") {
    // Local dev resolves to the docker-compose api hostname or localhost.
    return true
  }
  const host = url.hostname.toLowerCase()
  if (FORBIDDEN_HOSTS.has(host)) return false
  if (host.startsWith("127.")) return false
  if (host.startsWith("169.254.")) return false
  if (host.startsWith("fe80:")) return false
  return true
}

function verifyVercelCron(req: NextRequest): boolean {
  const expected = process.env.CRON_SECRET ?? ""
  if (!expected) return false
  // Vercel signs cron requests with `Authorization: Bearer <CRON_SECRET>`.
  const auth = req.headers.get("authorization") ?? ""
  if (!auth.startsWith("Bearer ")) return false
  const token = auth.slice("Bearer ".length).trim()
  // Constant-time comparison via Node's crypto.timingSafeEqual. The
  // length check IS itself a timing oracle for the secret length, but
  // CRON_SECRET is fixed-width per rotation, so length is not secret.
  if (token.length !== expected.length) return false
  return timingSafeEqual(Buffer.from(token, "utf8"), Buffer.from(expected, "utf8"))
}

async function forward(req: NextRequest) {
  if (!verifyVercelCron(req)) {
    return new Response(JSON.stringify({ detail: "Unauthorized" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    })
  }
  const target = apiBase()
  if (!isAllowedApiUrl(target)) {
    // Fail-loud: misconfigured API_URL must surface as a 500 in logs,
    // not silently exfiltrate the secret to the wrong host.
    return new Response(JSON.stringify({ detail: "Misconfigured upstream" }), {
      status: 500,
      headers: { "Content-Type": "application/json" },
    })
  }
  try {
    const upstream = await fetch(`${target}/api/drift/run`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Cron-Token": process.env.CRON_SECRET ?? "",
      },
      body: JSON.stringify({}),
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

// Vercel Cron fires GET — only export GET to keep the surface small
// (security-reviewer M-2). POST returns 405 by default for unhandled
// methods on a Next.js Route Handler.
export const GET = forward
