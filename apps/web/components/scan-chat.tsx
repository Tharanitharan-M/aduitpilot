"use client"

/**
 * ScanChat — AI SDK 6 useChat client island for the dashboard.
 *
 * Renders the readiness-scan chat surface: user bubbles (right-aligned),
 * assistant text bubbles (left-aligned), and tool invocations as <ToolCard>.
 * Connects to the Next.js proxy at /api/chat which forwards to FastAPI /chat.
 *
 * Props:
 *   connectorId      — Clerk external-account id (eac_*). Forwarded to backend.
 *   repoIncludeList  — provider_repo_id strings from the scoped-repos picker.
 *                      When empty, the "Run readiness scan" button is disabled
 *                      and a CTA links to the scope-picker page.
 *
 * Refs: PLAN.md chunks 4.1, 4.2; US-010; ADR-0003.
 */

import { useRef, useEffect } from "react"
import Link from "next/link"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { ToolCard } from "@/components/tool-card"
import {
  useScanStream,
  type DynamicToolPart,
  type ScanMessage,
  type UseScanStreamReturn,
} from "@/lib/use-scan-stream"

interface ScanChatProps {
  connectorId: string
  repoIncludeList: string[]
  /**
   * Sprint 5 — when supplied, the chat uses an externally-owned stream
   * (so siblings like the Control Posture grid render off the same
   * single source of truth). When omitted, the component falls back to
   * its self-contained Sprint 4 behaviour and owns its own hook.
   */
  stream?: UseScanStreamReturn
}

export function ScanChat({
  connectorId,
  repoIncludeList,
  stream,
}: ScanChatProps) {
  const hasScope = repoIncludeList.length > 0
  const scrollRef = useRef<HTMLDivElement>(null)

  const ownStream = useScanStream({
    api: "/api/chat",
    body: {
      intent: "run_readiness_scan",
      repo_include_list: repoIncludeList,
      connector_id: connectorId,
    },
  })

  const {
    messages,
    input,
    handleInputChange,
    handleSubmit,
    status,
    append,
    error,
  } = stream ?? ownStream

  const isStreaming = status === "submitted" || status === "streaming"

  // Auto-scroll to bottom on new chunks.
  useEffect(() => {
    const el = scrollRef.current
    if (el) {
      el.scrollTop = el.scrollHeight
    }
  }, [messages])

  function handleScanClick() {
    if (!hasScope || isStreaming) return
    append({
      role: "user",
      content: "Run readiness scan on the scoped repositories.",
    })
  }

  return (
    <section aria-label="Readiness scan chat" className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Readiness Scan</h2>

        {hasScope ? (
          <Button
            size="sm"
            onClick={handleScanClick}
            disabled={isStreaming}
            aria-label="Run readiness scan"
          >
            {isStreaming ? "Scanning…" : "Run readiness scan"}
          </Button>
        ) : (
          <div className="flex items-center gap-2">
            <Button size="sm" disabled aria-label="Run readiness scan">
              Run readiness scan
            </Button>
            <p className="text-xs text-muted-foreground">
              No repos scoped.{" "}
              <Link
                href={`/dashboard/connectors/${connectorId}/scope`}
                className="underline underline-offset-2 hover:text-foreground"
              >
                Configure scope →
              </Link>
            </p>
          </div>
        )}
      </div>

      {/* Visible error surface — shown when the proxy or stream fails.
          Sprint 4 chunk 4.14 — ``aria-live="assertive"`` interrupts the
          screen reader, ``aria-atomic`` reads the whole message, and the
          existing ``role="alert"`` keeps the assertive politeness without
          needing a redundant value. */}
      {error && (
        <div
          role="alert"
          aria-live="assertive"
          aria-atomic="true"
          data-testid="scan-chat-error"
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          {error.message}
        </div>
      )}

      {/* Message thread.
          Sprint 4 chunk 4.14 — ``aria-live="polite"`` so screen readers
          announce new assistant turns without interrupting whatever the
          user is reading. ``aria-busy`` flips during the stream so AT
          knows updates are in progress and can wait for the quiet
          moment before reading the final message. */}
      <div
        ref={scrollRef}
        role="log"
        aria-live="polite"
        aria-relevant="additions text"
        aria-busy={isStreaming}
        className="flex max-h-[60vh] min-h-[200px] flex-col gap-3 overflow-y-auto rounded-xl border bg-card p-4"
        aria-label="Chat messages"
      >
        {messages.length === 0 && !isStreaming && (
          <p className="m-auto text-sm text-muted-foreground">
            {hasScope
              ? 'Click "Run readiness scan" to start, or type a question below.'
              : "Configure a repo scope to enable the readiness scan."}
          </p>
        )}

        {messages.map((msg) => (
          <MessageRow key={msg.id} message={msg} />
        ))}

        {isStreaming && messages.length === 0 && (
          <div
            role="status"
            aria-label="Streaming readiness scan response"
            className="space-y-2"
          >
            <Skeleton className="h-4 w-3/4" />
            <Skeleton className="h-4 w-1/2" />
          </div>
        )}
      </div>

      {/* Free-text input */}
      <form
        onSubmit={handleSubmit}
        className="flex gap-2"
        aria-label="Chat input"
      >
        <Input
          value={input}
          onChange={handleInputChange}
          placeholder="Ask a question about your readiness…"
          disabled={isStreaming}
          aria-label="Chat message input"
        />
        <Button type="submit" size="sm" disabled={isStreaming || !input.trim()}>
          Send
        </Button>
      </form>
    </section>
  )
}

// ── MessageRow ────────────────────────────────────────────────────────────────

// useScanStream emits a discriminated union of TextPart | DynamicToolPart so
// the duck-type guards below resolve without `as unknown` widening.
function MessageRow({ message }: { message: ScanMessage }) {
  const isUser = message.role === "user"

  if (isUser) {
    const text =
      message.parts
        .filter((p): p is { type: "text"; text: string } => p.type === "text")
        .map((p) => p.text)
        .join("") || message.content

    return (
      <div className="flex justify-end">
        <div className="max-w-[75%] rounded-xl rounded-tr-sm bg-primary px-3 py-2 text-sm text-primary-foreground">
          {text}
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-2">
      {message.parts.map((part, i) => {
        if (part.type === "text") {
          if (!part.text) return null
          return (
            <div
              key={`${message.id}-text-${i}`}
              className="max-w-[85%] rounded-xl rounded-tl-sm bg-muted px-3 py-2 text-sm"
            >
              {part.text}
            </div>
          )
        }

        if (part.type === "dynamic-tool") {
          // typescript-reviewer H-4 — toolCallId is stable across the
          // pending → success/failure transitions, so React reconciles
          // the existing ToolCard instead of unmounting + remounting it.
          const toolPart: DynamicToolPart = part
          return <ToolCard key={toolPart.toolCallId} part={toolPart} />
        }

        return null
      })}
    </div>
  )
}
