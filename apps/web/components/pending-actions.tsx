"use client"

/**
 * PendingActions — Pending Actions queue UI for the /dashboard page.
 *
 * Fetches GET /api/actions on mount. Renders one shadcn Card per action with
 * status-appropriate affordances:
 *   pending_review → Approve, Edit (inline, client-only Sprint 4), Reject
 *                    (reason modal inline), Mark as Done
 *   approved       → Mark as Done
 *   completed      → read-only (Sprint 9 adds Revert)
 *   rejected       → read-only, shows rejected_reason
 *   revoked        → read-only, shows revoked_reason
 *
 * Optimistic updates: a successful PATCH replaces the row in local state.
 * On 409 / 422 the upstream `detail` string is shown inline on the card.
 *
 * Edit mode is client-side only in Sprint 4 — the backend PATCH endpoint does
 * not yet accept a `description` field. Refreshing the page reverts the local
 * edit. See TODO below.
 *
 * Refs: PLAN.md chunk 4.7; US-007; ADR-0008.
 */

import { useEffect, useRef, useState, useCallback } from "react"
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

/** Allowlist of host suffixes a `source_link` is permitted to point at.
 * The orchestrator currently emits links only into github.com (read-only
 * deeplinks to repo settings) so the allowlist is intentionally small.
 * Sprint 5 will widen this when evidence-store-mcp adds R2-hosted
 * artefact links. */
const _SOURCE_LINK_HOST_ALLOWLIST: ReadonlyArray<string> = [
  "github.com",
  "www.github.com",
]

/** Return the URL only if it is a valid absolute https URL pointing at
 * an allowlisted host. Returns ``null`` for any other input —
 * including ``javascript:``, ``data:``, ``http://``, relative paths,
 * and unknown hosts.
 *
 * Refs: typescript-reviewer C-1 (Sprint 4 reviewer pass).
 */
function _toSafeSourceHref(value: string | null): string | null {
  if (!value) return null
  let url: URL
  try {
    url = new URL(value)
  } catch {
    return null
  }
  if (url.protocol !== "https:") return null
  return _SOURCE_LINK_HOST_ALLOWLIST.includes(url.host.toLowerCase())
    ? url.toString()
    : null
}

// ── Types ─────────────────────────────────────────────────────────────────

export type ActionStatus =
  | "pending_review"
  | "approved"
  | "rejected"
  | "completed"
  | "revoked"

export interface ActionOut {
  id: string
  user_id: string
  scan_run_id: string | null
  kind: string
  title: string
  description: string
  status: ActionStatus
  tsc_id: string | null
  source_link: string | null
  rejected_reason: string | null
  revoked_reason: string | null
  revoked_at: string | null
  created_at: string
  updated_at: string
}

// ── Status badge helper ───────────────────────────────────────────────────

type BadgeVariant =
  | "outline"
  | "secondary"
  | "destructive"
  | "success"
  | "default"

const STATUS_BADGE_VARIANT: Record<ActionStatus, BadgeVariant> = {
  pending_review: "outline",
  approved: "secondary",
  rejected: "destructive",
  completed: "success",
  revoked: "outline",
}

const STATUS_LABEL: Record<ActionStatus, string> = {
  pending_review: "Pending Review",
  approved: "Approved",
  rejected: "Rejected",
  completed: "Completed",
  revoked: "Revoked",
}

// ── Loading skeletons ─────────────────────────────────────────────────────

function ActionSkeletons() {
  return (
    <div className="space-y-4">
      {[0, 1, 2].map((i) => (
        <Card key={i} data-testid="action-skeleton">
          <CardHeader>
            <Skeleton className="h-5 w-2/3" />
          </CardHeader>
          <CardContent>
            <Skeleton className="h-4 w-full mb-2" />
            <Skeleton className="h-4 w-4/5" />
          </CardContent>
          <CardFooter>
            <Skeleton className="h-8 w-24" />
          </CardFooter>
        </Card>
      ))}
    </div>
  )
}

// ── Single action card ────────────────────────────────────────────────────

interface ActionCardProps {
  action: ActionOut
  /** Local description override (Sprint 4 client-only edit). */
  localDescription: string | null
  /** Whether this card is in "rejecting" mode (reason input shown). */
  isRejecting: boolean
  /** The current value of the reject reason input. */
  rejectReason: string
  /** Error string from the last PATCH call on this card. */
  patchError: string | null
  /** Whether the description is being edited inline. */
  isEditing: boolean
  /** The current value of the edit textarea. */
  editValue: string
  onApprove: () => void
  onStartReject: () => void
  onRejectReasonChange: (v: string) => void
  onConfirmReject: () => void
  onCancelReject: () => void
  onMarkDone: () => void
  onStartEdit: () => void
  onEditChange: (v: string) => void
  onSaveEdit: () => void
  onCancelEdit: () => void
}

