"use client"

/**
 * ScanRunsClient — Sprint 9 chunks 9.11 + 9.13.
 *
 * - Lists the user's scan runs newest first
 * - Re-run button on each row -> POST /api/scan-runs (chunk 9.11)
 * - Pick two rows + click "Compare" -> renders side-by-side ScanRunDiff
 *   from GET /api/scan-runs/diff (chunk 9.13)
 *
 * Refs: PLAN.md chunks 9.10, 9.11, 9.12, 9.13; system-design 15.
 */

import { useCallback, useEffect, useRef, useState } from "react"
import Link from "next/link"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { EmptyState } from "@/components/empty-state"
import { History } from "lucide-react"

interface ScanRunOut {
  id: string
  user_id: string
  connector_id: string | null
  repo_include_list: string[]
  status: "running" | "completed" | "failed" | "cancelled"
  started_at: string
  completed_at: string | null
  cancelled: boolean
  parent_scan_run_id: string | null
}

interface ControlDiff {
  control_id: string
  a_status: string
  b_status: string
  a_confidence: number
  b_confidence: number
  rationale_changed: boolean
}

interface ScanRunDiff {
  a: string
  b: string
  controls_changed: ControlDiff[]
  evidence_added: string[]
  evidence_removed: string[]
}

const STATUS_BADGE: Record<ScanRunOut["status"], "outline" | "secondary" | "default" | "destructive"> = {
  running: "default",
  completed: "secondary",
  failed: "destructive",
  cancelled: "outline",
}

function diffCellTone(a: string, b: string): string {
  if (a === b) return "text-muted-foreground"
  const order = ["passing", "partial", "unknown", "failing"]
  const ai = order.indexOf(a)
  const bi = order.indexOf(b)
  if (ai === -1 || bi === -1) return ""
  return bi > ai ? "text-destructive" : "text-emerald-700 dark:text-emerald-400"
}

