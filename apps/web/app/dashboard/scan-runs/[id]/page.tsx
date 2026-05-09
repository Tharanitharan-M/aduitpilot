import Link from "next/link";

import { PageHeader } from "@/components/page-header";

export const metadata = {
  title: "Scan run detail — AuditPilot",
};

interface ScanRunDetailPageProps {
  params: Promise<{ id: string }>;
}

export default async function ScanRunDetailPage({ params }: ScanRunDetailPageProps) {
  const { id } = await params;
  return (
    <>
      <PageHeader
        title="Scan run detail"
        description="Detail and lineage for a single readiness scan run."
        breadcrumbExtra={{ label: id.slice(0, 8) + "…" }}
      />
      <div className="space-y-3 text-sm">
        <p>
          Run id: <code className="font-mono">{id}</code>
        </p>
        <p className="text-muted-foreground">
          Compare against another run from the{" "}
          <Link href="/dashboard/scan-runs" className="underline">
            Scan runs list
          </Link>{" "}
          (pick two and click <span className="font-semibold">Compare</span>).
        </p>
      </div>
    </>
  );
}