function ActionCard({
  action,
  localDescription,
  isRejecting,
  rejectReason,
  patchError,
  isEditing,
  editValue,
  onApprove,
  onStartReject,
  onRejectReasonChange,
  onConfirmReject,
  onCancelReject,
  onMarkDone,
  onStartEdit,
  onEditChange,
  onSaveEdit,
  onCancelEdit,
}: ActionCardProps) {
  const description = localDescription ?? action.description
  const hasLocalEdit = localDescription !== null && localDescription !== action.description

  return (
    <Card data-testid="action-card" data-status={action.status} data-id={action.id}>
      {/* ── Header ── */}
      <CardHeader>
        <div className="flex flex-wrap items-start gap-2">
          <CardTitle className="flex-1 font-bold">{action.title}</CardTitle>
          <Badge
            variant={STATUS_BADGE_VARIANT[action.status]}
            data-testid="action-status-badge"
          >
            {STATUS_LABEL[action.status]}
          </Badge>
          {action.tsc_id && (
            <Badge variant="outline" className="font-mono text-xs">
              {action.tsc_id}
            </Badge>
          )}
        </div>
      </CardHeader>

      {/* ── Content ── */}
      <CardContent className="space-y-2">
        {isEditing ? (
          <div className="space-y-2">
            <textarea
              data-testid="action-edit-textarea"
              value={editValue}
              onChange={(e) => onEditChange(e.target.value)}
              className="w-full rounded-md border border-input bg-transparent px-2.5 py-1.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/50 min-h-[80px] resize-y"
              aria-label="Edit description"
            />
            <p className="text-xs text-muted-foreground">
              (unsaved — server-side persistence in Sprint 5)
            </p>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">
            {description}
            {hasLocalEdit && (
              <span className="ml-2 text-xs text-amber-600">(unsaved — server-side persistence in Sprint 5)</span>
            )}
          </p>
        )}

        {/* typescript-reviewer C-1 — never render an unvalidated URL as
            an href. ``javascript:alert(1)`` defeats target/rel; an open
            redirect to a phishing host defeats trust. We require an
            absolute https:// URL whose host is on the allowlist (the
            backend feeds these via the `source_link` column, which is
            populated only by the orchestrator's own deeplink builders —
            but defense-in-depth at the rendering boundary is cheap). */}
        {(() => {
          const safeHref = _toSafeSourceHref(action.source_link)
          if (!safeHref) return null
          return (
            <a
              href={safeHref}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-primary underline-offset-4 hover:underline"
              data-testid="action-source-link"
            >
              View in GitHub →
            </a>
          )
        })()}

        {/* Terminal state reason display */}
        {action.status === "rejected" && action.rejected_reason && (
          <p className="text-xs text-destructive">
            Reason: {action.rejected_reason}
          </p>
        )}
        {action.status === "revoked" && action.revoked_reason && (
          <p className="text-xs text-muted-foreground">
            Revoked: {action.revoked_reason}
          </p>
        )}

        {/* Inline reject reason input */}
        {isRejecting && (
          <div className="space-y-2 pt-1">
            <Input
              data-testid="action-reject-reason-input"
              placeholder="Reason for rejection (required)"
              value={rejectReason}
              onChange={(e) => onRejectReasonChange(e.target.value)}
              aria-label="Reject reason"
            />
            {rejectReason.trim() === "" && (
              <p
                data-testid="action-reject-reason-error"
                className="text-xs text-destructive"
              >
                A reason is required to reject this action.
              </p>
            )}
          </div>
        )}

        {/* Inline patch error */}
        {patchError && (
          <p
            data-testid="action-patch-error"
            className="text-xs text-destructive"
          >
            {patchError}
          </p>
        )}
      </CardContent>

      {/* ── Footer ── */}
      <CardFooter className="flex flex-wrap gap-2">
        {/* Edit mode buttons */}
        {isEditing && (
          <>
            <Button
              size="sm"
              variant="default"
              data-testid="action-button-save-edit"
              onClick={onSaveEdit}
            >
              Save
            </Button>
            <Button
              size="sm"
              variant="ghost"
              data-testid="action-button-cancel-edit"
              onClick={onCancelEdit}
            >
              Cancel
            </Button>
          </>
        )}

        {/* Reject confirm/cancel */}
        {isRejecting && !isEditing && (
          <>
            <Button
              size="sm"
              variant="destructive"
              data-testid="action-button-confirm-reject"
              onClick={onConfirmReject}
            >
              Confirm Reject
            </Button>
            <Button
              size="sm"
              variant="ghost"
              data-testid="action-button-cancel-reject"
              onClick={onCancelReject}
            >
              Cancel
            </Button>
          </>
        )}

        {/* Primary action buttons — only shown when not in edit/reject mode */}
        {!isEditing && !isRejecting && (
          <>
            {action.status === "pending_review" && (
              <>
                <Button
                  size="sm"
                  variant="default"
                  data-testid="action-button-approve"
                  onClick={onApprove}
                >
                  Approve
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  data-testid="action-button-edit"
                  onClick={onStartEdit}
                >
                  Edit
                </Button>
                <Button
                  size="sm"
                  variant="destructive"
                  data-testid="action-button-reject"
                  onClick={onStartReject}
                >
                  Reject
                </Button>
                <Button
                  size="sm"
                  variant="secondary"
                  data-testid="action-button-mark-done"
                  onClick={onMarkDone}
                >
                  Mark as Done
                </Button>
              </>
            )}

            {action.status === "approved" && (
              <Button
                size="sm"
                variant="secondary"
                data-testid="action-button-mark-done"
                onClick={onMarkDone}
              >
                Mark as Done
              </Button>
            )}
            {/* completed / rejected / revoked — no buttons (Sprint 9 adds Revert for completed) */}
          </>
        )}
      </CardFooter>
    </Card>
  )
}

