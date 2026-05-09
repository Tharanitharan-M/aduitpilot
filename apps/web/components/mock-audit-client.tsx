"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { z } from "zod";
import {
  AlertTriangle,
  CheckCircle2,
  Download,
  Loader2,
  ShieldAlert,
  Swords,
  XCircle,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { EmptyState } from "@/components/empty-state";
import { IslandErrorBoundary } from "@/components/error-boundary";
import { PageHeader } from "@/components/page-header";

// ── Zod schemas ─────────────────────────────────────────────────────────────

const FindingSchema = z.object({
  id: z.string(),
  run_id: z.string(),
  severity: z.enum(["low", "medium", "high", "critical"]),
  tsc_id: z.string().nullable().optional(),
  objection: z.string(),
  recommended_next_step: z.string().default(""),
  sequence_idx: z.number().default(0),
});

const RunSummarySchema = z.object({
  id: z.string(),
  user_id: z.string(),
  scan_run_id: z.string().nullable().optional(),
  status: z.enum([
    "queued",
    "dispatching",
    "running",
    "completed",
    "failed",
    "budget_exceeded",
  ]),
  summary: z.string().default(""),
  findings_count: z.number().default(0),
  severity_max: z.enum(["none", "low", "medium", "high", "critical"]).default("none"),
  spent_usd: z.number().default(0),
  cap_usd: z.number().default(0),
  a2a_task_id: z.string().nullable().optional(),
  report_r2_key: z.string().nullable().optional(),
  failure_reason: z.string().nullable().optional(),
  created_at: z.string(),
  updated_at: z.string(),
});

const RunListSchema = z.object({
  runs: z.array(RunSummarySchema),
  count: z.number(),
});

const RunDetailSchema = z.object({
  run: RunSummarySchema,
  findings: z.array(FindingSchema),
});

const StartResponseSchema = z.object({
  run_id: z.string(),
  task_id: z.string(),
  status: z.string(),
  deduplicated: z.boolean(),
});

type Finding = z.infer<typeof FindingSchema>;
type RunSummary = z.infer<typeof RunSummarySchema>;

const STATUS_LABELS: Record<RunSummary["status"], string> = {
  queued: "Queued",
  dispatching: "Dispatching",
  running: "Running",
  completed: "Completed",
  failed: "Failed",
  budget_exceeded: "Halted (budget)",
};

const STATUS_BADGE: Record<RunSummary["status"], string> = {
  queued:
    "bg-slate-100 text-slate-700 border-slate-200 dark:bg-slate-900/40 dark:text-slate-200 dark:border-slate-700",
  dispatching:
    "bg-blue-50 text-blue-700 border-blue-200 dark:bg-blue-950/40 dark:text-blue-200 dark:border-blue-800",
  running:
    "bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950/40 dark:text-amber-200 dark:border-amber-800",
  completed:
    "bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950/40 dark:text-emerald-200 dark:border-emerald-800",
  failed:
    "bg-rose-50 text-rose-700 border-rose-200 dark:bg-rose-950/40 dark:text-rose-200 dark:border-rose-800",
  budget_exceeded:
    "bg-orange-50 text-orange-700 border-orange-200 dark:bg-orange-950/40 dark:text-orange-200 dark:border-orange-800",
};

const SEVERITY_BADGE: Record<Finding["severity"], string> = {
  critical:
    "bg-rose-100 text-rose-800 border-rose-300 dark:bg-rose-950/60 dark:text-rose-200 dark:border-rose-800",
  high:
    "bg-orange-100 text-orange-800 border-orange-300 dark:bg-orange-950/60 dark:text-orange-200 dark:border-orange-800",
  medium:
    "bg-amber-50 text-amber-800 border-amber-200 dark:bg-amber-950/60 dark:text-amber-200 dark:border-amber-800",
  low:
    "bg-slate-50 text-slate-700 border-slate-200 dark:bg-slate-900/40 dark:text-slate-200 dark:border-slate-700",
};

const TERMINAL: ReadonlySet<RunSummary["status"]> = new Set([
  "completed",
  "failed",
  "budget_exceeded",
]);

// ── Component ───────────────────────────────────────────────────────────────

export function MockAuditClient() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [activeRun, setActiveRun] = useState<RunSummary | null>(null);
  const [starting, setStarting] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const pollAbort = useRef<AbortController | null>(null);
  const listAbort = useRef<AbortController | null>(null);
  // Mirror activeRun in a ref so the polling interval can read the
  // current value without being listed as a useEffect dependency
  // (which would tear down + recreate the interval on every detail
  // refresh and starve the poll cadence).
  const activeRunRef = useRef<RunSummary | null>(null);

  // ── Initial list load ────────────────────────────────────────────────────
  const refreshList = useCallback(async () => {
    listAbort.current?.abort();
    const controller = new AbortController();
    listAbort.current = controller;
    try {
      const res = await fetch("/api/mock-audit", { signal: controller.signal });
      if (!res.ok) {
        if (res.status === 401) return;
        throw new Error(`List failed: ${res.status}`);
      }
      const parsed = RunListSchema.safeParse(await res.json());
      if (!parsed.success) {
        setErrorMsg("Unexpected list response — schema mismatch");
        return;
      }
      setRuns(parsed.data.runs);
      if (parsed.data.runs.length > 0 && activeRunId === null) {
        setActiveRunId(parsed.data.runs[0].id);
      }
    } catch (err) {
      if (err instanceof Error && err.name !== "AbortError") {
        setErrorMsg(err.message);
      }
    }
  }, [activeRunId]);

  useEffect(() => {
    refreshList();
    // Refresh list periodically while a non-terminal run exists.
    const id = setInterval(refreshList, 4000);
    return () => {
      clearInterval(id);
      listAbort.current?.abort();
    };
  }, [refreshList]);

  // ── Detail polling for the active run ───────────────────────────────────
  const fetchDetail = useCallback(async (runId: string) => {
    const controller = new AbortController();
    pollAbort.current?.abort();
    pollAbort.current = controller;
    try {
      const res = await fetch(`/api/mock-audit/${runId}`, {
        signal: controller.signal,
      });
      if (!res.ok) {
        if (res.status === 404) {
          setErrorMsg("Run not found");
          return;
        }
        throw new Error(`Detail failed: ${res.status}`);
      }
      const parsed = RunDetailSchema.safeParse(await res.json());
      if (!parsed.success) {
        setErrorMsg("Unexpected detail response — schema mismatch");
        return;
      }
      setActiveRun(parsed.data.run);
      activeRunRef.current = parsed.data.run;
      setFindings(parsed.data.findings);
    } catch (err) {
      if (err instanceof Error && err.name !== "AbortError") {
        setErrorMsg(err.message);
      }
    }
  }, []);

  useEffect(() => {
    if (!activeRunId) {
      setActiveRun(null);
      activeRunRef.current = null;
      setFindings([]);
      return;
    }
    fetchDetail(activeRunId);
    // Poll detail every 3s until run reaches a terminal state.
    const id = setInterval(() => {
      const current = activeRunRef.current;
      if (current && TERMINAL.has(current.status)) return;
      fetchDetail(activeRunId);
    }, 3000);
    return () => {
      clearInterval(id);
      pollAbort.current?.abort();
    };
  }, [activeRunId, fetchDetail]);

  // ── Start a new run ──────────────────────────────────────────────────────
  const startRun = useCallback(async () => {
    setStarting(true);
    setErrorMsg(null);
    try {
      const res = await fetch("/api/mock-audit/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!res.ok) {
        if (res.status === 401) {
          setErrorMsg("Sign in required");
          return;
        }
        if (res.status === 429) {
          setErrorMsg("Too many runs — wait a minute and try again");
          return;
        }
        throw new Error(`Start failed: ${res.status}`);
      }
      const parsed = StartResponseSchema.safeParse(await res.json());
      if (!parsed.success) {
        setErrorMsg("Invalid start response");
        return;
      }
      setActiveRunId(parsed.data.run_id);
      await refreshList();
    } catch (err) {
      if (err instanceof Error) setErrorMsg(err.message);
    } finally {
      setStarting(false);
    }
  }, [refreshList]);

  return (
    <IslandErrorBoundary name="MockAuditClient">
      <div className="flex flex-col gap-4">
        <PageHeader
          title="Mock readiness challenge"
          description="Internal adversarial pass over your draft readiness assessment. Findings are draft suggestions for human review."
        />
        <div className="flex justify-end">
          <Button onClick={startRun} disabled={starting}>
            {starting ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <Swords className="mr-2 h-4 w-4" />
            )}
            Run mock readiness challenge
          </Button>
        </div>
        {errorMsg ? (
          <Card className="border-rose-300 bg-rose-50/40 dark:bg-rose-950/20">
            <CardContent className="py-3 text-sm text-rose-700 dark:text-rose-200">
              {errorMsg}
            </CardContent>
          </Card>
        ) : null}
        <div className="grid gap-4 lg:grid-cols-[280px_1fr]">
          <RunList
            runs={runs}
            activeRunId={activeRunId}
            onSelect={setActiveRunId}
          />
          <RunDetail run={activeRun} findings={findings} />
        </div>
      </div>
    </IslandErrorBoundary>
  );
}