export function ScanRunsClient() {
  const [loadState, setLoadState] = useState<"loading" | "ready" | "error">("loading")
  const [runs, setRuns] = useState<ScanRunOut[]>([])
  const [pickA, setPickA] = useState<string | null>(null)
  const [pickB, setPickB] = useState<string | null>(null)
  const [diff, setDiff] = useState<ScanRunDiff | null>(null)
  const [diffError, setDiffError] = useState<string | null>(null)
  const [rerunError, setRerunError] = useState<Record<string, string | null>>({})

  const generationRef = useRef(0)
  const mountedRef = useRef(true)
  const abortRef = useRef<AbortController | null>(null)

  const load = useCallback(async (signal?: AbortSignal) => {
    const myGen = ++generationRef.current
    setLoadState("loading")
    try {
      const res = await fetch("/api/scan-runs", { cache: "no-store", signal })
      if (!mountedRef.current || generationRef.current !== myGen) return
      if (!res.ok) {
        setLoadState("error")
        return
      }
      const body = (await res.json()) as { runs: ScanRunOut[]; count: number }
      if (!mountedRef.current || generationRef.current !== myGen) return
      setRuns(body.runs ?? [])
      setLoadState("ready")
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return
      if (!mountedRef.current || generationRef.current !== myGen) return
      setLoadState("error")
    }
  }, [])

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

  async function rerun(parentId: string) {
    setRerunError((prev) => ({ ...prev, [parentId]: null }))
    try {
      const res = await fetch("/api/scan-runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source: "rerun",
          parent: parentId,
          params_override: {},
        }),
      })
      const data = (await res.json()) as ScanRunOut & { detail?: string }
      if (!res.ok) {
        setRerunError((prev) => ({ ...prev, [parentId]: data.detail ?? "Rerun failed" }))
        return
      }
      // Prepend the new run.
      setRuns((prev) => [data as ScanRunOut, ...prev])
    } catch {
      setRerunError((prev) => ({ ...prev, [parentId]: "Network error" }))
    }
  }

  async function compare() {
    if (!pickA || !pickB || pickA === pickB) {
      setDiffError("Pick two different runs to compare.")
      return
    }
    setDiffError(null)
    try {
      const res = await fetch(
        `/api/scan-runs/diff?a=${encodeURIComponent(pickA)}&b=${encodeURIComponent(pickB)}`,
      )
      const data = (await res.json()) as ScanRunDiff & { detail?: string }
      if (!res.ok) {
        setDiff(null)
        setDiffError(data.detail ?? "Diff failed")
        return
      }
      setDiff(data as ScanRunDiff)
    } catch {
      setDiffError("Network error")
    }
  }

  function togglePick(id: string) {
    if (pickA === id) {
      setPickA(null)
      return
    }
    if (pickB === id) {
      setPickB(null)
      return
    }
    if (pickA === null) setPickA(id)
    else if (pickB === null) setPickB(id)
    else setPickB(id)
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-xl font-semibold flex items-center gap-2">
          <History className="h-5 w-5" /> Scan runs
        </h2>
        <div className="flex-1" />
        <Button size="sm" variant="outline" onClick={() => void load()}>
          Refresh
        </Button>
        <Button
          size="sm"
          onClick={() => void compare()}
          disabled={!pickA || !pickB}
          data-testid="scan-runs-compare"
        >
          Compare selected
        </Button>
      </div>

      {pickA && pickB && (
        <p className="text-xs text-muted-foreground">
          Comparing <code className="font-mono">{pickA.slice(0, 8)}</code> vs{" "}
          <code className="font-mono">{pickB.slice(0, 8)}</code>
        </p>
      )}

      {diffError && (
        <p className="text-sm text-destructive" data-testid="scan-runs-diff-error">
          {diffError}
        </p>
      )}

      {diff && (
        <Card data-testid="scan-runs-diff-panel">
          <CardHeader>
            <CardTitle>Diff</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b text-left">
                    <th className="pb-2">Control</th>
                    <th className="pb-2">A status</th>
                    <th className="pb-2">B status</th>
                    <th className="pb-2">A conf.</th>
                    <th className="pb-2">B conf.</th>
                    <th className="pb-2">Rationale</th>
                  </tr>
                </thead>
                <tbody>
                  {diff.controls_changed.map((c) => (
                    <tr key={c.control_id} className="border-b last:border-0">
                      <td className="py-1 font-mono">{c.control_id}</td>
                      {/* typescript-reviewer M-5: A column is the
                          baseline — leave it neutral. Only B (the new
                          value) is coloured by the regression /
                          improvement direction. */}
                      <td className="py-1 text-muted-foreground">
                        {c.a_status || "—"}
                      </td>
                      <td className={`py-1 ${diffCellTone(c.a_status, c.b_status)}`}>
                        {c.b_status || "—"}
                      </td>
                      <td className="py-1">{c.a_confidence.toFixed(2)}</td>
                      <td className="py-1">{c.b_confidence.toFixed(2)}</td>
                      <td className="py-1 text-xs">
                        {c.rationale_changed ? "changed" : "—"}
                      </td>
                    </tr>
                  ))}
                  {diff.controls_changed.length === 0 && (
                    <tr>
                      <td colSpan={6} className="py-2 text-muted-foreground">
                        No control differences.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
            <p className="mt-3 text-xs text-muted-foreground">
              Evidence added: {diff.evidence_added.length} / removed:{" "}
              {diff.evidence_removed.length}
            </p>
          </CardContent>
        </Card>
      )}

      {loadState === "loading" && (
        <div className="space-y-2">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-20 w-full" />
          ))}
        </div>
      )}

      {loadState === "error" && (
        <div className="rounded-lg border border-destructive bg-destructive/10 p-4 text-sm text-destructive">
          Failed to load scan runs.{" "}
          <button onClick={() => void load()} className="underline">
            Retry
          </button>
        </div>
      )}

      {loadState === "ready" && runs.length === 0 && (
        <EmptyState
          icon={History}
          title="No scan runs yet"
          description="Run a readiness scan from the Chat page to populate this list."
        />
      )}

      <div className="space-y-3">
        {runs.map((run) => {
          const isPicked = run.id === pickA || run.id === pickB
          return (
            <Card
              key={run.id}
              data-testid="scan-run-card"
              className={isPicked ? "ring-2 ring-primary" : ""}
            >
              <CardHeader>
                <div className="flex flex-wrap items-start gap-2">
                  <CardTitle className="flex-1 font-mono text-sm">
                    {run.id.slice(0, 8)}…
                  </CardTitle>
                  <Badge variant={STATUS_BADGE[run.status]}>{run.status}</Badge>
                  {run.parent_scan_run_id && (
                    <Badge variant="outline" data-testid="scan-run-rerun-badge">
                      re-run
                    </Badge>
                  )}
                </div>
              </CardHeader>
              <CardContent className="space-y-1 text-sm">
                <p className="text-muted-foreground">
                  Started: {new Date(run.started_at).toLocaleString()}
                </p>
                <p className="text-xs text-muted-foreground">
                  {run.repo_include_list.length} repo(s) in scope
                </p>
                {run.parent_scan_run_id && (
                  <p className="text-xs">
                    Parent:{" "}
                    <Link
                      href={`/dashboard/scan-runs/${run.parent_scan_run_id}`}
                      className="font-mono underline"
                    >
                      {run.parent_scan_run_id.slice(0, 8)}…
                    </Link>
                  </p>
                )}
                {rerunError[run.id] && (
                  <p className="text-xs text-destructive">{rerunError[run.id]}</p>
                )}
              </CardContent>
              <CardFooter className="flex flex-wrap gap-2">
                <Button
                  size="sm"
                  variant={isPicked ? "default" : "outline"}
                  onClick={() => togglePick(run.id)}
                  data-testid="scan-run-pick"
                >
                  {isPicked ? "Picked" : "Pick"}
                </Button>
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => void rerun(run.id)}
                  data-testid="scan-run-rerun"
                >
                  Re-run
                </Button>
                <Link
                  href={`/dashboard/scan-runs/${run.id}`}
                  className="text-xs text-primary underline-offset-4 hover:underline self-center"
                >
                  Detail →
                </Link>
              </CardFooter>
            </Card>
          )
        })}
      </div>
    </div>
  )
}
