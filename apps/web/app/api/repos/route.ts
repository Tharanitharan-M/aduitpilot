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
//
// Sprint 3.5.10 / 4.20 — `full_name` regex tightened to GitHub's actual
// owner/repo grammar:
//   - owner:  1–39 alphanumerics, allowing single hyphens (no leading or
//             trailing, no consecutive — matches GitHub's username rules)
//   - repo:   1–100 chars, alphanumerics, underscore, dot, hyphen
// Length cap of 200 bytes mirrors the backend Pydantic ScopedRepoSelection
// constraint (apps/api/routes/connectors.py). A repo with a `..` or `/`
// path-traversal byte must NOT pass — it would later be interpolated into
// `github://owner/repo` URIs and into REST URL paths.
const GITHUB_FULL_NAME_RE =
  /^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}\/[a-zA-Z0-9._-]{1,100}$/

const GitHubRepoSchema = z.object({
  id: z.number().int().positive(),
  full_name: z
    .string()
    .min(3)
    .max(200)
    .regex(GITHUB_FULL_NAME_RE, {
      message: "Invalid GitHub full_name (expected 'owner/repo')",
    }),
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

// Sprint 3.5.10 / 4.20 — structured logger.
//
// `console.log` is fine for dev, but in prod the Vercel/Cloud Run log
// drain treats every line as opaque text. A small JSON-line helper makes
// every event grep-able and pipes cleanly into the same PostHog +
// Langfuse log shape the backend uses. Keep it inline (no separate
// module) because the route runs in the Next.js Edge / Node runtime and
// importing a logger adds startup cost for a 5-line emitter.
//
// Output shape:
//   {"ts":"2026-...","level":"info","route":"/api/repos","event":"github.repos.fetched","status":200,"count":42,"user_id":"user_..."}
//
// `user_id` is truncated to the first 8 chars when present so the log
// line is searchable but not directly usable for cross-tenant pivoting.
type LogLevel = "info" | "warn" | "error"
function logEvent(
  level: LogLevel,
  event: string,
  fields: Record<string, unknown> = {},
): void {
  const line = JSON.stringify({
    ts: new Date().toISOString(),
    level,
    route: "/api/repos",
    event,
    ...fields,
  })
  if (level === "error") {
    // eslint-disable-next-line no-console
    console.error(line)
  } else if (level === "warn") {
    // eslint-disable-next-line no-console
    console.warn(line)
  } else {
    // eslint-disable-next-line no-console
    console.log(line)
  }
}

function _redactedUserId(userId: string | null | undefined): string | undefined {
  if (!userId) return undefined
  return `${userId.slice(0, 8)}…`
}

export async function GET() {
  const t0 = Date.now()
  const { userId } = await auth()
  if (!userId) {
    logEvent("warn", "auth.missing")
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
    logEvent("warn", "clerk.oauth_token.fetch_failed", {
      user_id: _redactedUserId(userId),
      error: err instanceof Error ? err.message : "unknown",
    })
  }

  if (!accessToken) {
    logEvent("warn", "github.not_connected", {
      user_id: _redactedUserId(userId),
    })
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
      logEvent("error", "github.repos.fetch_non_2xx", {
        user_id: _redactedUserId(userId),
        status: res.status,
        latency_ms: Date.now() - t0,
      })
      return NextResponse.json(
        { detail: `GitHub API ${res.status}: ${detail.slice(0, 200)}` },
        { status: 502 }
      )
    }
    const parsed = GitHubRepoListSchema.safeParse(await res.json())
    if (!parsed.success) {
      logEvent("error", "github.repos.shape_invalid", {
        user_id: _redactedUserId(userId),
        zod_issues: parsed.error.issues.length,
        latency_ms: Date.now() - t0,
      })
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
    logEvent("error", "github.repos.fetch_threw", {
      user_id: _redactedUserId(userId),
      error: err instanceof Error ? err.message : "unknown",
      latency_ms: Date.now() - t0,
    })
    return NextResponse.json(
      { detail: err instanceof Error ? err.message : "GitHub fetch failed" },
      { status: 502 }
    )
  }

  logEvent("info", "github.repos.fetched", {
    user_id: _redactedUserId(userId),
    count: repos.length,
    latency_ms: Date.now() - t0,
  })
  return NextResponse.json({ repos })
}
