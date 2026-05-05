/**
 * GET /api/auth/github-callback
 *
 * Landing page after Clerk completes the GitHub OAuth flow.
 * Reads the stashed redirect_url cookie and sends the user back to a
 * same-origin path.
 *
 * Security: the redirect destination is validated to be a relative path
 * (must start with "/" and not with "//") so that a CSRF-planted or
 * injected cookie value cannot be used to redirect users to an external
 * domain — a canonical OAuth callback open-redirect vector.
 *
 * Refs: PLAN.md chunk 3.6, ADR-0008.
 */

import { auth } from "@clerk/nextjs/server"
import { NextRequest, NextResponse } from "next/server"

function safeRedirectPath(value: string | undefined): string {
  if (
    typeof value === "string" &&
    value.startsWith("/") &&
    !value.startsWith("//")
  ) {
    return value
  }
  return "/dashboard"
}

export async function GET(req: NextRequest) {
  // Require an authenticated session — an unauthenticated hit on this
  // endpoint should not consume the cookie or redirect anywhere useful.
  const { userId } = await auth()
  if (!userId) {
    return NextResponse.redirect(new URL("/sign-in", req.url))
  }

  const rawRedirect = req.cookies.get("github_connect_redirect")?.value
  const redirectPath = safeRedirectPath(rawRedirect)

  const response = NextResponse.redirect(new URL(redirectPath, req.url))
  // Clear the one-use cookie
  response.cookies.set("github_connect_redirect", "", { maxAge: 0, path: "/" })
  return response
}
