/**
 * Next.js proxy: PATCH /api/connectors/:id/scoped-repos
 *
 * Forwards the picker's "save scope" submission to the FastAPI
 * `PATCH /api/connectors/{id}/scoped-repos` endpoint (Sprint 3.5.3).
 * Keeping the proxy server-side means the FastAPI origin (API_URL) is
 * never exposed to the browser — same SSRF/origin-leak posture as
 * /api/me.
 *
 * Refs: PLAN.md Sprint 3.5 chunks 3.5.2 + 3.5.3; ADR-0008, ADR-0015.
 */

import { auth } from "@clerk/nextjs/server"
import { NextRequest, NextResponse } from "next/server"
import { z } from "zod"

const ExternalAccountIdSchema = z
  .string()
  .regex(/^eac_[a-zA-Z0-9]+$/, "Invalid connector id format")
  .max(64)

export async function GET(
  _req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { userId, getToken } = await auth()
  if (!userId) {
    return NextResponse.json({ detail: "Unauthorized" }, { status: 401 })
  }
  const { id: rawId } = await params
  const idParse = ExternalAccountIdSchema.safeParse(rawId)
  if (!idParse.success) {
    return NextResponse.json(
      { detail: idParse.error.errors[0]?.message ?? "Invalid id" },
      { status: 422 }
    )
  }
  const apiBase = process.env.API_URL ?? "http://localhost:8000"
  const token = await getToken()
  if (!token) {
    return NextResponse.json({ detail: "Session expired" }, { status: 401 })
  }
  try {
    const res = await fetch(
      `${apiBase}/api/connectors/${idParse.data}/scoped-repos`,
      {
        headers: { Authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(5000),
      }
    )
    const text = await res.text()
    return new NextResponse(text || null, {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    })
  } catch (err) {
    return NextResponse.json(
      { detail: err instanceof Error ? err.message : "Upstream error" },
      { status: 502 }
    )
  }
}

const RepoSelectionSchema = z.object({
  provider_repo_id: z.string().min(1).max(64),
  full_name: z.string().min(3).max(200),
  private: z.boolean(),
})

const PatchBodySchema = z
  .object({
    repos: z.array(RepoSelectionSchema).max(500),
  })
  .strict()

export async function PATCH(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { userId, getToken } = await auth()
  if (!userId) {
    return NextResponse.json({ detail: "Unauthorized" }, { status: 401 })
  }

  const { id: rawId } = await params
  const idParse = ExternalAccountIdSchema.safeParse(rawId)
  if (!idParse.success) {
    return NextResponse.json(
      { detail: idParse.error.errors[0]?.message ?? "Invalid id" },
      { status: 422 }
    )
  }
  const connectorId = idParse.data

  const json = await req.json().catch(() => null)
  const bodyParse = PatchBodySchema.safeParse(json)
  if (!bodyParse.success) {
    return NextResponse.json(
      { detail: bodyParse.error.errors[0]?.message ?? "Invalid body" },
      { status: 422 }
    )
  }

  const apiBase = process.env.API_URL ?? "http://localhost:8000"
  const token = await getToken()
  if (!token) {
    return NextResponse.json({ detail: "Session expired" }, { status: 401 })
  }
  try {
    const res = await fetch(
      `${apiBase}/api/connectors/${connectorId}/scoped-repos`,
      {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify(bodyParse.data),
        // 15s — a 500-row PATCH against a cold Neon connection has been
        // observed to take 7-9s under burst load. 5s was rejected as too
        // tight (typescript-reviewer HIGH-2).
        signal: AbortSignal.timeout(15000),
      }
    )
    const text = await res.text()
    return new NextResponse(text || null, {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    })
  } catch (err) {
    return NextResponse.json(
      { detail: err instanceof Error ? err.message : "Upstream error" },
      { status: 502 }
    )
  }
}
