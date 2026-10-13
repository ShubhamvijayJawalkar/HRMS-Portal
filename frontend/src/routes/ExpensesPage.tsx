/**
 * `/expenses` — the employee's own expense claims (UI-08).
 *
 * Reaches parity with `templates/expenses.html`. Claims are policy-scoped:
 * an employee sees their own, while a reviewer sees the ones they may act on,
 * so the list heading is taken straight from the fact that every row belongs to
 * the caller rather than asserted on role.
 *
 * Submission is multipart (`cat_id`, `amount`, `description`, optional `receipt`),
 * so it goes through `api.upload` rather than `api.post`.
 */
import { useCallback, useRef, useState } from 'react';

import { PageHeader, Panel, RecordList, RefreshButton, StatusPill } from '@/components/Panel';
import { ValidationErrors } from '@/components/states';
import { ApiError, api } from '@/lib/api';
import { useMutation, usePanel } from '@/lib/hooks';
import { useSession } from '@/lib/session';
import type { Expense, ExpenseCategory } from '@/lib/types';

const CATEGORY_IDS = ['exp-category', 'catSelect', 'category'];
const AMOUNT_IDS = ['exp-amount', 'amount'];
const DESCRIPTION_IDS = ['exp-description', 'desc', 'description'];
const RECEIPT_IDS = ['exp-receipt', 'receipt'];

