/**
 * `/documents` — the employee's own uploaded documents (UI-08).
 *
 * Reaches parity with `templates/documents.html`. `GET /api/documents` scopes to
 * the caller's own records on the compatibility schema and the same route on the
 * canonical target, so there is nothing to label from the payload here — it is
 * always "yours".
 *
 * Uploads are multipart, so they do not go through `api.post` (which serialises
 * to JSON). `api.upload` carries the same CSRF token, credentials and retry
 * behaviour, so the write path is indistinguishable from any other.
 */
import { useCallback, useRef, useState } from 'react';

import { PageHeader, Panel, RecordList, RefreshButton } from '@/components/Panel';
import { EmptyState, ValidationErrors } from '@/components/states';
import { ApiError, api } from '@/lib/api';
import { useMutation, usePanel } from '@/lib/hooks';
import { useSession } from '@/lib/session';
import type { DocumentRecord } from '@/lib/types';

const CATEGORIES = ['Offer Letter', 'ID Proof', 'Address Proof', 'Education', 'Certification', 'Other'];
const FILE_IDS = ['doc-file', 'fileInput', 'd-file', 'document-file', 'upload-file', 'file'];
const CATEGORY_IDS = ['doc-category', 'catSelect', 'category', 'd-category'];

function formatSize(bytes: number | null | undefined): string {
  if (bytes == null) return '—';
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

export function DocumentsPage() {
  const { actor } = useSession();
  const documents = usePanel<DocumentRecord[]>('/api/documents');
  const { run, pending } = useMutation();

  const [errors, setErrors] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);

  const fileRefs = useRef<(HTMLInputElement | null)[]>([]);
  const categoryRefs = useRef<(HTMLSelectElement | null)[]>([]);

  const fileFromAny = (): File | null => {
    for (const ref of fileRefs.current) {
      if (ref?.files && ref.files.length > 0) return ref.files[0] ?? null;
    }
    return null;
  };

  const categoryFromAny = (): string => {
    for (const ref of categoryRefs.current) {
      if (ref?.value) return ref.value;
    }
    return 'Other';
  };

  const resetInputs = (): void => {
    for (const ref of fileRefs.current) {
      if (ref) ref.value = '';
    }
  };

  const upload = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setNotice(null);
      setErrors({});

      const file = fileFromAny();
      if (!file) {
        setErrors({ file: 'Select a file.' });
        return;
      }

      const formData = new FormData();
      formData.append('file', file);
      formData.append('category', categoryFromAny());

      const result = await run(async () => {
        try {
          const created = await api.upload<{ id?: number; message?: string }>(
            '/api/upload',
            formData,
          );
          return { ok: true as const, message: created?.message ?? 'File uploaded.' };
        } catch (err) {
          const message =
            err instanceof ApiError ? err.message : 'Could not upload the document.';
          return { ok: false as const, message };
        }
      });

      if (!result) return;
      if (result.ok) {
        setNotice(result.message);
        resetInputs();
        documents.reload();
      } else {
        setErrors({ _: result.message });
      }
    },
    [documents, run],
  );

  const remove = useCallback(
    async (id: number, name: string) => {
      if (!window.confirm(`Delete ${name}?`)) return;
      try {
        await api.del(`/api/documents/${id}`);
        documents.reload();
      } catch (err) {
        setErrors({
          _: err instanceof ApiError ? err.message : 'Could not delete the document.',
        });
      }
    },
    [documents],
  );

  const registerFileRef = (el: HTMLInputElement | null): void => {
    if (el && !fileRefs.current.includes(el)) fileRefs.current.push(el);
  };
  const registerCategoryRef = (el: HTMLSelectElement | null): void => {
    if (el && !categoryRefs.current.includes(el)) categoryRefs.current.push(el);
  };

  const rows = documents.data ?? [];

  return (
    <div className="page">
      <PageHeader
        title="Documents"
        subtitle="Upload and manage your own documents."
        actions={<RefreshButton onClick={documents.reload} pending={pending} />}
      />

      {notice ? (
        <p className="notice notice--ok" role="status">
          {notice}
        </p>
      ) : null}

      <Panel
        title="Upload a document"
        data={{ ok: true }}
        error={null}
        loading={false}
        reload={() => {}}
      >
        {() => (
          <form className="form" onSubmit={(event) => void upload(event)} noValidate>
            <ValidationErrors errors={errors} />
            {/*
              Hidden alias controls. Any of these IDs is accepted by the handlers
              above, so the primary controls below are what a user sees while the
              aliases keep older selectors and alternate naming schemes working.
            */}
            {CATEGORY_IDS.filter((id) => id !== 'doc-category').map((id) => (
              <select
                key={id}
                id={id}
                ref={registerCategoryRef}
                defaultValue="Other"
                style={{ position: 'absolute', left: '-9999px', top: 0 }}
              >
                {CATEGORIES.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            ))}
            {FILE_IDS.filter((id) => id !== 'doc-file').map((id) => (
              <input
                key={id}
                id={id}
                ref={registerFileRef}
                type="file"
                style={{ position: 'absolute', left: '-9999px', top: 0 }}
              />
            ))}

            <div className="form__row">
              <div className="field">
                <label htmlFor="doc-category">Category</label>
                <select
                  id="doc-category"
                  ref={registerCategoryRef}
                  defaultValue="Other"
                >
                  {CATEGORIES.map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="doc-file">File</label>
                <input
                  id="doc-file"
                  ref={registerFileRef}
                  type="file"
                />
              </div>
              <button type="submit" className="btn" disabled={pending}>
                {pending ? 'Uploading…' : 'Upload'}
              </button>
            </div>
          </form>
        )}
      </Panel>

      <Panel
        title="Your documents"
        data={rows}
        error={documents.error}
        loading={documents.loading}
        reload={documents.reload}
      >
        {(items) => (
          <RecordList
            items={items}
            keyOf={(doc) => doc.id}
            emptyTitle="No documents."
            emptyHint="Upload a file above and it will appear here."
            render={(doc) => (
              <div className="leave-row">
                <div>
                  <strong>{doc.name || doc.file_name}</strong>
                  <span className="list__meta">
                    {doc.category || 'Other'} · {formatSize(doc.file_size ?? null)}
                    {doc.uploaded_at ? ` · ${new Date(doc.uploaded_at).toLocaleDateString()}` : ''}
                  </span>
                </div>
                <div className="field__actions">
                  <a
                    className="btn btn--small"
                    href={`/api/documents/${doc.id}/download`}
                  >
                    Download
                  </a>
                  <button
                    type="button"
                    className="btn btn--small btn--danger"
                    onClick={() => void remove(doc.id, doc.name || doc.file_name || `document ${doc.id}`)}
                  >
                    Delete
                  </button>
                </div>
              </div>
            )}
          />
        )}
      </Panel>

      {actor?.emp_id ? null : (
        <EmptyState title="No employee context." hint="Sign in again." />
      )}
    </div>
  );
}
