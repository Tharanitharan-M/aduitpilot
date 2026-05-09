import { DriftClient } from "@/components/drift-client";
import { PageHeader } from "@/components/page-header";

export const metadata = {
  title: "Drift watcher — AuditPilot",
};

export default function DriftPage() {
  return (
    <>
      <PageHeader
        title="Compliance drift"
        description="Surface real configuration changes since the last readiness scan. The watcher runs every six hours via Vercel Cron and uses 2-scan flap protection so it never alarms on transient blips."
      />
      <DriftClient />
    </>
  );
}
