"use client"

/**
 * RepoPicker — client component for the connector-scope picker page.
 *
 * Renders a shadcn Table with one row per repo, a checkbox per row, a
 * search input, and a "Save scope" button. The full desired selection
 * is submitted on save (PATCH /api/connectors/:id/scoped-repos — full
 * replace, not delta). Default-deny: nothing is selected on first
 * render (ADR-0015).
 *
 * Refs: PLAN.md Sprint 3.5 chunk 3.5.2; ADR-0015; US-002.
 */

import { useEffect, useMemo, useRef, useState } from "react"
import { useRouter } from "next/navigation"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Badge } from "@/components/ui/badge"

export interface PickerRepo {
  provider_repo_id: string
  full_name: string
  private: boolean
}

interface RepoPickerProps {
  connectorId: string
  repos: PickerRepo[]
  initialSelectedIds: string[]
}

export function RepoPicker({
  connectorId,
  repos,
  initialSelectedIds,
}: RepoPickerProps) {
  const router = useRouter()
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(initialSelectedIds)
  )
  const [query, setQuery] = useState("")
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // typescript-reviewer HIGH-1: in-flight save needs to be abortable so a
  // mid-save navigation does not orphan the fetch.
  const abortRef = useRef<AbortController | null>(null)
  useEffect(() => {
    return () => {
      abortRef.current?.abort()
    }
  }, [])

  const filtered = useMemo(() => {
    if (!query.trim()) return repos
    const q = query.toLowerCase()
    return repos.filter((r) => r.full_name.toLowerCase().includes(q))
  }, [repos, query])

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function selectAllVisible() {
    setSelected((prev) => {
      const next = new Set(prev)
      for (const r of filtered) next.add(r.provider_repo_id)
      return next
    })
  }

  function clearAllVisible() {
    setSelected((prev) => {
      const next = new Set(prev)
      for (const r of filtered) next.delete(r.provider_repo_id)
      return next
    })
  }

  async function save() {
    setSaving(true)
    setError(null)
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    try {
      const payload = {
        repos: repos
          .filter((r) => selected.has(r.provider_repo_id))
          .map((r) => ({
            provider_repo_id: r.provider_repo_id,
            full_name: r.full_name,
            private: r.private,
          })),
      }
      const res = await fetch(
        `/api/connectors/${connectorId}/scoped-repos`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
          signal: controller.signal,
        }
      )
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body?.detail ?? `HTTP ${res.status}`)
      }
      // refresh BEFORE push so the dashboard sees fresh scoped_repo_count
      // (typescript-reviewer MEDIUM-5: push first re-routes Server Component
      // refresh to the wrong path).
      router.refresh()
      router.push("/dashboard")
    } catch (err: unknown) {
      if ((err as { name?: string })?.name === "AbortError") return
      setError(err instanceof Error ? err.message : "Save failed")
    } finally {
      setSaving(false)
      abortRef.current = null
    }
  }

  const selectedCount = selected.size

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <Input
          type="search"
          placeholder="Search repos…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Search repos"
          className="sm:max-w-xs"
        />
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={selectAllVisible}
            disabled={filtered.length === 0}
          >
            Select all visible
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={clearAllVisible}
            disabled={selectedCount === 0}
          >
            Clear visible
          </Button>
        </div>
      </div>

      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-10" aria-label="Select" />
              <TableHead>Repository</TableHead>
              <TableHead className="w-24">Visibility</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {filtered.length === 0 ? (
              <TableRow>
                <TableCell colSpan={3} className="text-center text-sm text-muted-foreground">
                  No repos match.
                </TableCell>
              </TableRow>
            ) : (
              filtered.map((r) => {
                const checked = selected.has(r.provider_repo_id)
                return (
                  <TableRow
                    key={r.provider_repo_id}
                    data-testid="repo-row"
                    data-selected={checked ? "true" : "false"}
                  >
                    <TableCell>
                      <Checkbox
                        checked={checked}
                        onCheckedChange={() => toggle(r.provider_repo_id)}
                        aria-label={`Select ${r.full_name}`}
                      />
                    </TableCell>
                    <TableCell className="font-medium">{r.full_name}</TableCell>
                    <TableCell>
                      <Badge variant={r.private ? "secondary" : "outline"}>
                        {r.private ? "Private" : "Public"}
                      </Badge>
                    </TableCell>
                  </TableRow>
                )
              })
            )}
          </TableBody>
        </Table>
      </div>

      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground" aria-live="polite">
          {selectedCount === 0
            ? "Pick at least one repo to scan."
            : `${selectedCount} selected`}
        </p>
        {error && (
          <p className="text-sm text-destructive" data-testid="picker-error">
            {error}
          </p>
        )}
        <Button
          onClick={save}
          // Disable when nothing is selected — prevents the user from
          // accidentally clearing their persisted scope (typescript-
          // reviewer MEDIUM-4). Server-side, the FastAPI ScopedReposPatch
          // schema also caps at 500 rows; the empty list is a deliberate
          // "clear all" path which is reachable only via API, not UI.
          disabled={saving || selectedCount === 0}
          size="sm"
        >
          {saving ? "Saving…" : "Save scope"}
        </Button>
      </div>
    </div>
  )
}
