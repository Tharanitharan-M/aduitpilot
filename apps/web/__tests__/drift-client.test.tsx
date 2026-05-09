/**
 * DriftClient — Sprint 9 chunks 9.6, 9.8, 9.9 + post-Sprint-9 heartbeat.
 *
 * Covers:
 *   - Loading skeletons -> ready state
 *   - Empty state when zero events
 *   - Renders one card per event with severity + status badge
 *   - Resolve action: PATCHes /api/drift/events/{id} and updates UI
 *   - Dismiss without reason: button disabled
 *   - Run drift now: POSTs /api/drift/run and shows the enqueued message
 *   - Heartbeat panel renders baselines / events_open from /api/drift/status
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest"

import { DriftClient } from "@/components/drift-client"

interface DriftEvent {
  id: string
  user_id: string
  control_id: string
  event_type: "status_changed" | "config_changed" | "evidence_removed"
  what_changed: string
  previous_value: Record<string, unknown>
  current_value: Record<string, unknown>
  suggested_fix: string
  source_link: string | null
  severity: "low" | "medium" | "high"
  detected_at: string
  status: "open" | "resolved" | "dismissed"
  content_hash: string
}

const baseEvent: DriftEvent = {
  id: "evt-1",
  user_id: "user_abc",
  control_id: "CC6.1",
  event_type: "config_changed",
  what_changed: "removed: enforcement",
  previous_value: { enforcement: "active" },
  current_value: {},
  suggested_fix: "Re-enable branch protection.",
  source_link: "https://github.com/example/repo/settings/branches",
  severity: "high",
  detected_at: "2026-05-09T00:00:00Z",
  status: "open",
  content_hash: "deadbeef",
}

interface MockResponse {
  ok: boolean
  status?: number
  json: () => Promise<unknown>
}

/**
 * URL-routed fetch mock so the parallel /api/drift/events + /api/drift/status
 * calls (and the polling loop in runDrift) all get correct responses without
 * order-dependent mockResolvedValueOnce chains.
 */
