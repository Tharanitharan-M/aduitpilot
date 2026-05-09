"use client"

/**
 * DriftClient — Sprint 9 chunk 9.8.
 *
 * Renders the user's drift events as DriftEventCard items.
 * Affordances:
 *   - "Run drift scan now" button (POST /api/drift/run)
 *   - per-card Dismiss (with reason) / Resolve buttons
 *   - filter chips: open / dismissed / resolved
 *   - safe source_link rendering (allowlisted https hosts only)
 *
 * Refs: PLAN.md chunks 9.6, 9.8, 9.9; system-design 13.4.
 */

import { useCallback, useEffect, useRef, useState } from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { EmptyState } from "@/components/empty-state"
import { Activity, ShieldAlert } from "lucide-react"

const SOURCE_LINK_HOSTS: ReadonlyArray<string> = [
  "github.com",
  "www.github.com",
]

function safeSourceHref(value: string | null | undefined): string | null {
  if (!value) return null
  let url: URL
  try {
    url = new URL(value)
  } catch {
    return null
  }
  if (url.protocol !== "https:") return null
  return SOURCE_LINK_HOSTS.includes(url.host.toLowerCase()) ? url.toString() : null
}

type DriftStatus = "open" | "resolved" | "dismissed"
type DriftSeverity = "low" | "medium" | "high"
type DriftEventType = "status_changed" | "config_changed" | "evidence_removed"

interface DriftEvent {
  id: string
  user_id: string
  control_id: string
  event_type: DriftEventType
  what_changed: string
  previous_value: Record<string, unknown>
  current_value: Record<string, unknown>
  suggested_fix: string
  source_link: string | null
  severity: DriftSeverity
  detected_at: string
  status: DriftStatus
  content_hash: string
}

interface DriftStatusOut {
  baselines: number
  last_scan_at: string | null
  events_total: number
  events_open: number
}

function formatRelative(iso: string | null): string {
  if (!iso) return "never"
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return "never"
  const seconds = Math.max(1, Math.round((Date.now() - then) / 1000))
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 48) return `${hours}h ago`
  const days = Math.round(hours / 24)
  return `${days}d ago`
}

const SEVERITY_BADGE: Record<DriftSeverity, "outline" | "secondary" | "destructive"> = {
  low: "outline",
  medium: "secondary",
  high: "destructive",
}

const STATUS_BADGE: Record<DriftStatus, "outline" | "secondary" | "default"> = {
  open: "default",
  resolved: "secondary",
  dismissed: "outline",
}

const EVENT_TYPE_LABEL: Record<DriftEventType, string> = {
  status_changed: "Status changed",
  config_changed: "Configuration changed",
  evidence_removed: "Evidence removed",
}

