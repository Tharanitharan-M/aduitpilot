/**
 * GET /api/repos
 *
 * Returns the authenticated user's GitHub repos as a typed list. Used by
 * the repo-picker page (Sprint 3.5 chunk 3.5.2). Reads the user's GitHub
 * OAuth access token from Clerk via `getUserOauthAccessToken` (Backend
 * SDK — secret key only used server-side) and calls
 * `GET https://api.github.com/user/repos`.
 *
 * Read-only by design (ADR-0004): the call is bounded by the
 * `public_repo` + `read:org` scopes the user authorized at connect time.
 *
 * Refs: PLAN.md Sprint 3.5 chunk 3.5.2; ADR-0008 (Clerk), ADR-0015
 * (repo-selection at scan time).
 */

import { auth, clerkClient } from "@clerk/nextjs/server"
import { NextResponse } from "next/server"
import { z } from "zod"

// Validate the shape we read from GitHub. typescript-reviewer CRITICAL-1:
// `res.json() as GitHubRepo[]` would happily accept a non-array error
// payload (e.g. 200 OK with `{ message, documentation_url }` from GitHub
// API versioning hiccups) and crash downstream on `.map`. Zod gives us a
// runtime contract.
const GitHubRepoSchema = z.object({
  id: z.number(),
  full_name: z.string(),
  private: z.boolean(),
})
const GitHubRepoListSchema = z.array(GitHubRepoSchema)

// Validate the Clerk Backend SDK return for getUserOauthAccessToken.
// typescript-reviewer CRITICAL-2: `as { data?: ... }` would silently
// break on any future Clerk SDK shape change.
const ClerkOauthTokenSchema = z.object({ token: z.string().min(1) })
const ClerkOauthTokenListSchema = z.array(ClerkOauthTokenSchema)
const ClerkOauthTokenPagedSchema = z.object({
  data: ClerkOauthTokenListSchema,
})

export interface RepoListItem {
  provider_repo_id: string
  full_name: string
  private: boolean
}

export async function GET() {
  const { userId } = await auth()
  if (!userId) {
    return NextResponse.json({ detail: "Unauthorized" }, { status: 401 })
  }

  const clerk = await clerkClient()

  // The Clerk Backend SDK paginates oauth tokens; for "github" the user
  // can only have one token at a time so the first page is the answer.
  let accessToken: string | null = null
  try {
    const tokens = await clerk.users.getUserOauthAccessToken(userId, "github")
    // Clerk v6 wraps results as `{ data: [...], totalCount }`; older
    // versions returned a bare array. Validate both shapes via Zod.
    const paged = ClerkOauthTokenPagedSchema.safeParse(tokens)
    const bare = ClerkOauthTokenListSchema.safeParse(tokens)
    const items = paged.success ? paged.data.data : bare.success ? bare.data : []
    if (items.length > 0) {
      accessToken = items[0].token
    }
  } catch (err) {
    console.warn("clerk.getUserOauthAccessToken.failed", err)
  }

  if (!accessToken) {
    return NextResponse.json(
      { detail: "GitHub not connected or token unavailable" },
      { status: 400 }
    )
  }

  // Fetch the user's first page of repos. 100 per page covers the common
  // case; orgs with > 100 repos use the cmdk fallback (deferred to
  // Sprint 4 if data shows it's needed).
  let repos: RepoListItem[] = []
  try {
    const res = await fetch(
      "https://api.github.com/user/repos?per_page=100&sort=full_name",
      {
        headers: {
          Authorization: `Bearer ${accessToken}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        cache: "no-store",
      }
    )
    if (!res.ok) {
      const detail = await res.text().catch(() => "")
      return NextResponse.json(
        { detail: `GitHub API ${res.status}: ${detail.slice(0, 200)}` },
        { status: 502 }
      )
    }
    const parsed = GitHubRepoListSchema.safeParse(await res.json())
    if (!parsed.success) {
      return NextResponse.json(
        { detail: "Unexpected GitHub response shape" },
        { status: 502 }
      )
    }
    repos = parsed.data.map((r) => ({
      provider_repo_id: String(r.id),
      full_name: r.full_name,
      private: r.private,
    }))
  } catch (err) {
    return NextResponse.json(
      { detail: err instanceof Error ? err.message : "GitHub fetch failed" },
      { status: 502 }
    )
  }

  return NextResponse.json({ repos })
}
