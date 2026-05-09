/**
 * ScanRunsClient — Sprint 9 chunks 9.11, 9.13.
 *
 * Covers:
 *   - Loads list, renders one card per run
 *   - Pick two runs and click Compare -> renders the diff table
 *   - Re-run prepends a new ScanRunOut row
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest"

import { ScanRunsClient } from "@/components/scan-runs-client"

const runA = {
  id: "11111111-1111-1111-1111-111111111111",
  user_id: "u",
  connector_id: "github_default",
  repo_include_list: ["1234"],
  status: "completed" as const,
  started_at: "2026-05-09T00:00:00Z",
  completed_at: "2026-05-09T00:01:00Z",
  cancelled: false,
  parent_scan_run_id: null,
}

const runB = {
  ...runA,
  id: "22222222-2222-2222-2222-222222222222",
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe("ScanRunsClient", () => {
  it("lists runs", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValueOnce({
        ok: true,
        json: async () => ({ runs: [runA, runB], count: 2 }),
      }),
    )
    render(<ScanRunsClient />)
    await waitFor(() => {
      expect(screen.getAllByTestId("scan-run-card")).toHaveLength(2)
    })
  })

  it("Pick two and Compare renders diff", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ runs: [runA, runB], count: 2 }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          a: runA.id,
          b: runB.id,
          controls_changed: [
            {
              control_id: "CC6.1",
              a_status: "passing",
              b_status: "failing",
              a_confidence: 0.85,
              b_confidence: 0.3,
              rationale_changed: true,
            },
          ],
          evidence_added: ["ev-2"],
          evidence_removed: [],
        }),
      })
    vi.stubGlobal("fetch", fetchMock)
    render(<ScanRunsClient />)
    await waitFor(() => screen.getAllByTestId("scan-run-card"))
    const picks = screen.getAllByTestId("scan-run-pick")
    fireEvent.click(picks[0])
    fireEvent.click(picks[1])
    fireEvent.click(screen.getByTestId("scan-runs-compare"))
    await waitFor(() => {
      expect(screen.getByTestId("scan-runs-diff-panel")).toBeInTheDocument()
    })
    expect(screen.getByText("CC6.1")).toBeInTheDocument()
    // Diff URL has both ids.
    const diffCall = fetchMock.mock.calls[1][0] as string
    expect(diffCall).toContain(runA.id)
    expect(diffCall).toContain(runB.id)
  })

  it("Re-run prepends new row", async () => {
    const newRun = {
      ...runA,
      id: "33333333-3333-3333-3333-333333333333",
      status: "running" as const,
      parent_scan_run_id: runA.id,
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ runs: [runA], count: 1 }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => newRun,
      })
    vi.stubGlobal("fetch", fetchMock)
    render(<ScanRunsClient />)
    await waitFor(() => screen.getByTestId("scan-run-rerun"))
    fireEvent.click(screen.getByTestId("scan-run-rerun"))
    await waitFor(() => {
      const cards = screen.getAllByTestId("scan-run-card")
      expect(cards).toHaveLength(2)
    })
    expect(screen.getByTestId("scan-run-rerun-badge")).toBeInTheDocument()
  })
})
