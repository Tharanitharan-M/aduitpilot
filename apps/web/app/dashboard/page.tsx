/**
 * Dashboard page — connector grid + repo list.
 *
 * Server Component. Uses the shared getMeData() helper which:
 *   - tries FastAPI /api/me first (with Clerk session JWT), and
 *   - falls back to the Clerk Backend SDK if FastAPI is offline / 401.
 *
 * The Clerk fallback prevents the UI from silently showing "Not connected"
 * when only the API tier is mis-configured (e.g. missing CLERK_JWKS_URL).
 *
 * Refs: PLAN.md chunks 3.6, 3.7, 3.9; US-002, US-004, US-005.
 */

import { ConnectorCard } from "@/components/connector-card"
import { RepoList } from "@/components/repo-list"
import { getMeData } from "@/lib/me"

export default async function DashboardPage() {
  const me = await getMeData()

  const githubConnector =
    me?.connectors?.find((c) => c.provider === "github") ?? null

  const repos = me?.repos ?? []

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold">Dashboard</h1>
        <p className="mt-1 text-muted-foreground">
          Connect your GitHub organization to start a readiness scan.
        </p>
      </div>

      {/* Connectors section — chunk 3.6, 3.9. Debug flag flips both Connect
          and Disconnect to always-enabled and renders a raw-payload panel. */}
      <section aria-label="Connectors">
        <h2 className="mb-4 text-lg font-semibold">Connectors</h2>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <ConnectorCard
            connector={githubConnector}
            debug={process.env.NEXT_PUBLIC_CONNECTOR_DEBUG === "true"}
          />
        </div>
      </section>

      {/* Repos section — chunk 3.7 */}
      {repos.length > 0 && (
        <section aria-label="Connected repositories">
          <h2 className="mb-4 text-lg font-semibold">Connected repositories</h2>
          <RepoList repos={repos} />
        </section>
      )}
    </div>
  )
}
