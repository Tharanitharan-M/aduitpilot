/**
 * GET /api/auth/github-connect
 *
 * @deprecated Since Clerk v6, `clerkClient().users` does not expose a
 * server-side `createExternalAccount` method — OAuth initiation is a
 * FAPI (browser) operation. The connect flow now runs entirely client-side
 * via `user.createExternalAccount()` in ConnectorCard (chunk 3.6).
 *
 * This route is kept as a safe redirect fallback in case any bookmarked or
 * cached URL still hits this path.
 *
 * Refs: PLAN.md chunk 3.6, ADR-0008.
 */

import { auth } from "@clerk/nextjs/server"
import { NextRequest, NextResponse } from "next/server"

export async function GET(req: NextRequest) {
  const { userId } = await auth()
  if (!userId) {
    return NextResponse.redirect(new URL("/sign-in", req.url))
  }
  return NextResponse.redirect(new URL("/dashboard", req.url))
}
