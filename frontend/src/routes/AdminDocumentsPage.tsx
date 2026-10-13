/**
 * `/admin/documents` — admin view of everyone's documents (UI-09).
 *
 * Reaches parity with `templates/admin_documents.html`: pick an employee, pick a
 * category, upload a file, and review / delete the resulting company-wide list.
 * `GET /api/documents` already scopes the list from the session (all rows for
 * an admin), and `POST /api/upload` accepts an `emp_id`, so there is no second
 * implementation of the access rule here.
 */
import { useCallback, useRef, useState } from 'react';

import { PageHeader, Panel, RecordList, RefreshButton } from '@/components/Panel';
import { ValidationErrors } from '@/components/states';
import { ApiError, api } from '@/lib/api';
import { useMutation, usePanel } from '@/lib/hooks';
import { useSession } from '@/lib/session';
import type { DocumentRecord } from '@/lib/types';

const CATEGORIES = ['Offer Letter', 'ID Proof', 'Address Proof', 'Education', 'Certification', 'Other'];

interface UserOption {
  emp_id: string;
  name?: string | null;
}

interface UserListPayload {
  data: UserOption[];
  total?: number;
}

function formatSize(bytes: number | null | undefined): string {
  if (bytes == null) return '—';
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

export function AdminDocumentsPage() {
  const { actor } = useSession();
  const documents = usePanel<DocumentRecord[]>('/api/documents');
  const users = usePanel<UserListPayload>(
    '/api/users?per_page=200&sort_by=emp_id&sort_dir=asc',
  );
  const { run, pending } = useMutation();

  const [errors, setErrors] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);

  const userRefs = useRef<(HTMLSelectElement | null)[]>([]);
  const categoryRefs = useRef<(HTMLSelectElement | null)[]>([]);
  const fileRefs = useRef<(HTMLInputElement | null)[]>([]);

  const options = users.data?.data ?? [];
  const rows = documents.data ?? [];

  const firstUser = options.length ? (options[0]?.emp_id ?? '') : '';

  const collectUser = (): string => {
    for (const ref of userRefs.current) if (ref?.value) return ref.value;
    return firstUser;
  };
  const collectCategory = (): string => {
    for (const ref of categoryRefs.current) if (ref?.value) return ref.value;
    return 'Other';
  };
  const collectFile = (): File | null => {
    for (const ref of fileRefs.current) {
      if (ref?.files && ref.files.length > 0) return ref.files[0] ?? null;
    }
    return null;
  };
  const resetInputs = (): void => {
    for (const ref of fileRefs.current) if (ref) ref.value = '';
  };

  const register = <T,>(list: React.MutableRefObject<(T | null)[]>) =>
    (el: T | null): void => {
      if (el && !list.current.includes(el)) list.current.push(el);
    };

  const upload = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setNotice(null);
      setErrors({});

      const file = collectFile();
      if (!file) {
        setErrors({ file: 'Select a file.' });
        return;
      }
      const formData = new FormData();
      formData.append('emp_id', collectUser());
      formData.append('file', file);
      formData.append('category', collectCategory());

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

  const rowsForDisplay = rows;

  return (
    <div className="page">
      <PageHeader
        title="Documents"
        subtitle="Upload and review documents for any employee."
        actions={<RefreshButton onClick={documents.reload} pending={false} />}
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

            <div className="form__row">
              <div className="field">
                <label htmlFor="admin-doc-employee">Employee</label>
                <select
                  id="admin-doc-employee"
                  ref={register(userRefs)}
                  defaultValue={firstUser}
                >
                  {options.map((user) => (
                    <option key={user.emp_id} value={user.emp_id}>
                      {user.emp_id} — {user.name || user.emp_id}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="admin-doc-category">Category</label>
                <select
                  id="admin-doc-category"
                  ref={register(categoryRefs)}
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
                <label htmlFor="admin-doc-file">File</label>
                <input
                  id="admin-doc-file"
                  ref={register(fileRefs)}
                  type="file"
                />
              </div>
              <button type="submit" className="btn" disabled={pending || users.loading}>
                {pending ? 'Uploading…' : 'Upload'}
              </button>
            </div>
          </form>
        )}
      </Panel>

      <Panel
        title="All documents"
        data={rowsForDisplay}
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
                    {doc.employee ? ` · ${doc.employee}` : ''}
                    {doc.emp_id && !doc.employee ? ` · ${doc.emp_id}` : ''}
                    {doc.category ? ` · ${doc.category}` : ''}
                    {formatSize(doc.file_size ?? null)}
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
                    onClick={() =>
                      void remove(doc.id, doc.name || doc.file_name || `document ${doc.id}`)
                    }
                  >
                    Delete
                  </button>
                </div>
              </div>
            )}
          />
        )}
      </Panel>

      {actor?.emp_id ? null : null}
    </div>
  );
}
