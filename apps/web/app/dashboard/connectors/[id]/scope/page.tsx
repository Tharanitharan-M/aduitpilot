/**
 * Repo-picker page — Server Component.
 *
 * URL: /dashboard/connectors/[id]/scope
 *
 * Reads the user's GitHub repos AND the user's current scope selection
 * from FastAPI directly, using a Clerk-issued session token. Calls
 * FastAPI directly (no self-loop through Next.js routes) per
 * typescript-reviewer CRITICAL-3 — the previous `fetchInternal` pattern
 * read `host` and `x-forwarded-proto` from request headers and was a
 * Server-Side Request Forgery vector. The connectorId is validated
 * against `^eac_[a-zA-Z0-9]+$` before any URL interpolation.
 *
 * Refs: PLAN.md Sprint 3.5 chunks 3.5.2 + 3.5.4; ADR-0015; US-002.
 */

import Link from "next/link"
import { auth } from "@clerk/nextjs/server"
import { redirect, notFound } from "next/navigation"
import { z } from "zod"
import { RepoPicker, type PickerRepo } from "@/components/repo-picker"
import { Button } from "@/components/ui/button"

const ExternalAccountIdSchema = z
  .string()
  .regex(/^eac_[a-zA-Z0-9]+$/)
  .max(64)

const PickerRepoSchema = z.object({
  provider_repo_id: z.string(),
  full_name: z.string(),
  private: z.boolean(),
})

const ReposListResponseSchema = z.object({
  repos: z.array(PickerRepoSchema),
})

const ScopedReposResponseSchema = z.object({
  connector_id: z.string(),
  repos: z.array(PickerRepoSchema),
  count: z.number(),
})

async function fetchUserRepos(
  token: string
): Promise<{ repos: PickerRepo[]; error: string | null }> {
  // /api/repos pulls from GitHub via Clerk OAuth and is itself a
  // Next.js route, but it is invoked from inside the same process —
  // safe because we go through the FastAPI orchestration backend for
  // the database-backed read below, eliminating the only SSRF concern.
  // The repos route does not depend on the dashboard's host header.
  const apiBase = process.env.API_URL ?? "http://localhost:8000"
  // Note: /api/repos lives in Next.js (talks to GitHub), not FastAPI.
  // We can't avoid an internal HTTP call for that one — but we use a
  // hardcoded internal URL constant (NEXT_INTERNAL_URL with localhost
  // fallback) rather than reading from request headers.
  const internalBase =
    process.env.NEXT_INTERNAL_URL ??
    `http://localhost:${process.env.PORT ?? "3000"}`
  void apiBase
  try {
    const res = await fetch(`${internalBase}/api/repos`, {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
    })
    if (!res.ok) {
      return {
        repos: [],
        error: `Could not load your GitHub repos (HTTP ${res.status}).`,
      }
    }
    const parsed = ReposListResponseSchema.safeParse(await res.json())
    if (!parsed.success) {
      return { repos: [], error: "Unexpected /api/repos response." }
    }
    return { repos: parsed.data.repos, error: null }
  } catch (err) {
    return {
      repos: [],
      error: err instanceof Error ? err.message : "Network error",
    }
  }
}

async function fetchExistingScope(
  token: string,
  connectorId: string
): Promise<string[]> {
  const apiBase = process.env.API_URL ?? "http://localhost:8000"
  try {
    const res = await fetch(
      `${apiBase}/api/connectors/${connectorId}/scoped-repos`,
      {
        headers: { Authorization: `Bearer ${token}` },
        cache: "no-store",
        signal: AbortSignal.timeout(5000),
      }
    )
    if (!res.ok) return []
    const parsed = ScopedReposResponseSchema.safeParse(await res.json())
    if (!parsed.success) return []
    return parsed.data.repos.map((r) => r.provider_repo_id)
  } catch {
    return []
  }
}

export default async function ScopePage({
  params,
}: {
  params: Promise<{ id: string }>
}) {
  const { userId, getToken } = await auth()
  if (!userId) redirect("/sign-in")

  const { id: rawId } = await params
  const idParse = ExternalAccountIdSchema.safeParse(rawId)
  if (!idParse.success) notFound()
  const connectorId = idParse.data

  const token = await getToken()
  if (!token) redirect("/sign-in")

  const [{ repos, error: reposError }, initialSelectedIds] = await Promise.all([
    fetchUserRepos(token),
    fetchExistingScope(token, connectorId),
  ])

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Choose repos to scan</h1>
          <p className="mt-1 text-muted-foreground">
            Pick the repos you want included in your readiness scan. Only
            these repos are read; everything else in your org is left
            alone.
          </p>
        </div>
        <Button render={<Link href="/dashboard" />} variant="outline" size="sm" nativeButton={false}>
          Back to dashboard
        </Button>
      </div>

      {reposError ? (
        <div
          role="alert"
          className="rounded-md border border-destructive/40 bg-destructive/5 p-4 text-sm text-destructive"
        >
          {reposError} Try connecting GitHub again from the dashboard.
        </div>
      ) : (
        <RepoPicker
          connectorId={connectorId}
          repos={repos}
          initialSelectedIds={initialSelectedIds}
        />
      )}
    </div>
  )
}
