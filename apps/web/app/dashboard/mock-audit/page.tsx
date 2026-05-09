import { MockAuditClient } from "@/components/mock-audit-client";

export const metadata = {
  title: "Mock readiness challenge — AuditPilot",
};

export default function MockAuditPage() {
  return <MockAuditClient />;
}
