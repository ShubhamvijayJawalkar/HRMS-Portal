/**
 * `/admin/reports` — attendance and break reporting (UI-09).
 */
import { useCallback, useEffect, useState } from 'react';

import { PageHeader, Panel, RecordList } from '@/components/Panel';
import { ApiError, api } from '@/lib/api';

interface ReportRow {
  emp_id: string;
  employee_name: string;
  department?: string;
  total_session_hours?: number;
  total_break_minutes?: number;
  productive_hours?: number;
  efficiency_percent?: number;
  [key: string]: unknown;
}
interface ReportResponse {
  summary?: ReportRow[];
  report_range?: { start_date: string; end_date: string };
  departments?: string[];
  employees?: { emp_id: string; name: string }[];
}
interface DeptRow {
  department: string;
  employee_count: number;
  total_hours: number;
  total_break_minutes: number;
  productive_hours: number;
  efficiency_percent: number;
  [key: string]: unknown;
}
interface DeptResponse {
  departments: DeptRow[];
}

export function AdminReportsPage() {
  const today = new Date().toISOString().slice(0, 10);
  const [startDate, setStartDate] = useState(today);
  const [endDate, setEndDate] = useState(today);
  const [report, setReport] = useState<ReportResponse | null>(null);
  const [departmentSummary, setDepartmentSummary] = useState<DeptResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [main, dept] = await Promise.all([
        api.get<ReportResponse>('/api/reports', { start_date: startDate, end_date: endDate }),
        api.get<DeptResponse>('/api/reports/department-summary', { start_date: startDate, end_date: endDate }),
      ]);
      setReport(main);
      setDepartmentSummary(dept);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not load the report.');
    } finally {
      setLoading(false);
    }
  }, [endDate, startDate]);

  useEffect(() => {
    void run();
  }, [run]);

  const formatNumber = (value: unknown): string => {
    const n = Number(value);
    return Number.isFinite(n) ? n.toFixed(2) : String(value ?? '');
  };

  return (
    <div className="page">
      <PageHeader title="Reports" subtitle="Attendance, break, and efficiency reporting." />\n      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}

      <Panel
        title="Filters"
        data={{ ok: true }}
        error={null}
        loading={false}
        reload={() => {}}
      >
        {() => (
          <div className="form">
            <div className="form__row">
              <div className="field">
                <label htmlFor="report-start">Start date</label>
                <input
                  id="report-start"
                  type="date"
                  value={startDate}
                  onChange={(e) => setStartDate(e.target.value)}
                />
              </div>
              <div className="field">
                <label htmlFor="report-end">End date</label>
                <input
                  id="report-end"
                  type="date"
                  value={endDate}
                  onChange={(e) => setEndDate(e.target.value)}
                />
              </div>
              <button type="button" className="btn" disabled={loading} onClick={() => void run()}>
                {loading ? 'Loading…' : 'Run report'}
              </button>
            </div>
          </div>
        )}
      </Panel>

      <Panel
        title="Department summary"
        data={departmentSummary?.departments ?? []}
        error={null}
        loading={loading}
        reload={() => void run()}
      >
        {(items) => (
          <RecordList
            items={items}
            keyOf={(row) => row.department}
            emptyTitle="No department rows."
            emptyHint="Try a different date range."
            render={(row) => (
              <div className="leave-row">
                <div>
                  <strong>{row.department}</strong>
                  <span className="list__meta">
                    {` · ${row.employee_count} employees · ${formatNumber(row.total_hours)} hours · ${row.total_break_minutes} break minutes · ${formatNumber(row.productive_hours)} productive · ${row.efficiency_percent}%`}
                  </span>
                </div>
              </div>
            )}
          />
        )}
      </Panel>

      <Panel
        title="Employee summary"
        data={report?.summary ?? []}
        error={null}
        loading={loading}
        reload={() => void run()}
      >
        {(items) => (
          <RecordList
            items={items}
            keyOf={(row) => row.emp_id}
            emptyTitle="No employee rows."
            emptyHint="Try a different date range."
            render={(row) => (
              <div className="leave-row">
                <div>
                  <strong>{row.employee_name}</strong>
                  <span className="list__meta">
                    {` · ${row.emp_id} · ${row.department ?? 'N/A'} · ${formatNumber(row.total_session_hours)} hours · ${row.total_break_minutes ?? 0} break minutes · ${formatNumber(row.productive_hours)} productive · ${row.efficiency_percent ?? 0}%`}
                  </span>
                </div>
              </div>
            )}
          />
        )}
      </Panel>
    </div>
  );
}