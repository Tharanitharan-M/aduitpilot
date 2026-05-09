/**
 * PendingActions — state-machine transition tests (chunk 4.7).
 *
 * Mocks fetch so the component renders against canned ActionOut data.
 * Covers every FE-exposed transition in the state machine:
 *   a. Loading skeletons
 *   b. One card per action
 *   c. Empty state
 *   d. Error banner + retry
 *   e. Approve (pending_review → approved)
 *   f. Reject with reason (pending_review → rejected)
 *   g. Reject without reason — inline error, no PATCH sent
 *   h. Mark as Done from approved (approved → completed)
 *   i. 409 conflict — inline error, status unchanged
 *
 * Refs: PLAN.md chunk 4.7; US-007.
 */

import { render, screen, fireEvent, waitFor, act } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest"
import { PendingActions, type ActionOut } from "@/components/pending-actions"

// ── Mocks ──────────────────────────────────────────────────────────────────

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: async () => "test-token" }),
}))

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
}))

// ── Fixtures ───────────────────────────────────────────────────────────────

const pendingAction: ActionOut = {
  id: "act-001",
  user_id: "user_abc",
  scan_run_id: "scan-1",
  kind: "enable_branch_protection",
  title: "Enable branch protection on main",
  description: "Branch protection is not enabled on the main branch.",
  status: "pending_review",
  tsc_id: "CC6.1",
  source_link: "https://github.com/org/repo/settings/branches",
  rejected_reason: null,
  revoked_reason: null,
  revoked_at: null,
  created_at: "2026-05-01T10:00:00Z",
  updated_at: "2026-05-01T10:00:00Z",
}

const approvedAction: ActionOut = {
  ...pendingAction,
  id: "act-002",
  title: "Require code review",
  description: "Code review is not required before merging.",
  status: "approved",
  tsc_id: "CC6.2",
  source_link: null,
}

const rejectedAction: ActionOut = {
  ...pendingAction,
  id: "act-003",
  title: "Enable access logging",
  status: "rejected",
  rejected_reason: "Already handled by third-party tool.",
}

const completedAction: ActionOut = {
  ...pendingAction,
  id: "act-004",
  title: "Rotate access tokens",
  status: "completed",
}

/** Build a resolved fetch mock returning a list response. */
function mockFetchGet(actions: ActionOut[]) {
  return vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ actions, count: actions.length }),
  })
}

/** Build a fetch mock: GET returns list, PATCH returns the given ActionOut. */
function mockFetchGetAndPatch(actions: ActionOut[], patchResult: ActionOut) {
  let callCount = 0
  return vi.fn().mockImplementation((_url: string, init?: RequestInit) => {
    if (!init?.method || init.method === "GET") {
      callCount++
      if (callCount === 1) {
        return Promise.resolve({
          ok: true,
          json: async () => ({ actions, count: actions.length }),
        })
      }
    }
    if (init?.method === "PATCH") {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => patchResult,
      })
    }
    return Promise.resolve({
      ok: true,
      json: async () => ({ actions, count: actions.length }),
    })
  })
}

// ── Tests ──────────────────────────────────────────────────────────────────

