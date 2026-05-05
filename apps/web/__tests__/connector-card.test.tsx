/**
 * ConnectorCard — unit tests for all three status states (chunk 3.9).
 *
 * Mocks: @clerk/nextjs (useAuth, useUser), next/navigation (useRouter).
 * Does NOT hit any network; the connect/disconnect actions are tested
 * via button rendering and click handler assertions.
 *
 * Refs: PLAN.md chunk 3.9, US-005.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach } from "vitest"
import { ConnectorCard, type Connector } from "@/components/connector-card"

// ── Mocks ──────────────────────────────────────────────────────────────────

const mockCreateExternalAccount = vi.fn()
let mockExternalAccounts: Array<{
  id: string
  provider: string
  verification?: { status: string } | null
}> = []
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: async () => "test-token" }),
  useUser: () => ({
    user: {
      createExternalAccount: mockCreateExternalAccount,
      externalAccounts: mockExternalAccounts,
    },
  }),
}))

const mockRefresh = vi.fn()
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: mockRefresh }),
}))

// ── Fixtures ───────────────────────────────────────────────────────────────

const connected: Connector = {
  id: "ext_abc123",
  provider: "github",
  status: "connected",
  last_used_at: "2026-05-01T10:00:00Z",
  error_message: null,
}

const errored: Connector = {
  id: "ext_abc456",
  provider: "github",
  status: "error",
  last_used_at: null,
  error_message: "OAuth token expired",
}

// ── Tests ──────────────────────────────────────────────────────────────────

describe("ConnectorCard", () => {
  beforeEach(() => {
    mockRefresh.mockReset()
    mockCreateExternalAccount.mockReset()
    mockExternalAccounts = []
  })

  it("renders 'Not connected' state — Connect enabled, Disconnect disabled", () => {
    render(<ConnectorCard connector={null} />)
    expect(screen.getByText("Not connected")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /^connect github$/i })).toBeEnabled()
    expect(screen.getByRole("button", { name: /disconnect/i })).toBeDisabled()
  })

  it("renders 'Connected' state — Disconnect enabled, Connect disabled, Configure-scope banner when no scope", () => {
    render(<ConnectorCard connector={connected} />)
    expect(screen.getByText("Connected")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /disconnect/i })).toBeEnabled()
    expect(screen.getByRole("button", { name: /^connect github$/i })).toBeDisabled()
    expect(screen.getByText(/last used/i)).toBeInTheDocument()
    // Sprint 3.5 chunk 3.5.4: empty-scope state shows the Configure-scope CTA.
    const banner = screen.getByTestId("configure-scope-banner")
    expect(banner).toBeInTheDocument()
    expect(banner.querySelector("a")).toHaveAttribute(
      "href",
      `/dashboard/connectors/${connected.id}/scope`
    )
  })

  it("renders 'Connected · N repos' badge + Edit-scope link when scope > 0", () => {
    render(
      <ConnectorCard
        connector={{ ...connected, scoped_repo_count: 3 }}
      />
    )
    expect(screen.getByText(/connected · 3 repos/i)).toBeInTheDocument()
    expect(screen.queryByTestId("configure-scope-banner")).not.toBeInTheDocument()
    const editLink = screen.getByTestId("edit-scope-link")
    expect(editLink).toHaveAttribute(
      "href",
      `/dashboard/connectors/${connected.id}/scope`
    )
  })

  it("renders 'Error' state — both Re-connect and Disconnect available", () => {
    // Error state implies an external account exists in Clerk → disconnect should
    // resolve via useUser fallback. Simulate by populating externalAccounts.
    mockExternalAccounts = [
      { id: "ext_abc456", provider: "github", verification: { status: "expired" } },
    ]
    render(<ConnectorCard connector={errored} />)
    expect(screen.getByText(/error.*re-authentication needed/i)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /re-connect github/i })).toBeEnabled()
    expect(screen.getByText("OAuth token expired")).toBeInTheDocument()
  })

  it("debug=true keeps both buttons enabled regardless of state", () => {
    mockExternalAccounts = [
      { id: "ext_abc999", provider: "github", verification: { status: "verified" } },
    ]
    render(<ConnectorCard connector={null} debug />)
    expect(screen.getByRole("button", { name: /^connect github$/i })).toBeEnabled()
    expect(screen.getByRole("button", { name: /disconnect/i })).toBeEnabled()
    expect(screen.getByText(/debug: raw connector payload/i)).toBeInTheDocument()
  })

  it("calls DELETE /api/connectors/:id and refreshes on disconnect", async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({ ok: true, json: async () => ({}) })

    render(<ConnectorCard connector={connected} />)
    fireEvent.click(screen.getByRole("button", { name: /disconnect/i }))

    await waitFor(() => expect(mockRefresh).toHaveBeenCalledOnce())
    expect(global.fetch).toHaveBeenCalledWith(
      `/api/connectors/ext_abc123`,
      expect.objectContaining({ method: "DELETE" })
    )
  })

  it("shows error message when disconnect API call fails", async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: false,
      json: async () => ({ detail: "Clerk error" }),
    })

    render(<ConnectorCard connector={connected} />)
    fireEvent.click(screen.getByRole("button", { name: /disconnect/i }))

    await waitFor(() =>
      expect(screen.getByText("Clerk error")).toBeInTheDocument()
    )
    expect(mockRefresh).not.toHaveBeenCalled()
  })

  it("data-testid and data-status attributes present for Playwright selection", () => {
    const { rerender } = render(<ConnectorCard connector={null} />)
    expect(screen.getByTestId("connector-card")).toHaveAttribute("data-status", "not_connected")

    rerender(<ConnectorCard connector={connected} />)
    expect(screen.getByTestId("connector-card")).toHaveAttribute("data-status", "connected")

    rerender(<ConnectorCard connector={errored} />)
    expect(screen.getByTestId("connector-card")).toHaveAttribute("data-status", "error")
  })
})
