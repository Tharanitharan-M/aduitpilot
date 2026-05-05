/**
 * Next.js client instrumentation file (Next.js 15 convention).
 * Runs once in the browser before the app hydrates.
 *
 * PostHog is also initialised inside PostHogProvider (components/posthog-provider.tsx)
 * for React-aware event capture. This file is the fallback entry point that
 * ensures posthog-js is available even on the very first page paint before
 * any React component mounts.
 *
 * Refs: PLAN.md chunk 3.10, ADR-0009.
 */

import posthog from "posthog-js"

export function register() {
  const key = process.env.NEXT_PUBLIC_POSTHOG_KEY
  if (!key) return

  posthog.init(key, {
    api_host: "/ingest",
    ui_host: "https://us.posthog.com",
    defaults: "2026-01-30",
    capture_exceptions: true,
    debug: process.env.NODE_ENV === "development",
  })
}
