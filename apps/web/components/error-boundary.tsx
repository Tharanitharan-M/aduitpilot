"use client"

/**
 * IslandErrorBoundary — class-based error boundary for client islands.
 *
 * Sprint 4 chunk 4.19. Each interactive client island on the dashboard
 * (ScanWorkspace, ConnectorCard, RepoList, PendingActions) is wrapped in
 * this boundary so a runtime crash in one island shows an inline fallback
 * instead of taking down the whole page.
 *
 * Why a custom boundary in addition to Next.js's ``app/.../error.tsx``?
 *
 *   ``error.tsx`` files catch render errors at the route boundary — they
 *   replace the entire route tree below the segment. That is the right
 *   behaviour for unrecoverable failures, but it is too coarse for the
 *   dashboard: if the readiness chat island throws because of a malformed
 *   SSE chunk, we don't want the connector grid + pending actions to also
 *   disappear. ``IslandErrorBoundary`` keeps siblings rendered while the
 *   broken island shows its inline fallback.
 *
 * Behaviour:
 *   - Catches errors thrown during render or in lifecycle methods of
 *     descendants.
 *   - Renders ``fallback`` when an error is caught, otherwise ``children``.
 *   - Logs ``console.error("island_error_boundary[<name>]", error)`` so
 *     PostHog session replay + browser devtools surface the failure.
 *
 * Refs: PLAN.md Sprint 4 chunk 4.19; ADR-0014 (PostHog observability).
 */

import { Component, type ErrorInfo, type ReactNode } from "react"

interface IslandErrorBoundaryProps {
  /**
   * Stable name for the island. Used in the console log key and the
   * default fallback heading. ``"ScanWorkspace"`` / ``"PendingActions"``.
   */
  name: string
  /**
   * Optional custom fallback. When omitted, a small inline panel is
   * rendered with the island name and the error message.
   */
  fallback?: ReactNode
  children: ReactNode
}

interface IslandErrorBoundaryState {
  error: Error | null
}

export class IslandErrorBoundary extends Component<
  IslandErrorBoundaryProps,
  IslandErrorBoundaryState
> {
  constructor(props: IslandErrorBoundaryProps) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error: Error): IslandErrorBoundaryState {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Console log keeps the failure visible in browser devtools and
    // PostHog session replay. Avoids importing the PostHog client here
    // so the boundary stays a leaf-level component with no side-effect
    // dependencies — important since it must keep rendering even if
    // the analytics SDK itself crashes.
    // eslint-disable-next-line no-console
    console.error(
      `island_error_boundary[${this.props.name}]`,
      error,
      info.componentStack,
    )
  }

  render(): ReactNode {
    if (this.state.error) {
      if (this.props.fallback) {
        return this.props.fallback
      }
      return (
        <div
          role="alert"
          aria-live="polite"
          data-testid={`island-error-${this.props.name}`}
          className="rounded-md border border-destructive/40 bg-destructive/5 p-4 text-sm"
        >
          <p className="font-medium text-destructive">
            {this.props.name} could not load
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            The rest of the dashboard is still usable. Refresh to retry, or
            check the browser console for details.
          </p>
        </div>
      )
    }
    return this.props.children
  }
}
