/**
 * Landing page — public, no auth required.
 * Shows value prop + "Try the demo" + sign-up CTA.
 * Refs: PLAN.md chunk 3.1, US-033.
 */

import Link from "next/link"
import { SignedIn, SignedOut } from "@clerk/nextjs"
import { Button } from "@/components/ui/button"

export default function LandingPage() {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center px-6 text-center">
      <div className="max-w-2xl space-y-6">
        <h1 className="text-4xl font-bold tracking-tight">
          AuditPilot
        </h1>
        <p className="text-lg text-muted-foreground">
          Open-source multi-agent SOC 2 readiness reference architecture.
          Three agents, five MCP servers, read-only by design.
        </p>

        <div className="flex flex-col items-center gap-3 sm:flex-row sm:justify-center">
          <SignedOut>
            <Button render={<Link href="/sign-up" />} size="lg" nativeButton={false}>
              Get started
            </Button>
            <Button render={<Link href="/sign-in" />} variant="outline" size="lg" nativeButton={false}>
              Sign in
            </Button>
          </SignedOut>

          <SignedIn>
            <Button render={<Link href="/dashboard" />} size="lg" nativeButton={false}>
              Go to dashboard
            </Button>
          </SignedIn>
        </div>

        <p className="text-sm text-muted-foreground">
          Apache 2.0 license &middot; Read-only on the way in &middot; Drafts on the way out
        </p>
      </div>
    </main>
  )
}
