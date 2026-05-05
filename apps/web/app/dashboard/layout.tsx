/**
 * Dashboard layout — app shell with header + logout button.
 * This route is protected by middleware.ts (chunk 3.5); if somehow reached
 * without auth, Clerk's <UserButton> will render nothing gracefully.
 * Refs: PLAN.md chunks 3.4, 3.5, US-001.
 */

import Link from "next/link"
import { HeaderActions } from "@/components/header-actions"

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <div className="min-h-screen bg-background">
      {/* App shell header */}
      <header className="sticky top-0 z-50 border-b bg-background/95 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-7xl items-center justify-between px-4">
          <Link href="/dashboard" className="font-semibold">
            AuditPilot
          </Link>

          {/* HeaderActions is a "use client" leaf — keeps this layout a
              Server Component (no direct Client Component imports here). */}
          <HeaderActions />
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-4 py-8">{children}</main>
    </div>
  )
}
