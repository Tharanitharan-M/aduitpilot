import { ScanRunsClient } from "@/components/scan-runs-client";
import { PageHeader } from "@/components/page-header";

export const metadata = {
  title: "Scan runs — AuditPilot",
};

export default function ScanRunsPage() {
  return (
    <>
      <PageHeader
        title="Scan runs"
        description="Past readiness scans. Re-run any past scan with the user's current scope, or pick two and compare them side by side."
      />
      <ScanRunsClient />
    </>
  );
}
