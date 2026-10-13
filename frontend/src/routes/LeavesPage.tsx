/**
 * `/leaves` — the self-service leave page (UI-08).
 *
 * The pilot already built the balance / apply / cancel workflow as a dashboard
 * panel. This route is the same component promoted to a full page — deliberately
 * **the same component, not a copy**. Two copies of the apply form would drift,
 * and the ledger reversal logic that `leave_policy.cancel` performs (release vs
 * unconsume) is easy to describe differently in two places.
 */
import { PageHeader } from '@/components/Panel';

import { LeavePanel } from './LeavePanel';

export function LeavesPage() {
  return (
    <div className="page">
      <PageHeader
        title="Leaves"
        subtitle="Apply for leave, check your balance, and track or cancel requests."
      />
      <LeavePanel />
    </div>
  );
}