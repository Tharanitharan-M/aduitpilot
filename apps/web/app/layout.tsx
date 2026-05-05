/**
 * Root layout — ClerkProvider + PostHog provider wrapper.
 *
 * ClerkProvider must wrap the entire tree so useAuth() / useUser() work in
 * any Server or Client Component. PostHogProvider is a Client Component that
 * initialises posthog-js and identifies the signed-in user on auth events.
 *
 * Refs: PLAN.md chunks 3.1, 3.2, 3.10; ADR-0008 (Clerk), ADR-0009 (PostHog).
 */

import type { Metadata } from "next"
import { Inter, Geist } from "next/font/google"
import { ClerkProvider } from "@clerk/nextjs"
import "./globals.css"
import { PostHogProvider } from "@/components/posthog-provider"
import { cn } from "@/lib/utils";

const geist = Geist({subsets:['latin'],variable:'--font-sans'});

const inter = Inter({ subsets: ["latin"] })

export const metadata: Metadata = {
  title: "AuditPilot — SOC 2 Readiness Reference Architecture",
  description:
    "Open-source multi-agent SOC 2 readiness reference architecture. Three agents, five MCP servers, read-only by design.",
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <ClerkProvider
      afterSignOutUrl="/"
      signInUrl="/sign-in"
      signUpUrl="/sign-up"
    >
      <html lang="en" className={cn("font-sans", geist.variable)}>
        <body className={inter.className}>
          <PostHogProvider>{children}</PostHogProvider>
        </body>
      </html>
    </ClerkProvider>
  )
}
