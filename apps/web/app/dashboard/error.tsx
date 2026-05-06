"use client"

/**
 * Dashboard route-level error boundary.
 *
 * Sprint 4 chunk 4.19. Next.js App Router renders this component when
 * a Server Component or layout below ``app/dashboard/`` throws during
 * render. It is the LAST line of defence — individual client islands
 * are wrapped in ``<IslandErrorBoundary>`` first so per-island crashes
 * don't reach this far.
 *
 * Reset behaviour: ``reset()`` re-runs the failed segment without a
 * full page reload. We expose it on a button so the user can recover
 * from a transient render error (e.g. a stale fetch result) without
 * losing their Clerk session.
 *
 * Refs: PLAN.md Sprint 4 chunk 4.19; Next.js App Router error.js docs.
 */

import { Button } from "@/components/ui/button"
import { useEffect } from "react"

interface DashboardErrorProps {
  error: Error & { digest?: string }
  reset: () => void
}

export default function DashboardError({ error, reset }: DashboardErrorProps) {
  useEffect(() => {
    // Surface the failure into the browser console + PostHog session
    // replay. ``error.digest`` is Next.js's server-side error id which
    // operators can correlate against server logs.
    // eslint-disable-next-line no-console
    console.error("dashboard.route_error", {
      message: error.message,
      digest: error.digest,
      stack: error.stack,
    })
  }, [error])

  return (
    <div
      role="alert"
      aria-live="assertive"
      className="space-y-4 rounded-lg border border-destructive/40 bg-destructive/5 p-6"
    >
      <h1 className="text-lg font-semibold text-destructive">
        Dashboard could not render
      </h1>
      <p className="text-sm text-muted-foreground">
        Something went wrong while preparing the dashboard. Your data is
        safe — this is a render-time error.
      </p>
      {error.digest && (
        <p className="font-mono text-xs text-muted-foreground">
          Reference: <span className="select-all">{error.digest}</span>
        </p>
      )}
      <div className="flex gap-2">
        <Button size="sm" onClick={() => reset()}>
          Retry
        </Button>
      </div>
    </div>
  )
}