// ── Subcomponents ───────────────────────────────────────────────────────────

function RunList({
  runs,
  activeRunId,
  onSelect,
}: {
  runs: RunSummary[];
  activeRunId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">Past runs</CardTitle>
      </CardHeader>
      <CardContent className="space-y-1 px-2">
        {runs.length === 0 ? (
          <p className="px-2 py-4 text-sm text-muted-foreground">
            No runs yet. Click &ldquo;Run mock readiness challenge&rdquo; to start.
          </p>
        ) : (
          runs.map((run) => (
            <button
              key={run.id}
              onClick={() => onSelect(run.id)}
              className={`flex w-full items-start justify-between rounded-md px-2 py-2 text-left text-sm transition-colors hover:bg-muted/60 ${
                run.id === activeRunId ? "bg-muted" : ""
              }`}
            >
              <div className="flex flex-col">
                <span className="font-medium">
                  {new Date(run.created_at).toLocaleString()}
                </span>
                <span className="text-xs text-muted-foreground">
                  {run.findings_count} finding{run.findings_count === 1 ? "" : "s"}
                </span>
              </div>
              <Badge
                variant="outline"
                className={STATUS_BADGE[run.status]}
              >
                {STATUS_LABELS[run.status]}
              </Badge>
            </button>
          ))
        )}
      </CardContent>
    </Card>
  );
}

