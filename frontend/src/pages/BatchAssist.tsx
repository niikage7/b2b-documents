import { useMutation, useQueryClient } from '@tanstack/react-query';
import { FileQuestion, MessageCircleQuestion, Sparkles } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router';
import { api } from '../api/endpoints';
import { keys } from '../api/queries';
import type { BatchView, FindingRef, PackageAnswer, PackageBrief } from '../api/types';
import { errorMessage } from '../components/feedback';
import { Button, LevelBadge } from '../components/ui';

export function refLink(ref: Pick<FindingRef, 'document_id' | 'block_id' | 'start' | 'end'>) {
  return `/documents/${ref.document_id}?block=${ref.block_id}&start=${ref.start}&end=${ref.end}`;
}

export function refPlace(ref: Pick<FindingRef, 'filename' | 'section_number' | 'page'>) {
  return [ref.filename, ref.section_number && `п. ${ref.section_number}`, ref.page && `стр. ${ref.page}`].filter(Boolean).join(', ');
}

const SUGGESTED = ['Какой срок поставки?', 'Какое обеспечение нужно внести?', 'Когда заказчик оплатит товар?', 'Какие документы нужны в заявке?'];

/** Вопрос к документам пакета: ответ только из текста, каждая цитата открывает место в документе. */
export function AskPanel({ batchId }: { batchId: string }) {
  const [question, setQuestion] = useState('');
  const [history, setHistory] = useState<{ question: string; answer: PackageAnswer }[]>([]);
  const ask = useMutation({
    mutationFn: (q: string) => api.askBatch(batchId, q),
    onSuccess: (answer, q) => {
      setHistory((h) => [{ question: q, answer }, ...h]);
      setQuestion('');
    },
  });
  const send = (q: string) => {
    if (q.trim() && !ask.isPending) ask.mutate(q.trim());
  };
  return (
    <section className="card ask">
      <div className="card__body stack" style={{ gap: 12 }}>
        <div>
          <h3>Спросить по документам пакета</h3>
          <p className="muted side-card__hint">Ответ строится только по тексту пакета. Нажмите на цитату, чтобы открыть это место в документе.</p>
        </div>
        <form
          className="ask__form"
          onSubmit={(e) => {
            e.preventDefault();
            send(question);
          }}
        >
          <input className="input" aria-label="Вопрос по документам" placeholder="Например: какой срок поставки?" value={question} maxLength={500} onChange={(e) => setQuestion(e.target.value)} />
          <Button type="submit" variant="primary" loading={ask.isPending} disabled={!question.trim()}>
            Спросить
          </Button>
        </form>
        {!history.length && !ask.isPending && (
          <div className="ask__suggested">
            {SUGGESTED.map((q) => (
              <button key={q} type="button" className="chip" onClick={() => send(q)}>
                {q}
              </button>
            ))}
          </div>
        )}
        {ask.isError && <div className="field__error">{errorMessage(ask.error)}</div>}
        {history.map((item, i) => (
          <article key={i} className="ask__item">
            <div className="ask__question">
              <MessageCircleQuestion size={16} /> {item.question}
            </div>
            <p className={item.answer.found ? 'ask__answer' : 'ask__answer muted'}>{item.answer.answer}</p>
            {item.answer.citations.map((ref, j) => (
              <Link key={j} className="ask__citation" to={refLink(ref)}>
                <span className="ask__quote">«{ref.quote}»</span>
                <span className="subtle">{refPlace(ref)}</span>
              </Link>
            ))}
          </article>
        ))}
      </div>
    </section>
  );
}

const DECISION_CLASS: Record<PackageBrief['decision'], string> = { go: 'brief__decision--ok', caution: 'brief__decision--warning', clarify: 'brief__decision--critical' };

/** Вывод для руководителя: итог считается по находкам после ревью, модель пишет только текст. */
export function BriefCard({ batch }: { batch: BatchView }) {
  const qc = useQueryClient();
  const brief = batch.meta?.brief as PackageBrief | undefined;
  const make = useMutation({
    mutationFn: () => api.batchBrief(batch.id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: keys.batch(batch.id) }),
  });
  const shown = make.data ?? brief;
  return (
    <section className="card side-card">
      <h3>Вывод для руководителя</h3>
      {shown ? (
        <div className="brief">
          <div className={`brief__decision ${DECISION_CLASS[shown.decision]}`}>{shown.decision_label}</div>
          <p>{shown.summary}</p>
          {shown.not_checked > 0 && <p className="fc__note">Не проверено документов: {shown.not_checked}. Вывод по пакету неполный.</p>}
          {shown.top_risks.length > 0 && (
            <ul className="brief__risks">
              {shown.top_risks.map((r, i) => (
                <li key={i}>
                  <LevelBadge level={r.level} small />{' '}
                  {r.document_id && r.finding_id ? <Link to={`/documents/${r.document_id}?finding=${r.finding_id}`}>{r.title}</Link> : r.title}
                  {r.section_number && <span className="subtle"> · п. {r.section_number}</span>}
                </li>
              ))}
            </ul>
          )}
          {shown.actions.length > 0 && (
            <>
              <b>Что сделать до подачи заявки</b>
              <ol className="brief__actions">
                {shown.actions.map((a, i) => (
                  <li key={i}>{a}</li>
                ))}
              </ol>
            </>
          )}
        </div>
      ) : (
        <p className="muted side-card__hint">Короткий вывод: участвовать ли и что сделать до подачи заявки. Учитывает решения по находкам.</p>
      )}
      {make.isError && <div className="field__error">{errorMessage(make.error)}</div>}
      <Button size="sm" loading={make.isPending} onClick={() => make.mutate()}>
        {!make.isPending && <Sparkles size={16} />}
        {shown ? 'Обновить вывод' : 'Составить вывод'}
      </Button>
    </section>
  );
}

export function ClarificationButton({ pending, onClick, disabled }: { pending: boolean; onClick: () => void; disabled: boolean }) {
  return (
    <Button loading={pending} disabled={disabled} onClick={onClick} title="Письмо заказчику в DOCX: вопросы по всем находкам, кроме отклонённых">
      {!pending && <FileQuestion size={18} />}
      Запрос о разъяснениях
    </Button>
  );
}