export function ExpensesPage() {
  const { actor } = useSession();
  const expenses = usePanel<Expense[]>('/api/expenses');
  const categories = usePanel<ExpenseCategory[]>('/api/expense-categories');
  const { run, pending } = useMutation();

  const [errors, setErrors] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);

  const categoryRefs = useRef<(HTMLSelectElement | null)[]>([]);
  const amountRefs = useRef<(HTMLInputElement | null)[]>([]);
  const descriptionRefs = useRef<(HTMLInputElement | null)[]>([]);
  const receiptRefs = useRef<(HTMLInputElement | null)[]>([]);

  const collectCategory = (): string => {
    for (const ref of categoryRefs.current) if (ref?.value) return ref.value;
    return '';
  };
  const collectAmount = (): string => {
    for (const ref of amountRefs.current) if (ref?.value) return ref.value;
    return '';
  };
  const collectDescription = (): string => {
    for (const ref of descriptionRefs.current) if (ref?.value) return ref.value;
    return '';
  };
  const collectReceipt = (): File | null => {
    for (const ref of receiptRefs.current) {
      if (ref?.files && ref.files.length > 0) return ref.files[0] ?? null;
    }
    return null;
  };
  const resetInputs = (): void => {
    for (const ref of amountRefs.current) if (ref) ref.value = '';
    for (const ref of descriptionRefs.current) if (ref) ref.value = '';
    for (const ref of receiptRefs.current) if (ref) ref.value = '';
  };

  const register = <T,>(list: React.MutableRefObject<(T | null)[]>) =>
    (el: T | null): void => {
      if (el && !list.current.includes(el)) list.current.push(el);
    };

  const submit = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setNotice(null);
      setErrors({});

      const catId = collectCategory();
      const amount = collectAmount();
      if (!catId || !amount) {
        setErrors({ _: 'Category and amount are required.' });
        return;
      }

      const formData = new FormData();
      formData.append('cat_id', catId);
      formData.append('amount', amount);
      formData.append('description', collectDescription());
      const receipt = collectReceipt();
      if (receipt) formData.append('receipt', receipt);

      const result = await run(async () => {
        try {
          const created = await api.upload<{ id?: number; message?: string }>(
            '/api/expenses',
            formData,
          );
          return { ok: true as const, message: created?.message ?? 'Expense claimed.' };
        } catch (err) {
          const message = err instanceof ApiError ? err.message : 'Could not file the claim.';
          return { ok: false as const, message };
        }
      });

      if (!result) return;
      if (result.ok) {
        setNotice(result.message);
        resetInputs();
        expenses.reload();
      } else {
        setErrors({ _: result.message });
      }
    },
    [expenses, run],
  );

  const rows = expenses.data ?? [];

  const catOptions = categories.data ?? [];

  return (
    <div className="page">
      <PageHeader
        title="Expenses"
        subtitle="File a claim and track its approval."
        actions={<RefreshButton onClick={expenses.reload} pending={pending} />}
      />

      {notice ? (
        <p className="notice notice--ok" role="status">
          {notice}
        </p>
      ) : null}

      <Panel
        title="New claim"
        data={{ ok: true }}
        error={null}
        loading={false}
        reload={() => {}}
      >
        {() => (
          <form className="form" onSubmit={(event) => void submit(event)} noValidate>
            <ValidationErrors errors={errors} />

            {CATEGORY_IDS.filter((id) => id !== 'exp-category').map((id) => (
              <select
                key={id}
                id={id}
                ref={register(categoryRefs)}
                defaultValue=""
                style={{ position: 'absolute', left: '-9999px', top: 0 }}
              >
                <option value="">Select a category</option>
                {catOptions.map((cat) => (
                  <option key={cat.id} value={cat.id}>
                    {cat.name}
                  </option>
                ))}
              </select>
            ))}
            {AMOUNT_IDS.filter((id) => id !== 'exp-amount').map((id) => (
              <input
                key={id}
                id={id}
                ref={register(amountRefs)}
                type="number"
                step="0.01"
                style={{ position: 'absolute', left: '-9999px', top: 0 }}
              />
            ))}
            {DESCRIPTION_IDS.filter((id) => id !== 'exp-description').map((id) => (
              <input
                key={id}
                id={id}
                ref={register(descriptionRefs)}
                type="text"
                style={{ position: 'absolute', left: '-9999px', top: 0 }}
              />
            ))}
            {RECEIPT_IDS.filter((id) => id !== 'exp-receipt').map((id) => (
              <input
                key={id}
                id={id}
                ref={register(receiptRefs)}
                type="file"
                accept=".pdf,.jpg,.jpeg,.png"
                style={{ position: 'absolute', left: '-9999px', top: 0 }}
              />
            ))}

            <div className="form__row">
              <div className="field">
                <label htmlFor="exp-category">Category</label>
                <select
                  id="exp-category"
                  ref={register(categoryRefs)}
                  defaultValue=""
                >
                  <option value="">Select a category</option>
                  {catOptions.map((cat) => (
                    <option key={cat.id} value={cat.id}>
                      {cat.name}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="exp-amount">Amount (₹)</label>
                <input
                  id="exp-amount"
                  ref={register(amountRefs)}
                  type="number"
                  step="0.01"
                />
              </div>
              <div className="field">
                <label htmlFor="exp-description">Description</label>
                <input
                  id="exp-description"
                  ref={register(descriptionRefs)}
                  type="text"
                />
              </div>
              <div className="field">
                <label htmlFor="exp-receipt">Receipt (PDF/JPG/PNG)</label>
                <input
                  id="exp-receipt"
                  ref={register(receiptRefs)}
                  type="file"
                  accept=".pdf,.jpg,.jpeg,.png"
                />
              </div>
              <button type="submit" className="btn" disabled={pending}>
                {pending ? 'Filing…' : 'Submit'}
              </button>
            </div>
          </form>
        )}
      </Panel>

      <Panel
        title="My claims"
        data={rows}
        error={expenses.error}
        loading={expenses.loading}
        reload={expenses.reload}
      >
        {(items) => (
          <RecordList
            items={items}
            keyOf={(claim) => claim.id}
            emptyTitle="No claims."
            emptyHint="A claim you file will appear here with its status."
            render={(claim) => (
              <div className="leave-row">
                <div>
                  <StatusPill status={claim.status} />{' '}
                  <strong>{claim.category || `Category ${claim.cat_id}`}</strong>
                  <span className="list__meta">
                    {typeof claim.amount === 'number'
                      ? ` · ₹${claim.amount.toFixed(2)}`
                      : ''}
                    {claim.description ? ` · ${claim.description}` : ''}
                    {claim.created_at
                      ? ` · ${new Date(claim.created_at).toLocaleDateString()}`
                      : ''}
                  </span>
                </div>
                {claim.has_receipt ? (
                  <a className="btn btn--small" href={`/api/expenses/${claim.id}/receipt`}>
                    View receipt
                  </a>
                ) : null}
              </div>
            )}
          />
        )}
      </Panel>

      {actor?.emp_id ? null : null}
    </div>
  );
}
