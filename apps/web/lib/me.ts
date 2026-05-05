/**
 * Shared connector/repo data fetcher used by both the dashboard Server
 * Component and the /api/me Next.js route.
 *
 * Strategy:
 *   1. Try the FastAPI proxy at $API_URL/api/me with the Clerk session JWT.
 *   2. On any non-OK response or network error, fall back to the Clerk
 *      Backend SDK to derive connector status from external_accounts.
 *
 * The Clerk fallback exists so the UI is not silently broken when FastAPI
 * is offline, mis-configured, or returns 401 (e.g. missing CLERK_JWKS_URL).
 *
 * Refs: PLAN.md chunks 3.7, 3.9; ADR-0008.
 */

import { auth, clerkClient } from "@clerk/nextjs/server"

export interface Connector {
  id: string
  provider: "github"
  status: "connected" | "error" | "not_connected"
  last_used_at: string | null
  error_message: string | null
  /**
   * Sprint 3.5: count of repos the user has scoped on this connector.
   * 0 means the connector is verified but the user has not yet picked
   * any repos. The dashboard renders a "Configure scope" CTA in that
   * state (ADR-0015 — repo selection at scan time).
   */
  scoped_repo_count?: number
}

export interface Repo {
  id: string
  full_name: string
  private: boolean
}

export interface MeData {
  connectors: Connector[]
  repos: Repo[]
}

async function tryFastApi(token: string): Promise<MeData | null> {
  const apiBase = process.env.API_URL ?? "http://localhost:8000"
  try {
    const res = await fetch(`${apiBase}/api/me`, {
      headers: { Authorization: `Bearer ${token}` },
      signal: AbortSignal.timeout(3000),
      next: { revalidate: 30 },
    })
    if (!res.ok) return null
    return (await res.json()) as MeData
  } catch {
    return null
  }
}

async function clerkFallback(userId: string): Promise<MeData> {
  const clerk = await clerkClient()
  const user = await clerk.users.getUser(userId)

  // Clerk Backend API returns provider="oauth_github" for GitHub external
  // accounts (verified via curl 2026-05-04).
  const githubAccount = user.externalAccounts.find(
    (a) => a.provider === "oauth_github" || a.provider === "github"
  )

  const connectors: Connector[] = githubAccount
    ? [
        {
          id: githubAccount.id,
          provider: "github",
          status:
            githubAccount.verification?.status === "verified"
              ? "connected"
              : "error",
          last_used_at: null,
          error_message:
            githubAccount.verification?.status !== "verified"
              ? "Re-authentication required"
              : null,
          // Clerk fallback path can't reach the connector_scoped_repos
          // table (no Clerk endpoint for it). Default to 0 — the
          // dashboard then renders "Configure scope" CTA, which still
          // works since the picker page reads its own scope via the
          // FastAPI route.
          scoped_repo_count: 0,
        },
      ]
    : []

  return { connectors, repos: [] }
}

export async function getMeData(): Promise<MeData | null> {
  const { userId, getToken } = await auth()
  if (!userId) return null

  const token = await getToken()
  if (token) {
    const upstream = await tryFastApi(token)
    if (upstream) return upstream
  }

  return clerkFallback(userId)
}
