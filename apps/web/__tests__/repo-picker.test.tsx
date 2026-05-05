/**
 * RepoPicker — unit tests for the picker UX (Sprint 3.5 chunk 3.5.2).
 *
 * Mocks: next/navigation (useRouter) and global fetch.
 *
 * Refs: PLAN.md Sprint 3.5 chunk 3.5.2; ADR-0015; US-002.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, it, expect, vi, beforeEach } from "vitest"
import { RepoPicker, type PickerRepo } from "@/components/repo-picker"

const mockPush = vi.fn()
const mockRefresh = vi.fn()
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush, refresh: mockRefresh }),
}))

const REPOS: PickerRepo[] = [
  { provider_repo_id: "111", full_name: "acme/orders-api", private: true },
  { provider_repo_id: "222", full_name: "acme/auth-service", private: true },
  { provider_repo_id: "333", full_name: "acme/marketing-site", private: false },
]

describe("RepoPicker", () => {
  beforeEach(() => {
    mockPush.mockReset()
    mockRefresh.mockReset()
  })

  it("renders one row per repo with default-deny selection (nothing pre-checked)", () => {
    render(
      <RepoPicker
        connectorId="eac_test"
        repos={REPOS}
        initialSelectedIds={[]}
      />
    )
    const rows = screen.getAllByTestId("repo-row")
    expect(rows).toHaveLength(3)
    rows.forEach((row) => {
      expect(row).toHaveAttribute("data-selected", "false")
    })
    // The default-deny copy is visible.
    expect(screen.getByText(/pick at least one repo to scan/i)).toBeInTheDocument()
  })

  it("pre-checks the rows that match initialSelectedIds", () => {
    render(
      <RepoPicker
        connectorId="eac_test"
        repos={REPOS}
        initialSelectedIds={["111", "333"]}
      />
    )
    const rows = screen.getAllByTestId("repo-row")
    const selectedFlags = rows.map((r) => r.getAttribute("data-selected"))
    expect(selectedFlags).toEqual(["true", "false", "true"])
  })

  it("filters rows by the search query", () => {
    render(
      <RepoPicker
        connectorId="eac_test"
        repos={REPOS}
        initialSelectedIds={[]}
      />
    )
    const search = screen.getByLabelText("Search repos")
    fireEvent.change(search, { target: { value: "auth" } })
    const rows = screen.getAllByTestId("repo-row")
    expect(rows).toHaveLength(1)
    expect(rows[0]).toHaveTextContent("acme/auth-service")
  })

  it("submits the FULL desired selection on save and routes back to /dashboard", async () => {
    const user = userEvent.setup()
    const fetchSpy = vi
      .spyOn(global, "fetch" as never)
      .mockResolvedValue({
        ok: true,
        json: async () => ({ connector_id: "eac_test", repos: [], count: 2 }),
      } as never)

    render(
      <RepoPicker
        connectorId="eac_test"
        repos={REPOS}
        initialSelectedIds={[]}
      />
    )
    // Tick the first two rows.
    const checkboxes = screen.getAllByRole("checkbox")
    await user.click(checkboxes[0])
    await user.click(checkboxes[1])

    await user.click(screen.getByRole("button", { name: /save scope/i }))

    await waitFor(() => expect(fetchSpy).toHaveBeenCalledOnce())
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toBe("/api/connectors/eac_test/scoped-repos")
    expect(init.method).toBe("PATCH")
    const body = JSON.parse(init.body as string)
    expect(body.repos).toHaveLength(2)
    expect(body.repos.map((r: PickerRepo) => r.full_name).sort()).toEqual([
      "acme/auth-service",
      "acme/orders-api",
    ])

    await waitFor(() => expect(mockPush).toHaveBeenCalledWith("/dashboard"))
  })

  it("renders the API error message when save fails", async () => {
    const user = userEvent.setup()
    vi.spyOn(global, "fetch" as never).mockResolvedValue({
      ok: false,
      status: 422,
      json: async () => ({ detail: "Invalid body" }),
    } as never)

    render(
      <RepoPicker
        connectorId="eac_test"
        repos={REPOS}
        initialSelectedIds={["111"]}
      />
    )
    await user.click(screen.getByRole("button", { name: /save scope/i }))
    await waitFor(() =>
      expect(screen.getByTestId("picker-error")).toHaveTextContent(/invalid body/i)
    )
    expect(mockPush).not.toHaveBeenCalled()
  })
})
