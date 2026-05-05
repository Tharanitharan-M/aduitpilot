/**
 * GET /api/me
 *
 * Returns the current user's connector status and repo list. Delegates to
 * lib/me.ts which tries FastAPI first then falls back to the Clerk Backend
 * SDK so the response is consistent with the dashboard Server Component.
 *
 * Refs: PLAN.md chunk 3.7, ADR-0008, US-002, US-005.
 */

import { NextResponse } from "next/server"
import { getMeData } from "@/lib/me"

export async function GET() {
  const data = await getMeData()
  if (!data) {
    return NextResponse.json({ detail: "Unauthorized" }, { status: 401 })
  }
  return NextResponse.json(data)
}
