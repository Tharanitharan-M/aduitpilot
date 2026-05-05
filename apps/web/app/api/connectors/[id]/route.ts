/**
 * DELETE /api/connectors/:id
 *
 * Revokes a connector (currently only GitHub OAuth) by deleting the external
 * account from Clerk. Read-only OAuth grant is fully removed — verified in
 * GitHub Settings > Applications.
 *
 * Auth: requires a valid Clerk session. The :id is the Clerk external-account
 * id returned by GET /api/me — never the GitHub installation id.
 *
 * Input validation: the id path segment is validated against the Clerk
 * external-account id format (ext_ prefix, alphanumeric) via Zod before any
 * Clerk API call is made. Rejects crafted ids early — prevents IDOR probing
 * via format confusion.
 *
 * Refs: PLAN.md chunk 3.8, ADR-0004 (read-only), ADR-0008 (Clerk), US-004.
 */

import { auth, clerkClient } from "@clerk/nextjs/server"
import { NextRequest, NextResponse } from "next/server"
import { z } from "zod"

// Clerk external account ids follow the pattern eac_<alphanumeric>
// (verified against the live Clerk API 2026-05-04 — eac_3DHw1k0oo4KgdqT7IdoEcr5mbZZ).
const ExternalAccountIdSchema = z
  .string()
  .regex(/^eac_[a-zA-Z0-9]+$/, "Invalid connector id format")
  .max(64)

export async function DELETE(
  _req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { userId } = await auth()
  if (!userId) {
    return NextResponse.json({ detail: "Unauthorized" }, { status: 401 })
  }

  const { id: rawId } = await params

  // Validate id format before touching Clerk
  const parsed = ExternalAccountIdSchema.safeParse(rawId)
  if (!parsed.success) {
    return NextResponse.json(
      { detail: parsed.error.errors[0]?.message ?? "Invalid id" },
      { status: 422 }
    )
  }
  const id = parsed.data

  // Verify the external account belongs to the authenticated user (IDOR prevention)
  const clerk = await clerkClient()
  const user = await clerk.users.getUser(userId)
  const owns = user.externalAccounts.some((a) => a.id === id)
  if (!owns) {
    return NextResponse.json({ detail: "Not found" }, { status: 404 })
  }

  try {
    await clerk.users.deleteUserExternalAccount({ userId, externalAccountId: id })
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : "Failed to disconnect"
    return NextResponse.json({ detail: message }, { status: 500 })
  }

  return new NextResponse(null, { status: 204 })
}
