/**
 * Next.js middleware — Clerk auth guard (Sprint 3 chunk 3.5)
 *
 * Public routes: landing (/), sign-in, sign-up, PostHog ingest proxy, health.
 * Everything else (including /dashboard) is protected — unauthenticated
 * requests get a 302 redirect to /sign-in.
 *
 * Refs: PLAN.md chunk 3.5, ADR-0008 (Clerk auth), system-design §6.1,
 *       US-001 (sign-up), US-002 (GitHub OAuth).
 */

import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server"

const isPublicRoute = createRouteMatcher([
  "/",
  "/sign-in(.*)",
  "/sign-up(.*)",
  "/ingest(.*)",
  "/api/health",
])

export default clerkMiddleware(async (auth, request) => {
  if (!isPublicRoute(request)) {
    await auth.protect()
  }
})

export const config = {
  matcher: [
    // Skip Next.js internals and static files
    "/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
    // Always run for API routes
    "/(api|trpc)(.*)",
  ],
}
