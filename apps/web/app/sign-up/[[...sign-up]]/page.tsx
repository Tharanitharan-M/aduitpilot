/**
 * Sign-up page — Clerk's hosted UI embedded via <SignUp>.
 * After sign-up, Clerk redirects to /dashboard.
 * Refs: PLAN.md chunk 3.2, ADR-0008, US-001.
 */

import { SignUp } from "@clerk/nextjs"

export default function SignUpPage() {
  return (
    <main className="flex min-h-screen items-center justify-center">
      <SignUp
        appearance={{
          elements: {
            rootBox: "mx-auto",
          },
        }}
        redirectUrl="/dashboard"
        signInUrl="/sign-in"
      />
    </main>
  )
}
