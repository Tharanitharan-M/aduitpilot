"use client"

/**
 * ConnectorCard — shows GitHub connection status + connect/disconnect CTA.
 *
 * States (US-005 + US-002 picker integration, ADR-0015):
 *   connected (scope > 0)  — green "Connected · N repos" badge, "Edit scope"
 *                            link, Disconnect enabled, Connect disabled
 *   connected (scope = 0)  — green "Connected" badge with prominent
 *                            "Configure scope" CTA pointing at the picker
 *   error                  — yellow badge, both buttons enabled
 *   not_connected          — gray badge, Connect enabled, Disconnect disabled
 *
 * Debug mode: when NEXT_PUBLIC_CONNECTOR_DEBUG is "true", BOTH buttons stay
 * enabled regardless of state, and a raw payload panel renders the connector
 * object the dashboard received from /api/me.
 *
 * Refs: PLAN.md chunks 3.6, 3.8, 3.9; Sprint 3.5 chunk 3.5.4; ADR-0015;
 * US-002, US-004, US-005.
 */

import { useState } from "react"
import Link from "next/link"
import { useRouter } from "next/navigation"
import { useAuth, useUser } from "@clerk/nextjs"
import { Github } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"

export type ConnectorStatus = "connected" | "error" | "not_connected"

export interface Connector {
  id: string
  provider: "github"
  status: ConnectorStatus
  last_used_at: string | null
  error_message: string | null
  scoped_repo_count?: number
}

interface ConnectorCardProps {
  connector: Connector | null
  /** Debug-mode override — both buttons enabled, raw payload visible. */
  debug?: boolean
}

function statusBadge(status: ConnectorStatus, scopedCount: number) {
  if (status === "connected") {
    const label = scopedCount > 0 ? `Connected · ${scopedCount} repos` : "Connected"
    return <Badge variant="success">{label}</Badge>
  }
  if (status === "error")
    return <Badge variant="warning">Error — re-authentication needed</Badge>
  return <Badge variant="outline">Not connected</Badge>
}

export function ConnectorCard({ connector, debug = false }: ConnectorCardProps) {
  const router = useRouter()
  const { getToken } = useAuth()
  const { user } = useUser()
  const [loading, setLoading] = useState<"connect" | "disconnect" | null>(null)
  const [error, setError] = useState<string | null>(null)

  const status: ConnectorStatus = connector?.status ?? "not_connected"
  const isConnected = status === "connected"
  const scopedCount = connector?.scoped_repo_count ?? 0
  const needsScope = isConnected && scopedCount === 0
  const scopeHref = connector?.id
    ? `/dashboard/connectors/${connector.id}/scope`
    : null
  // Disconnect needs an id from the API (or from useUser fallback).
  // Clerk's frontend SDK exposes provider as "github" (no oauth_ prefix);
  // the backend SDK returns "oauth_github". useUser() uses the frontend shape.
  const githubAccount = user?.externalAccounts?.find(
    (a) => a.provider === "github"
  )
  const disconnectId = connector?.id ?? githubAccount?.id ?? null

  async function handleConnect() {
    if (!user) return
    setLoading("connect")
    setError(null)
    try {
      const res = await user.createExternalAccount({
        strategy: "oauth_github",
        redirectUrl: "/dashboard",
        additionalScopes: ["public_repo", "read:org"],
      })
      const url = res.verification?.externalVerificationRedirectURL
      if (url) window.location.href = url.toString()
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Connect failed")
      setLoading(null)
    }
  }

  async function handleDisconnect() {
    if (!disconnectId) {
      setError("No GitHub external account id available to disconnect.")
      return
    }
    setLoading("disconnect")
    setError(null)
    try {
      const token = await getToken()
      const res = await fetch(`/api/connectors/${disconnectId}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token ?? ""}` },
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body?.detail ?? `HTTP ${res.status}`)
      }
      router.refresh()
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Disconnect failed")
    } finally {
      setLoading(null)
    }
  }

  // Production rules:
  //   - connect disabled when already connected
  //   - disconnect disabled when no external account exists at all
  // Debug override: both enabled.
  const connectDisabled = debug ? false : isConnected || loading !== null
  const disconnectDisabled = debug
    ? !disconnectId || loading !== null
    : !disconnectId || !isConnected || loading !== null

  return (
    <Card data-testid="connector-card" data-status={status}>
      <CardHeader className="flex flex-row items-center gap-3">
        <Github className="size-5" aria-hidden />
        <div>
          <CardTitle>GitHub</CardTitle>
          <CardDescription>Read-only · public_repo, read:org</CardDescription>
        </div>
      </CardHeader>

      <CardContent className="space-y-2">
        {statusBadge(status, scopedCount)}

        {needsScope && scopeHref && (
          <div
            data-testid="configure-scope-banner"
            className="rounded border border-yellow-300 bg-yellow-50 p-2 text-xs text-yellow-900 dark:border-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-200"
          >
            Pick the repos AuditPilot is allowed to scan.{" "}
            <Link
              href={scopeHref}
              className="font-medium underline underline-offset-2"
            >
              Configure scope →
            </Link>
          </div>
        )}

        {isConnected && scopedCount > 0 && scopeHref && (
          <Link
            href={scopeHref}
            data-testid="edit-scope-link"
            className="inline-block text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground"
          >
            Edit scope
          </Link>
        )}

        {connector?.last_used_at && (
          <p className="text-xs text-muted-foreground">
            Last used{" "}
            {new Date(connector.last_used_at).toLocaleString(undefined, {
              dateStyle: "medium",
              timeStyle: "short",
            })}
          </p>
        )}

        {status === "error" && connector?.error_message && (
          <p className="text-xs text-yellow-700">{connector.error_message}</p>
        )}

        {error && <p className="text-xs text-destructive">{error}</p>}

        {debug && (
          <details className="mt-3 rounded border border-dashed border-muted-foreground/40 p-2 text-xs">
            <summary className="cursor-pointer text-muted-foreground">
              debug: raw connector payload
            </summary>
            <pre className="mt-2 overflow-auto whitespace-pre-wrap break-all text-[11px] leading-tight">
              {JSON.stringify(
                {
                  connector,
                  clerk_external_account_id: githubAccount?.id ?? null,
                  clerk_external_account_provider:
                    githubAccount?.provider ?? null,
                  clerk_verification_status:
                    githubAccount?.verification?.status ?? null,
                  resolved_status: status,
                  resolved_disconnect_id: disconnectId,
                },
                null,
                2
              )}
            </pre>
          </details>
        )}
      </CardContent>

      <CardFooter className="gap-2">
        <Button
          size="sm"
          onClick={handleConnect}
          disabled={connectDisabled}
        >
          {loading === "connect"
            ? "Connecting…"
            : status === "error"
            ? "Re-connect GitHub"
            : "Connect GitHub"}
        </Button>

        <Button
          variant="outline"
          size="sm"
          onClick={handleDisconnect}
          disabled={disconnectDisabled}
          aria-label="Disconnect GitHub"
        >
          {loading === "disconnect" ? "Disconnecting…" : "Disconnect"}
        </Button>
      </CardFooter>
    </Card>
  )
}
