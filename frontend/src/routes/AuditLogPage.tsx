/**
 * `/admin/audit` — the read-only audit log (UI-09).
 *
 * Reaches parity with `templates/admin_audit.html`: time, actor, action, details,
 * IP, with paging. The filtering stays caller-side through the `limit`/`offset`
 * query parameters; the backend remains the source of what is recorded.
 */
import { useState } from 'react';

import { PageHeader, Panel } from '@/components/Panel';
import { EmptyState } from '@/components/states';
import { usePanel } from '@/lib/hooks';
import { useSession } from '@/lib/session';
import type { AuditLogPayload } from '@/lib/types';

const PAGE_SIZE = 50;

export function AuditLogPage() {
  useSession();
  const [offset, setOffset] = useState(0);

  const path = `/api/audit-log?limit=${PAGE_SIZE}&offset=${offset}`;
  const audit = usePanel<AuditLogPayload>(path);

  const reloadAt = (next: number) => {
    setOffset(next);
  };

  const rows = audit.data?.data ?? [];
  const total = audit.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  return (
    <div className="page">
      <PageHeader
        title="Audit log"
        subtitle={`Total: ${total} entries`}
      />

      {audit.error ? (
        <div className="notice notice--bad" role="alert">
          {audit.error.message}
        </div>
      ) : null}

      <Panel
        title="Entries"
        data={rows}
        error={null}
        loading={audit.loading}
        reload={audit.reload}
      >
        {(items) => {
          if (items.length === 0) {
            return <EmptyState title="No audit records." />;
          }
          return (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>User</th>
                    <th>Action</th>
                    <th>Details</th>
                    <th>IP</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((entry) => (
                    <tr key={entry.log_id ?? entry.created_at ?? Math.random()}>
                      <td>{entry.created_at ? new Date(entry.created_at).toLocaleString() : '—'}</td>
                      <td>{entry.emp_id || 'system'}</td>
                      <td>
                        <span className="action-badge">{entry.action}</span>
                      </td>
                      <td>{entry.details || '—'}</td>
                      <td>{entry.ip_address || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="pager">
                <button
                  type="button"
                  className="btn btn--small"
                  disabled={offset <= 0}
                  onClick={() => reloadAt(Math.max(0, offset - PAGE_SIZE))}
                >
                  ← Previous
                </button>
                <span>
                  Page {currentPage} of {pages}
                </span>
                <button
                  type="button"
                  className="btn btn--small"
                  disabled={offset + PAGE_SIZE >= total}
                  onClick={() => reloadAt(offset + PAGE_SIZE)}
                >
                  Next →
                </button>
              </div>
            </div>
          );
        }}
      </Panel>
    </div>
  );
}