describe("PendingActions", () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  // (a) Loading skeletons ───────────────────────────────────────────────────
  it("renders loading skeletons before the fetch resolves", () => {
    // Never resolves during this test
    global.fetch = vi.fn().mockReturnValue(new Promise(() => {}))
    render(<PendingActions />)
    const skeletons = screen.getAllByTestId("action-skeleton")
    expect(skeletons).toHaveLength(3)
  })

  // (b) One card per action ─────────────────────────────────────────────────
  it("renders one card per action returned", async () => {
    global.fetch = mockFetchGet([pendingAction, approvedAction])
    render(<PendingActions />)
    await waitFor(() =>
      expect(screen.getAllByTestId("action-card")).toHaveLength(2)
    )
    expect(screen.getByText("Enable branch protection on main")).toBeInTheDocument()
    expect(screen.getByText("Require code review")).toBeInTheDocument()
  })

  // (c) Empty state ─────────────────────────────────────────────────────────
  it("renders the empty state when actions=[]", async () => {
    global.fetch = mockFetchGet([])
    render(<PendingActions />)
    await waitFor(() =>
      expect(screen.getByTestId("action-empty-state")).toBeInTheDocument()
    )
    expect(
      screen.getByText(/no pending actions yet/i)
    ).toBeInTheDocument()
  })

  // (d) Error banner ────────────────────────────────────────────────────────
  it("renders an error banner when the GET fails", async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: false, json: async () => ({}) })
    render(<PendingActions />)
    await waitFor(() =>
      expect(screen.getByTestId("action-error-banner")).toBeInTheDocument()
    )
    expect(screen.getByTestId("action-retry-button")).toBeInTheDocument()
  })

  it("retries the fetch when the retry button is clicked", async () => {
    const fetchMock = vi
      .fn()
      // First call fails
      .mockResolvedValueOnce({ ok: false, json: async () => ({}) })
      // Second call succeeds
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ actions: [pendingAction], count: 1 }),
      })
    global.fetch = fetchMock
    render(<PendingActions />)
    await waitFor(() =>
      expect(screen.getByTestId("action-error-banner")).toBeInTheDocument()
    )
    fireEvent.click(screen.getByTestId("action-retry-button"))
    await waitFor(() =>
      expect(screen.getByTestId("action-card")).toBeInTheDocument()
    )
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  // (e) Approve transition: pending_review → approved ───────────────────────
  it("sends PATCH {status: approved} and updates the status badge on Approve click", async () => {
    const approvedResult: ActionOut = { ...pendingAction, status: "approved" }
    global.fetch = mockFetchGetAndPatch([pendingAction], approvedResult)

    render(<PendingActions />)
    await waitFor(() =>
      expect(screen.getByTestId("action-button-approve")).toBeInTheDocument()
    )

    await act(async () => {
      fireEvent.click(screen.getByTestId("action-button-approve"))
    })

    await waitFor(() => {
      const badge = screen.getByTestId("action-status-badge")
      expect(badge).toHaveTextContent("Approved")
    })

    // Verify PATCH was sent with correct body
    const patchCall = (global.fetch as ReturnType<typeof vi.fn>).mock.calls.find(
      (call: unknown[]) => {
        const init = call[1] as RequestInit | undefined
        return init?.method === "PATCH"
      }
    )
    expect(patchCall).toBeDefined()
    expect(JSON.parse(patchCall![1].body as string)).toEqual({
      status: "approved",
    })
  })

  // (f) Reject with reason: pending_review → rejected ───────────────────────
  it("sends PATCH with reason on Reject flow completion", async () => {
    const rejectedResult: ActionOut = {
      ...pendingAction,
      status: "rejected",
      rejected_reason: "Not applicable to our stack.",
    }
    global.fetch = mockFetchGetAndPatch([pendingAction], rejectedResult)

    render(<PendingActions />)
    await waitFor(() =>
      expect(screen.getByTestId("action-button-reject")).toBeInTheDocument()
    )

    // Open reject modal
    fireEvent.click(screen.getByTestId("action-button-reject"))
    await waitFor(() =>
      expect(screen.getByTestId("action-reject-reason-input")).toBeInTheDocument()
    )

    // Type a reason
    fireEvent.change(screen.getByTestId("action-reject-reason-input"), {
      target: { value: "Not applicable to our stack." },
    })

    // Confirm reject
    await act(async () => {
      fireEvent.click(screen.getByTestId("action-button-confirm-reject"))
    })

    await waitFor(() => {
      const badge = screen.getByTestId("action-status-badge")
      expect(badge).toHaveTextContent("Rejected")
    })

    const patchCall = (global.fetch as ReturnType<typeof vi.fn>).mock.calls.find(
      (call: unknown[]) => {
        const init = call[1] as RequestInit | undefined
        return init?.method === "PATCH"
      }
    )
    expect(patchCall).toBeDefined()
    expect(JSON.parse(patchCall![1].body as string)).toEqual({
      status: "rejected",
      reason: "Not applicable to our stack.",
    })
  })

  // (g) Reject without reason — inline error, no PATCH ──────────────────────
  it("shows an inline error and does NOT send PATCH when Confirm Reject is clicked with empty reason", async () => {
    global.fetch = mockFetchGet([pendingAction])

    render(<PendingActions />)
    await waitFor(() =>
      expect(screen.getByTestId("action-button-reject")).toBeInTheDocument()
    )

    fireEvent.click(screen.getByTestId("action-button-reject"))
    await waitFor(() =>
      expect(screen.getByTestId("action-reject-reason-input")).toBeInTheDocument()
    )

    // Leave reason empty and click Confirm
    fireEvent.click(screen.getByTestId("action-button-confirm-reject"))

    // Inline validation error should appear
    await waitFor(() =>
      expect(screen.getByTestId("action-reject-reason-error")).toBeInTheDocument()
    )

    // No PATCH call should have been made
    const fetchMock = global.fetch as ReturnType<typeof vi.fn>
    const patchCalls = fetchMock.mock.calls.filter((call: unknown[]) => {
      const init = call[1] as RequestInit | undefined
      return init?.method === "PATCH"
    })
    expect(patchCalls).toHaveLength(0)
  })

  // (h) Mark as Done from approved: approved → completed ────────────────────
  it("sends PATCH {status: completed} from approved state on Mark as Done click", async () => {
    const completedResult: ActionOut = { ...approvedAction, status: "completed" }
    global.fetch = mockFetchGetAndPatch([approvedAction], completedResult)

    render(<PendingActions />)
    await waitFor(() =>
      expect(screen.getByTestId("action-button-mark-done")).toBeInTheDocument()
    )

    await act(async () => {
      fireEvent.click(screen.getByTestId("action-button-mark-done"))
    })

    await waitFor(() => {
      const badge = screen.getByTestId("action-status-badge")
      expect(badge).toHaveTextContent("Completed")
    })

    const patchCall = (global.fetch as ReturnType<typeof vi.fn>).mock.calls.find(
      (call: unknown[]) => {
        const init = call[1] as RequestInit | undefined
        return init?.method === "PATCH"
      }
    )
    expect(patchCall).toBeDefined()
    expect(JSON.parse(patchCall![1].body as string)).toEqual({
      status: "completed",
    })
  })

  // (i) 409 conflict — inline error, status unchanged ───────────────────────
  it("shows the upstream error message inline and keeps original status on 409", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ actions: [pendingAction], count: 1 }),
      })
      .mockResolvedValueOnce({
        ok: false,
        status: 409,
        json: async () => ({
          detail: "Transition from pending_review to revoked is not allowed.",
        }),
      })
    global.fetch = fetchMock

    render(<PendingActions />)
    await waitFor(() =>
      expect(screen.getByTestId("action-button-approve")).toBeInTheDocument()
    )

    await act(async () => {
      fireEvent.click(screen.getByTestId("action-button-approve"))
    })

    await waitFor(() =>
      expect(screen.getByTestId("action-patch-error")).toBeInTheDocument()
    )

    // Status badge should still show Pending Review
    expect(screen.getByTestId("action-status-badge")).toHaveTextContent(
      "Pending Review"
    )
    // Error message shown
    expect(screen.getByTestId("action-patch-error")).toHaveTextContent(
      "Transition from pending_review to revoked is not allowed."
    )
  })

  // Badge variants ──────────────────────────────────────────────────────────
  it("renders correct status badges for each status", async () => {
    global.fetch = mockFetchGet([
      pendingAction,
      approvedAction,
      rejectedAction,
      completedAction,
    ])
    render(<PendingActions />)
    await waitFor(() =>
      expect(screen.getAllByTestId("action-card")).toHaveLength(4)
    )
    const badges = screen.getAllByTestId("action-status-badge")
    expect(badges[0]).toHaveTextContent("Pending Review")
    expect(badges[1]).toHaveTextContent("Approved")
    expect(badges[2]).toHaveTextContent("Rejected")
    expect(badges[3]).toHaveTextContent("Completed")
  })

  // Terminal state — no buttons for rejected ────────────────────────────────
  it("renders no action buttons for rejected status", async () => {
    global.fetch = mockFetchGet([rejectedAction])
    render(<PendingActions />)
    await waitFor(() =>
      expect(screen.getByTestId("action-card")).toBeInTheDocument()
    )
    expect(screen.queryByTestId("action-button-approve")).not.toBeInTheDocument()
    expect(screen.queryByTestId("action-button-reject")).not.toBeInTheDocument()
    expect(screen.queryByTestId("action-button-mark-done")).not.toBeInTheDocument()
  })

  // Sprint 9 chunk 9.15 — completed actions get a Revert button.
  it("renders only the Revert button for completed status", async () => {
    global.fetch = mockFetchGet([completedAction])
    render(<PendingActions />)
    await waitFor(() =>
      expect(screen.getByTestId("action-card")).toBeInTheDocument()
    )
    expect(screen.queryByTestId("action-button-approve")).not.toBeInTheDocument()
    expect(screen.queryByTestId("action-button-mark-done")).not.toBeInTheDocument()
    expect(screen.getByTestId("action-button-revert")).toBeInTheDocument()
  })

  // Sprint 9 chunk 9.15 — Revert flow happy path.
  it("sends PATCH {status: revoked, reason} when Revert flow is confirmed", async () => {
    const revokedResult: ActionOut = {
      ...completedAction,
      status: "revoked",
      revoked_reason: "Compliance team asked us to redo the fix.",
      revoked_at: "2026-05-09T01:00:00Z",
    }
    global.fetch = mockFetchGetAndPatch([completedAction], revokedResult)
    render(<PendingActions />)
    await waitFor(() => screen.getByTestId("action-button-revert"))
    fireEvent.click(screen.getByTestId("action-button-revert"))
    fireEvent.change(screen.getByTestId("action-revert-reason-input"), {
      target: { value: "Compliance team asked us to redo the fix." },
    })
    fireEvent.click(screen.getByTestId("action-button-confirm-revert"))
    await waitFor(() => {
      expect((global.fetch as ReturnType<typeof vi.fn>).mock.calls).toHaveLength(2)
    })
    const patchCall = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[1]
    expect(patchCall[0]).toBe(`/api/actions/${completedAction.id}`)
    const sent = JSON.parse(patchCall[1].body)
    expect(sent).toEqual({
      status: "revoked",
      reason: "Compliance team asked us to redo the fix.",
    })
  })

  // Sprint 9 chunk 9.15 — empty reason is blocked client-side.
  it("Revert without reason does not PATCH", async () => {
    global.fetch = mockFetchGet([completedAction])
    render(<PendingActions />)
    await waitFor(() => screen.getByTestId("action-button-revert"))
    fireEvent.click(screen.getByTestId("action-button-revert"))
    fireEvent.click(screen.getByTestId("action-button-confirm-revert"))
    expect(screen.getByTestId("action-revert-reason-error")).toBeInTheDocument()
    // Only the initial GET was made.
    expect((global.fetch as ReturnType<typeof vi.fn>).mock.calls).toHaveLength(1)
  })

  // tsc_id badge rendered ───────────────────────────────────────────────────
  it("renders tsc_id badge when present", async () => {
    global.fetch = mockFetchGet([pendingAction]) // has tsc_id: "CC6.1"
    render(<PendingActions />)
    await waitFor(() =>
      expect(screen.getByText("CC6.1")).toBeInTheDocument()
    )
  })

  // source_link rendered ────────────────────────────────────────────────────
  it("renders source_link as external link when present", async () => {
    global.fetch = mockFetchGet([pendingAction])
    render(<PendingActions />)
    await waitFor(() =>
      expect(screen.getByTestId("action-source-link")).toBeInTheDocument()
    )
    expect(screen.getByTestId("action-source-link")).toHaveAttribute(
      "href",
      "https://github.com/org/repo/settings/branches"
    )
  })
})
