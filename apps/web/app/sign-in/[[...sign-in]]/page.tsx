/**
 * Sign-in page — Clerk's hosted UI embedded via <SignIn>.
 * Redirects to /dashboard on success.
 * Refs: PLAN.md chunk 3.3, ADR-0008.
 */

import { SignIn } from "@clerk/nextjs"

export default function SignInPage() {
  return (
    <main className="flex min-h-screen items-center justify-center">
      <SignIn
        appearance={{
          elements: {
            rootBox: "mx-auto",
          },
        }}
        redirectUrl="/dashboard"
        signUpUrl="/sign-up"
      />
    </main>
  )
}