export function DriftClient() {
  const [loadState, setLoadState] = useState<"loading" | "ready" | "error">("loading")
  const [events, setEvents] = useState<DriftEvent[]>([])
  const [filter, setFilter] = useState<DriftStatus | "all">("open")
  const [running, setRunning] = useState(false)
  const [runMessage, setRunMessage] = useState<string | null>(null)
  const [dismissDraft, setDismissDraft] = useState<Record<string, string>>({})
  const [rowError, setRowError] = useState<Record<string, string | null>>({})
  const [status, setStatus] = useState<DriftStatusOut | null>(null)

  const generationRef = useRef(0)
  const mountedRef = useRef(true)
  const abortRef = useRef<AbortController | null>(null)

  const loadStatus = useCallback(async (signal?: AbortSignal) => {
    try {
      const res = await fetch("/api/drift/status", { cache: "no-store", signal })
      if (!mountedRef.current) return
      if (!res.ok) return
      const body = (await res.json()) as DriftStatusOut
      if (!mountedRef.current) return
      setStatus(body)
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return
      // Soft-fail: heartbeat is informational, never blocks the page.
    }
  }, [])

  const load = useCallback(async (signal?: AbortSignal) => {
    const myGen = ++generationRef.current
    setLoadState("loading")
    try {
      const search = filter === "all" ? "" : `?status=${filter}`
      const [eventsRes] = await Promise.all([
        fetch(`/api/drift/events${search}`, { cache: "no-store", signal }),
        loadStatus(signal),
      ])
      if (!mountedRef.current || generationRef.current !== myGen) return
      if (!eventsRes.ok) {
        setLoadState("error")
        return
      }
      const body = (await eventsRes.json()) as { events: DriftEvent[]; count: number }
      if (!mountedRef.current || generationRef.current !== myGen) return
      setEvents(body.events ?? [])
      setLoadState("ready")
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return
      if (!mountedRef.current || generationRef.current !== myGen) return
      setLoadState("error")
    }
  }, [filter, loadStatus])

  useEffect(() => {
    mountedRef.current = true
    const ac = new AbortController()
    abortRef.current = ac
    void load(ac.signal)
    return () => {
      mountedRef.current = false
      ac.abort()
    }
  }, [load])

  async function runDrift() {
    setRunning(true)
    setRunMessage(null)
    try {
      const res = await fetch("/api/drift/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      })
      const data = (await res.json()) as {
        enqueued?: number
        deduplicated?: number
        detail?: string
      }
      if (!res.ok) {
        setRunMessage(data.detail ?? "Could not start drift scan.")
        return
      }

      // Backend dedups same-minute clicks: if `deduplicated > 0` and
      // no fresh job was enqueued, tell the user explicitly so they
      // don't think the button is broken.
      const enq = data.enqueued ?? 0
      const dedup = data.deduplicated ?? 0
      if (enq === 0 && dedup > 0) {
        setRunMessage(
          "A drift scan was started in the same minute. Wait a few seconds for it to finish, then click Refresh — or click again in 60s for a fresh run.",
        )
        // Still poll a couple times to catch the in-flight job's
        // result so the heartbeat advances without the user thinking
        // nothing happened.
        for (let i = 0; i < 4; i++) {
          await new Promise((r) => setTimeout(r, 1500))
          if (!mountedRef.current) return
          await loadStatus()
        }
        if (mountedRef.current) await load()
        return
      }

      setRunMessage(`Started a drift scan. Watching for the heartbeat to advance…`)
      const baselinesBefore = status?.baselines ?? 0
      const lastScanBefore = status?.last_scan_at ?? null
      let advanced = false
      for (let i = 0; i < 12; i++) {
        await new Promise((r) => setTimeout(r, 1500))
        if (!mountedRef.current) return
        await loadStatus()
        // Read the latest status off the state setter so we don't
        // race with React batching.
        const advancedNow = await new Promise<boolean>((resolve) => {
          setStatus((curr) => {
            const moved =
              curr !== null &&
              (curr.baselines > baselinesBefore ||
                (curr.last_scan_at !== null &&
                  curr.last_scan_at !== lastScanBefore))
            resolve(moved)
            return curr
          })
        })
        if (advancedNow) {
          advanced = true
          break
        }
      }
      if (mountedRef.current) await load()
      if (mountedRef.current) {
        setRunMessage(
          advanced
            ? "Drift scan finished. The heartbeat below shows the latest run."
            : "Drift scan started but did not finish in 18s. Click Refresh to check status manually.",
        )
        // Auto-clear the success message after 8s so it doesn't sit
        // on screen forever like the previous "Enqueued 1 job(s)" did.
        if (advanced) {
          setTimeout(() => {
            if (mountedRef.current) setRunMessage(null)
          }, 8000)
        }
      }
    } catch {
      setRunMessage("Network error.")
    } finally {
      setRunning(false)
    }
  }

  async function patchEvent(id: string, body: { status: DriftStatus; reason?: string }) {
    setRowError((prev) => ({ ...prev, [id]: null }))
    try {
      const res = await fetch(`/api/drift/events/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      })
      const data = (await res.json()) as DriftEvent & { detail?: { error?: string } | string }
      if (!res.ok) {
        let msg = "Unexpected error."
        if (typeof data.detail === "object" && data.detail?.error) msg = data.detail.error
        else if (typeof data.detail === "string") msg = data.detail
        setRowError((prev) => ({ ...prev, [id]: msg }))
        return
      }
      setEvents((prev) => prev.map((e) => (e.id === id ? (data as DriftEvent) : e)))
    } catch {
      setRowError((prev) => ({ ...prev, [id]: "Network error." }))
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-xl font-semibold flex items-center gap-2">
          <Activity className="h-5 w-5" /> Compliance drift
        </h2>
        <div className="flex-1" />
        <Button
          size="sm"
          variant="outline"
          onClick={() => void load(abortRef.current?.signal)}
          data-testid="drift-button-refresh"
        >
          Refresh
        </Button>
        <Button
          size="sm"
          onClick={runDrift}
          disabled={running}
          data-testid="drift-button-run"
        >
          {running ? "Starting…" : "Run drift scan now"}
        </Button>
      </div>

      {runMessage && (
        <p className="text-xs text-muted-foreground" data-testid="drift-run-message">
          {runMessage}
        </p>
      )}

      {status !== null && (
        <div
          className="rounded-md border bg-muted/40 px-3 py-2 text-xs text-muted-foreground"
          data-testid="drift-status-heartbeat"
        >
          {status.baselines === 0 ? (
            <>
              No baselines recorded yet. Click <span className="font-medium">Run drift scan now</span> to record a baseline for each
              monitored control. The first scan never emits drift events — it only records the current state to compare against
              future scans (system-design 13.3).
            </>
          ) : (
            <>
              <span className="font-medium text-foreground">Last drift scan:</span>{" "}
              {formatRelative(status.last_scan_at)} — scanned{" "}
              <span data-testid="drift-status-baselines" className="font-medium text-foreground">
                {status.baselines}
              </span>{" "}
              control{status.baselines === 1 ? "" : "s"},{" "}
              <span data-testid="drift-status-events-open" className="font-medium text-foreground">
                {status.events_open}
              </span>{" "}
              open drift event{status.events_open === 1 ? "" : "s"}
              {status.events_total > status.events_open && (
                <>
                  {" "}({status.events_total - status.events_open} resolved or dismissed)
                </>
              )}
              .
              {status.events_open === 0 && status.events_total === 0 && (
                <>
                  {" "}Drift events fire only after a configuration changes
                  AND the watcher sees the new state on two consecutive scans.
                </>
              )}
            </>
          )}
        </div>
      )}

      <div className="flex flex-wrap gap-2">
        {(["open", "resolved", "dismissed", "all"] as const).map((f) => (
          <Button
            key={f}
            size="sm"
            variant={filter === f ? "default" : "outline"}
            onClick={() => setFilter(f)}
            data-testid={`drift-filter-${f}`}
          >
            {f}
          </Button>
        ))}
      </div>

      {loadState === "loading" && (
        <div className="space-y-3">
          {[0, 1, 2].map((i) => (
            <Card key={i} data-testid="drift-skeleton">
              <CardHeader>
                <Skeleton className="h-5 w-2/3" />
              </CardHeader>
              <CardContent>
                <Skeleton className="h-4 w-full mb-2" />
                <Skeleton className="h-4 w-4/5" />
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {loadState === "error" && (
        <div
          data-testid="drift-error"
          className="rounded-lg border border-destructive bg-destructive/10 p-4 text-sm text-destructive"
        >
          Failed to load drift events.{" "}
          <button onClick={() => void load()} className="underline">
            Retry
          </button>
        </div>
      )}

      {loadState === "ready" && events.length === 0 && (
        <EmptyState
          icon={ShieldAlert}
          title="No drift events"
          description={
            status && status.baselines > 0
              ? "Baselines are recorded — drift events will appear here only when a configuration changes and the new state is seen on two consecutive scans."
              : "The drift watcher runs on a 6-hour cron. Click 'Run drift scan now' to enqueue a scan."
          }
        />
      )}

      <div className="space-y-3">
        {events.map((event) => {
          const safeHref = safeSourceHref(event.source_link)
          return (
            <Card key={event.id} data-testid="drift-event-card" data-status={event.status}>
              <CardHeader>
                <div className="flex flex-wrap items-start gap-2">
                  <CardTitle className="flex-1 font-bold">
                    {event.control_id}: {EVENT_TYPE_LABEL[event.event_type]}
                  </CardTitle>
                  <Badge variant={SEVERITY_BADGE[event.severity]}>{event.severity}</Badge>
                  <Badge variant={STATUS_BADGE[event.status]}>{event.status}</Badge>
                </div>
              </CardHeader>
              <CardContent className="space-y-2">
                <p className="text-sm">{event.what_changed || "(no summary)"}</p>
                {event.suggested_fix && (
                  <p className="text-xs text-muted-foreground">
                    Suggested fix: {event.suggested_fix}
                  </p>
                )}
                {safeHref && (
                  <a
                    href={safeHref}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-xs text-primary underline-offset-4 hover:underline"
                    data-testid="drift-event-source-link"
                  >
                    View source →
                  </a>
                )}
                {event.status === "open" && (
                  <div className="space-y-2 pt-1">
                    <Input
                      value={dismissDraft[event.id] ?? ""}
                      onChange={(e) =>
                        setDismissDraft((prev) => ({ ...prev, [event.id]: e.target.value }))
                      }
                      placeholder="Reason (required to dismiss)"
                      data-testid="drift-event-dismiss-reason"
                    />
                  </div>
                )}
                {rowError[event.id] && (
                  <p className="text-xs text-destructive" data-testid="drift-event-error">
                    {rowError[event.id]}
                  </p>
                )}
              </CardContent>
              {event.status === "open" && (
                <CardFooter className="flex flex-wrap gap-2">
                  <Button
                    size="sm"
                    onClick={() =>
                      void patchEvent(event.id, { status: "resolved" })
                    }
                    data-testid="drift-event-resolve"
                  >
                    Resolve
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={(dismissDraft[event.id] ?? "").trim() === ""}
                    onClick={() =>
                      void patchEvent(event.id, {
                        status: "dismissed",
                        reason: (dismissDraft[event.id] ?? "").trim(),
                      })
                    }
                    data-testid="drift-event-dismiss"
                  >
                    Dismiss
                  </Button>
                </CardFooter>
              )}
            </Card>
          )
        })}
      </div>
    </div>
  )
}