function urlRouter(routes: Record<string, MockResponse | (() => MockResponse)>) {
  return vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const u = typeof url === "string" ? url : url.toString()
    // PATCH/POST routes are matched first by exact URL prefix.
    if (init?.method === "PATCH" || init?.method === "POST") {
      const route = routes[`${init.method} ${u}`]
      if (route) return typeof route === "function" ? route() : route
    }
    // GET routes (default) — match longest prefix first.
    const matches = Object.keys(routes)
      .filter((k) => !k.includes(" "))
      .filter((k) => u.startsWith(k))
      .sort((a, b) => b.length - a.length)
    if (matches.length > 0) {
      const route = routes[matches[0]]
      return typeof route === "function" ? route() : route
    }
    return { ok: false, status: 404, json: async () => ({ detail: "no mock" }) }
  })
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe("DriftClient", () => {
  it("renders empty state when API returns no events", async () => {
    vi.stubGlobal(
      "fetch",
      urlRouter({
        "/api/drift/events": {
          ok: true,
          json: async () => ({ events: [], count: 0 }),
        },
        "/api/drift/status": {
          ok: true,
          json: async () => ({
            baselines: 0,
            last_scan_at: null,
            events_total: 0,
            events_open: 0,
          }),
        },
      }),
    )
    render(<DriftClient />)
    await waitFor(() => {
      expect(screen.getByText(/No drift events/i)).toBeInTheDocument()
    })
  })

  it("renders one DriftEventCard per event with severity badge", async () => {
    vi.stubGlobal(
      "fetch",
      urlRouter({
        "/api/drift/events": {
          ok: true,
          json: async () => ({ events: [baseEvent], count: 1 }),
        },
        "/api/drift/status": {
          ok: true,
          json: async () => ({
            baselines: 1,
            last_scan_at: "2026-05-09T00:00:00Z",
            events_total: 1,
            events_open: 1,
          }),
        },
      }),
    )
    render(<DriftClient />)
    await waitFor(() => {
      expect(screen.getByTestId("drift-event-card")).toBeInTheDocument()
    })
    expect(screen.getByText("CC6.1: Configuration changed")).toBeInTheDocument()
    expect(screen.getByText("high")).toBeInTheDocument()
    const card = screen.getByTestId("drift-event-card")
    expect(card).toHaveAttribute("data-status", "open")
  })

  it("Resolve PATCHes the API and updates UI", async () => {
    const router = urlRouter({
      "/api/drift/events": {
        ok: true,
        json: async () => ({ events: [baseEvent], count: 1 }),
      },
      "/api/drift/status": {
        ok: true,
        json: async () => ({
          baselines: 1,
          last_scan_at: "2026-05-09T00:00:00Z",
          events_total: 1,
          events_open: 1,
        }),
      },
      "PATCH /api/drift/events/evt-1": {
        ok: true,
        json: async () => ({ ...baseEvent, status: "resolved" }),
      },
    })
    vi.stubGlobal("fetch", router)
    render(<DriftClient />)
    await waitFor(() => screen.getByTestId("drift-event-resolve"))
    fireEvent.click(screen.getByTestId("drift-event-resolve"))
    await waitFor(() => {
      expect(screen.getByText("resolved")).toBeInTheDocument()
    })
    // Confirm the resolve PATCH actually fired.
    const calls = router.mock.calls.filter(
      (c) => c[1]?.method === "PATCH" && c[0] === "/api/drift/events/evt-1",
    )
    expect(calls.length).toBe(1)
  })

  it("Dismiss without reason keeps the button disabled", async () => {
    vi.stubGlobal(
      "fetch",
      urlRouter({
        "/api/drift/events": {
          ok: true,
          json: async () => ({ events: [baseEvent], count: 1 }),
        },
        "/api/drift/status": {
          ok: true,
          json: async () => ({
            baselines: 1,
            last_scan_at: null,
            events_total: 1,
            events_open: 1,
          }),
        },
      }),
    )
    render(<DriftClient />)
    await waitFor(() => screen.getByTestId("drift-event-dismiss"))
    const dismiss = screen.getByTestId("drift-event-dismiss") as HTMLButtonElement
    expect(dismiss.disabled).toBe(true)
  })

  it("Run drift now POSTs and shows enqueued message", async () => {
    const router = urlRouter({
      "/api/drift/events": {
        ok: true,
        json: async () => ({ events: [], count: 0 }),
      },
      "/api/drift/status": {
        ok: true,
        json: async () => ({
          baselines: 0,
          last_scan_at: null,
          events_total: 0,
          events_open: 0,
        }),
      },
      "POST /api/drift/run": {
        ok: true,
        json: async () => ({ enqueued: 1, triggered_by: "user" }),
      },
    })
    vi.stubGlobal("fetch", router)
    render(<DriftClient />)
    await waitFor(() => screen.getByTestId("drift-button-run"))
    fireEvent.click(screen.getByTestId("drift-button-run"))
    await waitFor(() => {
      expect(screen.getByTestId("drift-run-message").textContent).toMatch(
        /Started a drift scan/i,
      )
    })
    const postCalls = router.mock.calls.filter(
      (c) => c[1]?.method === "POST" && c[0] === "/api/drift/run",
    )
    expect(postCalls.length).toBe(1)
  })

  it("shows a clear message when the click was deduplicated", async () => {
    const router = urlRouter({
      "/api/drift/events": {
        ok: true,
        json: async () => ({ events: [], count: 0 }),
      },
      "/api/drift/status": {
        ok: true,
        json: async () => ({
          baselines: 19,
          last_scan_at: "2026-05-09T22:00:00Z",
          events_total: 0,
          events_open: 0,
        }),
      },
      "POST /api/drift/run": {
        ok: true,
        json: async () => ({
          enqueued: 0,
          deduplicated: 1,
          triggered_by: "user",
        }),
      },
    })
    vi.stubGlobal("fetch", router)
    render(<DriftClient />)
    await waitFor(() => screen.getByTestId("drift-button-run"))
    fireEvent.click(screen.getByTestId("drift-button-run"))
    await waitFor(() => {
      expect(screen.getByTestId("drift-run-message").textContent).toMatch(
        /started in the same minute/i,
      )
    })
  })

  // Post-Sprint-9 heartbeat tests
  it("renders heartbeat panel with baselines + open events count", async () => {
    vi.stubGlobal(
      "fetch",
      urlRouter({
        "/api/drift/events": {
          ok: true,
          json: async () => ({ events: [], count: 0 }),
        },
        "/api/drift/status": {
          ok: true,
          json: async () => ({
            baselines: 76,
            last_scan_at: new Date(Date.now() - 60_000).toISOString(),
            events_total: 0,
            events_open: 0,
          }),
        },
      }),
    )
    render(<DriftClient />)
    await waitFor(() => screen.getByTestId("drift-status-heartbeat"))
    expect(screen.getByTestId("drift-status-baselines").textContent).toBe("76")
    expect(screen.getByTestId("drift-status-events-open").textContent).toBe("0")
    // Should mention "scanned 76 controls"
    expect(screen.getByTestId("drift-status-heartbeat").textContent).toMatch(
      /scanned/i,
    )
  })

  it("renders 'no baselines yet' heartbeat copy on a fresh DB", async () => {
    vi.stubGlobal(
      "fetch",
      urlRouter({
        "/api/drift/events": {
          ok: true,
          json: async () => ({ events: [], count: 0 }),
        },
        "/api/drift/status": {
          ok: true,
          json: async () => ({
            baselines: 0,
            last_scan_at: null,
            events_total: 0,
            events_open: 0,
          }),
        },
      }),
    )
    render(<DriftClient />)
    await waitFor(() => screen.getByTestId("drift-status-heartbeat"))
    expect(
      screen.getByTestId("drift-status-heartbeat").textContent,
    ).toMatch(/No baselines recorded yet/i)
  })
})