function RunDetail({
  run,
  findings,
}: {
  run: RunSummary | null;
  findings: Finding[];
}) {
  if (!run) {
    return (
      <Card>
        <CardContent className="py-12">
          <EmptyState
            icon={Swords}
            title="No run selected"
            description="Pick a run on the left or start a new mock readiness challenge."
          />
        </CardContent>
      </Card>
    );
  }

  const isTerminal = TERMINAL.has(run.status);
  const reportReady = run.status === "completed" && run.report_r2_key;

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-4">
        <div>
          <CardTitle className="text-base">
            Run {run.id.slice(0, 8)}
          </CardTitle>
          <p className="mt-1 text-xs text-muted-foreground">
            Started {new Date(run.created_at).toLocaleString()}
            {" • "}
            Budget ${run.spent_usd.toFixed(4)} / ${run.cap_usd.toFixed(4)}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant="outline" className={STATUS_BADGE[run.status]}>
            {STATUS_LABELS[run.status]}
          </Badge>
          {reportReady ? (
            <a
              href={`/api/mock-audit/${run.id}/report`}
              download={`mock-audit-${run.id}.md`}
              aria-label="Download Markdown gap report"
              className={cn(buttonVariants({ size: "sm", variant: "outline" }))}
            >
              <Download className="mr-2 h-4 w-4" />
              Gap report
            </a>
          ) : null}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {!isTerminal ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            AdversarialAuditor is challenging your draft assessment...
          </p>
        ) : null}
        {run.failure_reason ? (
          <div className="flex items-start gap-2 rounded-md border border-rose-200 bg-rose-50/40 p-3 text-sm text-rose-700 dark:border-rose-800 dark:bg-rose-950/30 dark:text-rose-200">
            <XCircle className="h-4 w-4 shrink-0" />
            <div>
              <p className="font-medium">Run failed</p>
              <p className="mt-1 text-xs">{run.failure_reason}</p>
            </div>
          </div>
        ) : null}
        {run.summary ? (
          <div className="rounded-md border border-border/60 bg-muted/40 p-3 text-sm">
            <p className="mb-1 flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
              <ShieldAlert className="h-3.5 w-3.5" />
              Adversarial summary
            </p>
            <p>{run.summary}</p>
          </div>
        ) : null}
        <FindingsList findings={findings} status={run.status} />
      </CardContent>
    </Card>
  );
}

function FindingsList({
  findings,
  status,
}: {
  findings: Finding[];
  status: RunSummary["status"];
}) {
  if (findings.length === 0) {
    if (status === "completed") {
      return (
        <div className="flex items-start gap-2 rounded-md border border-emerald-200 bg-emerald-50/40 p-3 text-sm text-emerald-700 dark:border-emerald-800 dark:bg-emerald-950/30 dark:text-emerald-200">
          <CheckCircle2 className="h-4 w-4 shrink-0" />
          <div>
            <p className="font-medium">No objections raised.</p>
            <p className="text-xs">
              The adversarial pass found no defensible gaps in the current scope.
            </p>
          </div>
        </div>
      );
    }
    return null;
  }

  const sorted = [...findings].sort((a, b) => {
    const order = { critical: 0, high: 1, medium: 2, low: 3 } as const;
    return order[a.severity] - order[b.severity];
  });

  return (
    <ul className="space-y-3">
      {sorted.map((finding, idx) => (
        <li
          key={finding.id}
          className="rounded-md border border-border/60 bg-background p-3"
        >
          <div className="mb-2 flex items-center gap-2">
            <Badge variant="outline" className={SEVERITY_BADGE[finding.severity]}>
              <AlertTriangle className="mr-1 h-3 w-3" />
              {finding.severity.toUpperCase()}
            </Badge>
            <span className="text-xs font-mono text-muted-foreground">
              {finding.tsc_id ?? "—"}
            </span>
            <span className="ml-auto text-xs text-muted-foreground">#{idx + 1}</span>
          </div>
          <p className="text-sm">{finding.objection}</p>
          {finding.recommended_next_step ? (
            <p className="mt-2 text-xs text-muted-foreground">
              <span className="font-medium">Next step:</span>{" "}
              {finding.recommended_next_step}
            </p>
          ) : null}
        </li>
      ))}
    </ul>
  );
}
