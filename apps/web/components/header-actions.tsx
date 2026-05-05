"use client"

/**
 * HeaderActions — client component boundary for interactive header elements.
 *
 * Extracts Clerk's <UserButton> (a Client Component) into its own "use client"
 * leaf so the parent DashboardLayout can remain a Server Component.
 * afterSignOutUrl is configured at the ClerkProvider level (app/layout.tsx);
 * setting it here on UserButton is deprecated in Clerk v6.
 *
 * Refs: PLAN.md chunk 3.4, ADR-0008.
 */

import { UserButton } from "@clerk/nextjs"

export function HeaderActions() {
  return <UserButton />
}
