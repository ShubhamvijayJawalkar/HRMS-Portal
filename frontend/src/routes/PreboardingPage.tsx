/**
 * `/preboarding/:token` — signed-token document upload (UI-09).
 *
 * Served under `/app/preboarding/:token` in the SPA; the API still needs no
 * normal session, so the signed token is the only credential.
 */
import { useCallback, useState } from 'react';
import { useParams } from 'react-router-dom';

import { PageHeader, Panel, RecordList, StatusPill } from '@/components/Panel';
import { ApiError, api } from '@/lib/api';
import { useMutation, usePanel } from '@/lib/hooks';

const REQUIRED_DOCS = [
  'ID Proof',
  'Address Proof',
  'Education',
  'Certification',
  'Bank Details',
];

interface ChecklistItem {
  id: number;
  doc_type: string;
  status: string;
  uploaded_at?: string | null;
}
interface WorkflowSummary {
  workflow_id: number;
  emp_id: string;
  employee: string;
  checklist?: ChecklistItem[];
  [key: string]: unknown;
}

export function PreboardingPage() {
  const { token = '' } = useParams();
  const workflow = usePanel<WorkflowSummary>(token ? `/api/preboarding/${token}` : '', { enabled: Boolean(token) });
  const { run, pending } = useMutation();
  const [selectedDoc, setSelectedDoc] = useState<string>(REQUIRED_DOCS[0] as string);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const upload = useCallback(
    async () => {
      if (!token || !selectedFile || !selectedDoc) return;
      setError(null);
      try {
        const form = new FormData();
        form.append('file', selectedFile);
        await api.upload(`/api/preboarding/${token}/documents/${encodeURIComponent(selectedDoc)}`, form);
        setMessage(`Uploaded ${selectedDoc}.`);
        workflow.reload();
        setSelectedFile(null);
      } catch (err) {
        setError(err instanceof ApiError ? err.message : 'Upload failed.');
      }
    },
    [selectedDoc, selectedFile, token, workflow],
  );

  const submit = useCallback(async () => {
    if (!token) return;
    try {
      await run(async () => {
        await api.post(`/api/preboarding/${token}/submit`);
        return null;
      });
      setMessage('Pre-boarding submitted.');
      workflow.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not submit.');
    }
  }, [run, token, workflow]);

  const expected = (workflow.data?.checklist ?? []).length
    ? workflow.data!.checklist!
    : REQUIRED_DOCS.map((doc, index) => ({
        id: -index,
        doc_type: doc,
        status: 'Required',
        uploaded_at: null,
      }));

  return (
    <div className="page">
      <PageHeader title="Pre-boarding" subtitle="Upload your documents securely." />

      {message ? (
        <p className="notice notice--ok" role="status">
          {message}
        </p>
      ) : null}
      {error ? (
        <p className="notice notice--bad" role="alert">
          {error}
        </p>
      ) : null}

      {workflow.error ? (
        <p className="notice notice--bad" role="alert">
          {workflow.error.message}
        </p>
      ) : null}

      {workflow.data ? (
        <Panel
          title={`Welcome, ${workflow.data.employee}`}
          data={expected}
          error={null}
          loading={workflow.loading}
          reload={workflow.reload}
        >
          {() => (
            <div>
              <p>
                Complete the required documents below, then submit your pre-boarding.
              </p>
              <RecordList
                items={expected}
                keyOf={(item) => `${item.doc_type}-${item.id}`}
                emptyTitle="No documents required."
                render={(item) => (
                  <div className="leave-row">
                    <div>
                      <strong>{item.doc_type}</strong>
                      <span className="list__meta">
                        {item.uploaded_at ? ` · uploaded ${item.uploaded_at}` : ''}
                      </span>
                    </div>
                    <StatusPill status={item.status} />
                  </div>
                )}
              />
            </div>
          )}
        </Panel>
      ) : null}

      <Panel
        title="Upload a document"
        data={{ ok: true }}
        error={null}
        loading={false}
        reload={() => {}}
      >
        {() => (
          <div className="form">
            <div className="form__row">
              <div className="field">
                <label htmlFor="pb-type">Document type</label>
                <select
                  id="pb-type"
                  value={selectedDoc}
                  onChange={(e) => setSelectedDoc(e.target.value)}
                >
                  {REQUIRED_DOCS.map((doc) => (
                    <option key={doc} value={doc}>
                      {doc}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <input
                  type="file"
                  onChange={(e) => setSelectedFile(e.target.files?.[0] ?? null)}
                />
              </div>
              <button type="button" className="btn" disabled={pending || !selectedFile} onClick={() => void upload()}>
                {pending ? 'Uploading…' : 'Upload'}
              </button>
              <button type="button" className="btn btn--primary" onClick={() => void submit()}>
                Submit pre-boarding
              </button>
            </div>
          </div>
        )}
      </Panel>
    </div>
  );
}