// ── Main component ────────────────────────────────────────────────────────

type LoadState = "loading" | "error" | "ready"

interface RowState {
  /** Client-side description override. null = use server value. */
  localDescription: string | null
  isRejecting: boolean
  rejectReason: string
  patchError: string | null
  isEditing: boolean
  editValue: string
}

function defaultRowState(action: ActionOut): RowState {
  return {
    localDescription: null,
    isRejecting: false,
    rejectReason: "",
    patchError: null,
    isEditing: false,
    editValue: action.description,
  }
}

export function PendingActions() {
  const [loadState, setLoadState] = useState<LoadState>("loading")
  const [actions, setActions] = useState<ActionOut[]>([])
  const [rowStates, setRowStates] = useState<Record<string, RowState>>({})

  // Sprint 4 chunk 4.18 — race guard.
  //
  // Two failure modes the guard closes:
  //
  // 1. Component-unmount race. The user navigates away mid-fetch; the
  //    in-flight ``fetch`` resolves and the resolver calls ``setState`` on
  //    an unmounted component (React logs a warning, and worse: a stale
  //    response can leak across navigations).
  // 2. Concurrent-load race. ``load()`` is invoked again (via mutation
  //    success or a future polling timer) while the previous request is
  //    still in flight. Whichever response arrives second wins, even if
  //    its underlying snapshot is older — the UI flickers between two
  //    valid-but-stale states.
  //
  // ``loadGenerationRef`` increments on every ``load()`` call. Every
  // resolver checks ``loadGenerationRef.current === myGeneration`` before
  // calling setState — older generations silently discard their results.
  // ``mountedRef`` flips to false on cleanup so post-unmount setState is
  // also blocked. Both refs combined cover both failure modes.
  const mountedRef = useRef(true)
  const loadGenerationRef = useRef(0)

  // ── Fetch ──────────────────────────────────────────────────────────────

  const load = useCallback(async () => {
    const myGeneration = ++loadGenerationRef.current
    setLoadState("loading")
    try {
      const res = await fetch("/api/actions", { cache: "no-store" })
      // Race guard: this response is stale if a newer load() ran after us.
      if (!mountedRef.current || loadGenerationRef.current !== myGeneration) {
        return
      }
      if (!res.ok) {
        setLoadState("error")
        return
      }
      const data = await res.json() as { actions: ActionOut[]; count: number }
      // Re-check after the second await — JSON parse can be slow on big payloads.
      if (!mountedRef.current || loadGenerationRef.current !== myGeneration) {
        return
      }
      setActions(data.actions ?? [])
      // Initialise per-row state for any new action ids.
      setRowStates((prev) => {
        const next = { ...prev }
        for (const a of data.actions ?? []) {
          if (!next[a.id]) next[a.id] = defaultRowState(a)
        }
        return next
      })
      setLoadState("ready")
    } catch {
      if (!mountedRef.current || loadGenerationRef.current !== myGeneration) {
        return
      }
      setLoadState("error")
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    load()
    return () => {
      mountedRef.current = false
    }
  }, [load])

  // ── Row state helpers ──────────────────────────────────────────────────

  function patchRow(id: string, partial: Partial<RowState>) {
    setRowStates((prev) => ({
      ...prev,
      [id]: { ...prev[id], ...partial },
    }))
  }

  // ── PATCH helper ───────────────────────────────────────────────────────

  async function sendPatch(
    id: string,
    body: { status: ActionStatus; reason?: string }
  ) {
    patchRow(id, { patchError: null })
    try {
      const res = await fetch(`/api/actions/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      })
      const data = await res.json() as ActionOut & { detail?: { error?: string } | string }
      if (!res.ok) {
        // Surface the upstream error message inline on the card.
        let errMsg = "An unexpected error occurred."
        if (typeof data.detail === "object" && data.detail?.error) {
          errMsg = data.detail.error
        } else if (typeof data.detail === "string") {
          errMsg = data.detail
        }
        patchRow(id, { patchError: errMsg })
        return
      }
      // Optimistic update: replace the row with the server response.
      setActions((prev) =>
        prev.map((a) => (a.id === id ? (data as ActionOut) : a))
      )
      // Reset row state for this id using the fresh action data.
      setRowStates((prev) => ({
        ...prev,
        [id]: defaultRowState(data as ActionOut),
      }))
    } catch {
      patchRow(id, { patchError: "Network error — please try again." })
    }
  }

  // ── Render ─────────────────────────────────────────────────────────────

  if (loadState === "loading") return <ActionSkeletons />

  if (loadState === "error") {
    return (
      <div
        data-testid="action-error-banner"
        className="rounded-lg border border-destructive bg-destructive/10 p-4 text-sm text-destructive"
      >
        Failed to load pending actions.{" "}
        <button
          onClick={load}
          className="underline underline-offset-4 hover:no-underline"
          data-testid="action-retry-button"
        >
          Retry
        </button>
      </div>
    )
  }

  if (actions.length === 0) {
    return (
      <p
        data-testid="action-empty-state"
        className="text-sm text-muted-foreground"
      >
        No pending actions yet — run a readiness scan to surface fixes.
      </p>
    )
  }

  return (
    <div className="space-y-4">
      {actions.map((action) => {
        const rs = rowStates[action.id] ?? defaultRowState(action)
        return (
          <ActionCard
            key={action.id}
            action={action}
            localDescription={rs.localDescription}
            isRejecting={rs.isRejecting}
            rejectReason={rs.rejectReason}
            patchError={rs.patchError}
            isEditing={rs.isEditing}
            editValue={rs.editValue}
            /* Approve. typescript-reviewer C-2 — we explicitly catch
               here so an unhandled rejection cannot escape the React
               event loop. ``sendPatch`` already updates the row's
               ``patchError`` on the failure path, so the catch is a
               last-resort net for genuinely unexpected throws. */
            onApprove={() => {
              void sendPatch(action.id, { status: "approved" }).catch(
                (err) => console.error("approve.unexpected_error", err)
              )
            }}
            /* Reject flow */
            onStartReject={() =>
              patchRow(action.id, { isRejecting: true, rejectReason: "", patchError: null })
            }
            onRejectReasonChange={(v) => patchRow(action.id, { rejectReason: v })}
            onConfirmReject={() => {
              if (rs.rejectReason.trim() === "") return // guard: do not send
              void sendPatch(action.id, {
                status: "rejected",
                reason: rs.rejectReason.trim(),
              }).catch((err) => console.error("reject.unexpected_error", err))
            }}
            onCancelReject={() =>
              patchRow(action.id, { isRejecting: false, rejectReason: "" })
            }
            /* Mark as Done */
            onMarkDone={() => {
              void sendPatch(action.id, { status: "completed" }).catch(
                (err) => console.error("mark_done.unexpected_error", err)
              )
            }}
            /* Edit flow — client-only Sprint 4.
             * TODO Sprint 5 chunk 5.x: add `description` column to the actions
             * table and accept `description` in PATCH /api/actions/{id} so that
             * edits persist. Until then Save only updates local React state. */
            onStartEdit={() =>
              patchRow(action.id, { isEditing: true, editValue: rs.localDescription ?? action.description })
            }
            onEditChange={(v) => patchRow(action.id, { editValue: v })}
            onSaveEdit={() =>
              patchRow(action.id, { isEditing: false, localDescription: rs.editValue })
            }
            onCancelEdit={() =>
              patchRow(action.id, { isEditing: false, editValue: rs.localDescription ?? action.description })
            }
          />
        )
      })}
    </div>
  )
}
