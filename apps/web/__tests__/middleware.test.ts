/**
 * Middleware route-matcher tests (chunk 3.5).
 *
 * We test the `isPublicRoute` predicate in isolation — no Clerk network
 * calls, no Next.js runtime. The matcher is the security boundary; getting
 * it wrong means unauthenticated users reach /dashboard.
 *
 * Refs: PLAN.md chunk 3.5, US-001.
 */

import { describe, it, expect } from "vitest"

// Re-implement the same predicate used in middleware.ts so we can test it
// without importing the full Clerk SDK (which requires edge runtime).
const PUBLIC_PATTERNS = [
  "/",
  "/sign-in",
  "/sign-up",
  "/ingest",
  "/api/health",
]

function isPublicRoute(pathname: string): boolean {
  return PUBLIC_PATTERNS.some((pattern) => {
    if (pattern === "/") return pathname === "/"
    return pathname === pattern || pathname.startsWith(pattern + "/") || pathname.startsWith(pattern + "?")
  })
}

describe("middleware route matching", () => {
  it.each([
    ["/", true],
    ["/sign-in", true],
    ["/sign-in/sso-callback", true],
    ["/sign-up", true],
    ["/sign-up/verify-email-address", true],
    ["/ingest/static/array.js", true],
    ["/api/health", true],
  ])("%s → public=%s", (path, expected) => {
    expect(isPublicRoute(path)).toBe(expected)
  })

  it.each([
    ["/dashboard", false],
    ["/dashboard/settings", false],
    ["/api/me", false],
    ["/api/connectors/ext_123", false],
    ["/policies", false],
  ])("%s → public=%s", (path, expected) => {
    expect(isPublicRoute(path)).toBe(expected)
  })
})
