import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../api/endpoints';
import { keys } from '../api/queries';
import { ConfirmDialog, Dialog, errorMessage, useToast } from './feedback';
import { Button, Field } from './ui';

export function RenameBatchDialog({ batch, onClose }: { batch: { id: string; name: string }; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [name, setName] = useState(batch.name);
  const mutation = useMutation({
    mutationFn: () => api.updateBatch(batch.id, { name: name.trim() }),
    onSuccess: (view) => {
      qc.setQueryData(keys.batch(batch.id), view);
      void qc.invalidateQueries({ queryKey: keys.batches() });
      toast.show('Пакет переименован');
      onClose();
    },
  });
  return (
    <Dialog
      title="Переименовать пакет"
      onClose={onClose}
      actions={
        <>
          <Button onClick={onClose}>Отмена</Button>
          <Button variant="primary" loading={mutation.isPending} disabled={!name.trim()} onClick={() => mutation.mutate()}>
            Сохранить
          </Button>
        </>
      }
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (name.trim()) mutation.mutate();
        }}
      >
        <Field label="Название пакета" htmlFor="batch-name" error={mutation.error ? errorMessage(mutation.error) : null}>
          <input id="batch-name" className="input" autoFocus value={name} onChange={(e) => setName(e.target.value)} maxLength={200} />
        </Field>
      </form>
    </Dialog>
  );
}

export function DeleteBatchDialog({ batch, onClose, onDeleted }: { batch: { id: string; name: string }; onClose: () => void; onDeleted?: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const mutation = useMutation({
    mutationFn: () => api.deleteBatch(batch.id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: keys.batches() });
      qc.removeQueries({ queryKey: keys.batch(batch.id) });
      toast.show(`Пакет «${batch.name}» удалён`);
      onClose();
      onDeleted?.();
    },
    onError: (e) => toast.error(e),
  });
  return (
    <ConfirmDialog
      title="Удалить пакет?"
      text={
        <>
          Пакет «<b>{batch.name}</b>» исчезнет из списка вместе с находками и решениями по ним. Если пакет ещё обрабатывается, обработка
          остановится.
        </>
      }
      confirmLabel="Удалить пакет"
      danger
      loading={mutation.isPending}
      onConfirm={() => mutation.mutate()}
      onClose={onClose}
    />
  );
}
