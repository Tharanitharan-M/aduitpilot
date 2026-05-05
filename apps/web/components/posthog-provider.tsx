"use client"

/**
 * PostHogProvider — initialises posthog-js and identifies the signed-in user.
 *
 * - Wraps the whole app tree (mounted in app/layout.tsx).
 * - Calls posthog.identify() when Clerk's session loads so every event is
 *   keyed to the Clerk user_id (distinct_id). US-001 funnel tracking.
 * - Uses the /ingest reverse-proxy rewrite (next.config.ts) to avoid
 *   adblocker blocking.
 *
 * Refs: PLAN.md chunk 3.10, ADR-0009, US-001.
 */

import posthog from "posthog-js"
import { PostHogProvider as PHProvider, usePostHog } from "posthog-js/react"
import { useEffect } from "react"
import { useUser } from "@clerk/nextjs"

function PostHogAuthSync() {
  const { user, isLoaded } = useUser()
  const client = usePostHog()

  useEffect(() => {
    if (!isLoaded) return
    if (user) {
      client.identify(user.id, {
        email: user.primaryEmailAddress?.emailAddress,
        name: user.fullName ?? undefined,
      })
    } else {
      client.reset()
    }
  }, [user, isLoaded, client])

  return null
}

export function PostHogProvider({ children }: { children: React.ReactNode }) {
  useEffect(() => {
    const key = process.env.NEXT_PUBLIC_POSTHOG_KEY
    if (!key) return
    posthog.init(key, {
      api_host: "/ingest",
      ui_host: "https://us.posthog.com",
      defaults: "2026-01-30",
      capture_exceptions: true,
      debug: process.env.NODE_ENV === "development",
    })
  }, [])

  return (
    <PHProvider client={posthog}>
      <PostHogAuthSync />
      {children}
    </PHProvider>
  )
}
