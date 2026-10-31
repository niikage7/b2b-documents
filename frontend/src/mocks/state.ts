// Состояние мока API: таблицы из «Требований к бэкенду» в памяти браузера.
// Сохраняется в localStorage, чтобы ревью находок переживало перезагрузку страницы, как в настоящем API.

import type {
  BatchMeta,
  BatchStatus,
  Block,
  DocumentKind,
  DocumentStatus,
  FindingSource,
  FindingStatus,
  HistoryAction,
  Level,
  Role,
  RuleKind,
  RuleVariable,
  Stage,
  StageDetail,
  UploadedFile,
} from '../api/types';
import checklist from './checklist_r44.json';
import {
  CONTRACT,
  type DocumentTemplate,
  LETTER,
  longDocument,
  NOTICE,
  PASSPORT_FIELDS,
  TECH_SPEC_SCAN,
} from './fixtures';

export const DEMO_PASSWORD = 'demo';
const STORAGE_KEY = 'b2b-tender-mock-v1';

export interface MUser {
  id: string;
  login: string;
  email: string;
  full_name: string;
  role: Role;
  failed_logins: number;
  locked_until: number | null;
}

export interface MProfile {
  id: string;
  name: string;
  short_name: string;
  is_default: boolean;
  created_at: string;
}

export interface MRule {
  id: string;
  profile_id: string;
  code: string;
  title: string;
  description: string;
  level: Level;
  kind: RuleKind;
  prompt_template: string;
  variables: RuleVariable[];
  enabled: boolean;
  version: number;
  updated_at: string;
  updated_by: string | null;
  deleted: boolean;
}

export interface MBatch {
  id: string;
  name: string;
  subject: string | null;
  meta: BatchMeta | null;
  status: BatchStatus;
  profile_id: string;
  author_id: string;
  total_bytes: number;
  created_at: string;
  finished_at: string | null;
  deleted: boolean;
  files: UploadedFile[];
}

export interface MDocument {
  id: string;
  batch_id: string;
  filename: string;
  original_path: string;
  kind: DocumentKind;
  pages: number | null;
  size_bytes: number;
  status: DocumentStatus;
  stage: Stage;
  stage_detail: StageDetail | null;
  progress: number;
  error_code: string | null;
  error_message: string | null;
  ocr_confidence: number | null;
  low_quality_pages: number[];
  worst_level: string | null;
  /** Чем закончится обработка в симуляции: ошибкой с этим кодом или шаблоном текста. */
  fail_with: string | null;
  template: TemplateName;
  include_rules: string[] | null;
  finished_at: string | null;
}

export type MBlock = Block & { document_id: string };

export interface MFinding {
  id: string;
  batch_id: string;
  document_id: string;
  rule_id: string | null;
  source: FindingSource;
  level: Level;
  original_level: Level;
  status: FindingStatus;
  block_id: string;
  start: number;
  end: number;
  quote: string;
  explanation: string;
  recommendation: string | null;
  confidence: number | null;
  comment: string | null;
  author_id: string | null;
  decided_by: string | null;
  decided_at: string | null;
  version: number;
  created_at: string;
}

export interface MEvent {
  id: string;
  finding_id: string;
  actor_id: string | null;
  action: HistoryAction;
  old: Record<string, unknown> | null;
  new: Record<string, unknown> | null;
  created_at: string;
}

export interface MExport {
  id: string;
  scope: 'document' | 'batch';
  target_id: string;
  filename: string;
  ready_at: number;
}

export interface MockState {
  users: MUser[];
  profiles: MProfile[];
  rules: MRule[];
  batches: MBatch[];
  documents: MDocument[];
  blocks: MBlock[];
  findings: MFinding[];
  events: MEvent[];
  exports: MExport[];
  session: string | null;
}

export type TemplateName = 'contract' | 'scan' | 'notice' | 'letter' | 'long';

export const TEMPLATES: Record<TemplateName, () => DocumentTemplate> = {
  contract: () => CONTRACT,
  scan: () => TECH_SPEC_SCAN,
  notice: () => NOTICE,
  letter: () => LETTER,
  long: longDocument,
};

/** Шаблон по имени файла: так новые пакеты в моке получают правдоподобное содержимое. */
export function templateNameFor(filename: string): TemplateName {
  const name = filename.toLowerCase();
  if (name.includes('контракт') || name.includes('договор') || name.includes('contract')) return 'contract';
  if (name.includes('тз') || name.includes('техн') || name.includes('скан') || name.includes('scan')) return 'scan';
  if (name.includes('извещ') || name.includes('notice')) return 'notice';
  if (name.includes('документац')) return 'long';
  return 'letter';
}

export function uid(): string {
  return crypto.randomUUID();
}

export function nowIso(offsetMs = 0): string {
  return new Date(Date.now() + offsetMs).toISOString().replace(/\.\d{3}Z$/, 'Z');
}

const HOUR = 3600_000;
const DAY = 24 * HOUR;

// --- Сборка документа из шаблона -------------------------------------------------------------

/** Блоки с разделами: номер пункта и заголовок верхнего уровня, как делает build_structure. */
export function buildBlocks(documentId: string, template: DocumentTemplate): { blocks: MBlock[]; keys: Map<string, MBlock> } {
  const keys = new Map<string, MBlock>();
  let section: MBlock | null = null;
  const blocks = template.blocks.map((tb, i) => {
    const block: MBlock = {
      id: `${documentId}-b${i}`,
      document_id: documentId,
      order: i,
      kind: tb.kind,
      level: tb.level ?? null,
      number: tb.number ?? null,
      text: tb.text,
      page: tb.page,
      low_ocr_quality: Boolean(tb.lowOcr),
    };
    if (tb.align) block.align = tb.align;
    if (tb.bold) block.bold = true;
    if (tb.table) Object.assign(block, { table_id: tb.table.id, row: tb.table.row, col: tb.table.col });
    if (tb.kind === 'heading' && (tb.level ?? 1) <= 1) {
      section = block;
    } else if (section) {
      block.section_block_id = section.id;
      if (tb.number) block.section_number = tb.number.replace(/\.$/, '');
      else if (section.number) block.section_number = section.number;
    }
    if (tb.key) keys.set(tb.key, block);
    return block;
  });
  return { blocks, keys };
}

export function buildFindings(
  state: Pick<MockState, 'rules' | 'users'>,
  doc: MDocument,
  batch: MBatch,
  template: DocumentTemplate,
  keys: Map<string, MBlock>,
  { reviewed }: { reviewed: boolean },
): MFinding[] {
  const out: MFinding[] = [];
  for (const tf of template.findings) {
    if (doc.include_rules && !doc.include_rules.includes(tf.rule)) continue;
    const block = keys.get(tf.block);
    if (!block) continue;
    const start = block.text.indexOf(tf.quote);
    if (start < 0) continue; // цитаты нет в тексте — находка отбрасывается, как в FR-4.3
    const rule =
      state.rules.find((r) => r.profile_id === batch.profile_id && r.code === tf.rule && !r.deleted) ??
      state.rules.find((r) => r.code === tf.rule);
    if (rule && !rule.enabled && rule.profile_id === batch.profile_id) continue;
    const level = tf.level ?? rule?.level ?? 'warning';
    const decided = reviewed && tf.status && tf.status !== 'new';
    const decider = decided ? state.users.find((u) => u.role === tf.decidedBy) : undefined;
    out.push({
      id: uid(),
      batch_id: batch.id,
      document_id: doc.id,
      rule_id: rule?.id ?? null,
      source: 'ai',
      level,
      original_level: level,
      status: decided ? tf.status! : 'new',
      block_id: block.id,
      start,
      end: start + tf.quote.length,
      quote: tf.quote,
      explanation: tf.explanation,
      recommendation: tf.recommendation,
      confidence: tf.confidence,
      comment: reviewed ? (tf.comment ?? null) : null,
      author_id: null,
      decided_by: decider?.id ?? null,
      decided_at: decided ? nowIso(-3 * HOUR) : null,
      version: 1,
      created_at: nowIso(-5 * HOUR),
    });
  }
  return out;
}

// --- Сид -------------------------------------------------------------------------------------

interface SeedDoc {
  filename: string;
  template: TemplateName;
  size: number;
  include?: string[];
  fail?: string;
  stage?: Stage;
  progress?: number;
  detail?: StageDetail;
}

interface SeedBatch {
  name: string;
  subject: string | null;
  author: Role;
  profile: number;
  ago: number;
  status: BatchStatus;
  docs: SeedDoc[];
  reviewed?: boolean;
  passport?: boolean;
}

const MB = 1024 * 1024;

const SEED_BATCHES: SeedBatch[] = [
  {
    name: 'Закупка_0001', subject: 'Поставка серверного оборудования', author: 'methodologist', profile: 0, ago: 0.6 * DAY,
    status: 'partial_error', reviewed: true, passport: true,
    docs: [
      { filename: 'Проект_контракта.docx', template: 'contract', size: 0.6 * MB },
      { filename: 'ТЗ_скан.pdf', template: 'scan', size: 30 * MB },
      { filename: 'Извещение.pdf', template: 'notice', size: 1.2 * MB },
      { filename: 'Письмо_заказчику.pdf', template: 'letter', size: 2 * MB, fail: 'encrypted' },
    ],
  },
  {
    name: 'Закупка_0002', subject: 'Ремонт кровли склада', author: 'specialist', profile: 1, ago: 0.2 * DAY, status: 'processing',
    docs: [
      { filename: 'Извещение.pdf', template: 'notice', size: 0.9 * MB },
      { filename: 'Проект_контракта.docx', template: 'contract', size: 0.7 * MB, stage: 'analysis', progress: 78, detail: { rules_done: 3, rules_total: 8 } },
      { filename: 'ТЗ_кровля_скан.pdf', template: 'scan', size: 18 * MB, stage: 'ocr', progress: 45, detail: { page: 4, pages: 12 } },
      { filename: 'Документация_о_закупке.pdf', template: 'long', size: 6 * MB, stage: 'extracting', progress: 20 },
      { filename: 'Локальная_смета.docx', template: 'letter', size: 0.3 * MB, stage: 'queued', progress: 0 },
    ],
  },
  {
    name: 'Закупка_0003', subject: 'Поставка канцелярских товаров', author: 'methodologist', profile: 2, ago: 1.4 * DAY, status: 'done',
    docs: [
      { filename: 'Договор_поставки.docx', template: 'contract', size: 0.4 * MB, include: ['R-07'] },
      { filename: 'Спецификация.docx', template: 'letter', size: 0.1 * MB },
    ],
  },
  {
    name: 'Закупка_0004', subject: 'Уборка офисных помещений', author: 'lawyer', profile: 3, ago: 1.8 * DAY, status: 'done',
    docs: [
      { filename: 'Извещение.pdf', template: 'notice', size: 0.8 * MB },
      { filename: 'Договор_оказания_услуг.docx', template: 'contract', size: 0.5 * MB, include: [] },
      { filename: 'Регламент_уборки.docx', template: 'letter', size: 0.2 * MB },
      { filename: 'График.docx', template: 'letter', size: 0.1 * MB },
    ],
  },
  {
    name: 'Закупка_0005', subject: 'Поставка офисной мебели', author: 'specialist', profile: 0, ago: 2.3 * DAY, status: 'done', reviewed: true,
    docs: [
      { filename: 'Проект_контракта.docx', template: 'contract', size: 0.6 * MB, include: ['R-04', 'R-07', 'R-08'] },
      { filename: 'Извещение.pdf', template: 'notice', size: 0.7 * MB },
      { filename: 'ТЗ_мебель_скан.pdf', template: 'scan', size: 12 * MB, include: [] },
      { filename: 'Обоснование_НМЦК.docx', template: 'letter', size: 0.2 * MB },
      { filename: 'Требования_к_заявке.docx', template: 'letter', size: 0.2 * MB },
      { filename: 'Документация_о_закупке.pdf', template: 'long', size: 5 * MB },
    ],
  },
  {
    name: 'Закупка_0006', subject: 'Услуги связи и интернета', author: 'methodologist', profile: 1, ago: 0.05 * DAY, status: 'queued',
    docs: [
      { filename: 'Извещение.pdf', template: 'notice', size: 0.6 * MB, stage: 'queued', progress: 0 },
      { filename: 'Проект_контракта.docx', template: 'contract', size: 0.5 * MB, stage: 'queued', progress: 0, include: ['R-05'] },
    ],
  },
  {
    name: 'Закупка_0007', subject: 'Поставка медицинских расходных материалов', author: 'lawyer', profile: 0, ago: 8 * DAY, status: 'done', reviewed: true,
    docs: [
      { filename: 'Проект_контракта.docx', template: 'contract', size: 0.6 * MB, include: ['R-05', 'R-02'] },
      { filename: 'Извещение.pdf', template: 'notice', size: 0.9 * MB },
      { filename: 'ТЗ_скан.pdf', template: 'scan', size: 9 * MB },
    ],
  },
  {
    name: 'Закупка_0008', subject: 'Техническое обслуживание лифтов', author: 'specialist', profile: 1, ago: 12 * DAY, status: 'done',
    docs: [
      { filename: 'Извещение.pdf', template: 'notice', size: 0.5 * MB },
      { filename: 'Договор.docx', template: 'contract', size: 0.4 * MB, include: [] },
    ],
  },
  {
    name: 'Закупка_0009', subject: 'Поставка ноутбуков', author: 'methodologist', profile: 0, ago: 15 * DAY, status: 'done',
    docs: [
      { filename: 'ТЗ_скан.pdf', template: 'scan', size: 14 * MB },
      { filename: 'Проект_контракта.docx', template: 'contract', size: 0.6 * MB, include: ['R-08'] },
      { filename: 'Извещение.pdf', template: 'notice', size: 0.6 * MB },
    ],
  },
  {
    name: 'Закупка_0010', subject: 'Ремонт дорожного покрытия', author: 'specialist', profile: 1, ago: 20 * DAY, status: 'partial_error',
    docs: [
      { filename: 'Проект_контракта.docx', template: 'contract', size: 0.8 * MB, include: ['R-04', 'R-05', 'R-07'] },
      { filename: 'Смета_скан.pdf', template: 'scan', size: 22 * MB, fail: 'corrupted' },
    ],
  },
  {
    name: 'Закупка_0011', subject: 'Поставка продуктов питания', author: 'lawyer', profile: 2, ago: 26 * DAY, status: 'done',
    docs: [
      { filename: 'Договор_поставки.docx', template: 'contract', size: 0.5 * MB, include: [] },
      { filename: 'Спецификация.docx', template: 'letter', size: 0.1 * MB },
    ],
  },
  {
    name: 'Закупка_0012', subject: 'Охрана объектов', author: 'methodologist', profile: 3, ago: 45 * DAY, status: 'done',
    docs: [{ filename: 'Договор_охраны.docx', template: 'contract', size: 0.5 * MB, include: ['R-05'] }],
  },
];

export const ERROR_MESSAGES: Record<string, string> = {
  encrypted: 'Файл защищён паролем',
  corrupted: 'Файл повреждён и не открывается',
  unsupported: 'Формат не поддерживается',
  too_many_pages: 'Документ больше 500 страниц',
};

function seedRules(profiles: MProfile[], methodologist: string): MRule[] {
  const base = checklist.rules as { code: string; title: string; description: string; level: Level; kind: RuleKind; prompt_template: string; variables: RuleVariable[] }[];
  const sets: string[][] = [
    base.map((r) => r.code),
    base.map((r) => r.code),
    ['R-01', 'R-04', 'R-05', 'R-07'],
    ['R-03', 'R-05', 'R-07'],
  ];
  const rules: MRule[] = [];
  profiles.forEach((profile, i) => {
    for (const r of base.filter((x) => sets[i].includes(x.code))) {
      rules.push({
        id: uid(), profile_id: profile.id, code: r.code, title: r.title, description: r.description, level: r.level,
        kind: r.kind, prompt_template: r.prompt_template, variables: r.variables.map((v) => ({ ...v })),
        enabled: r.code !== 'R-06', version: 1 + (r.code.charCodeAt(3) % 3), updated_at: nowIso(-(20 + i) * DAY),
        updated_by: methodologist, deleted: false,
      });
    }
  });
  return rules;
}

export function seedState(): MockState {
  const users: MUser[] = [
    { id: uid(), login: 'methodologist', email: 'm.volkova@company.ru', full_name: 'Мария Волкова', role: 'methodologist', failed_logins: 0, locked_until: null },
    { id: uid(), login: 'lawyer', email: 'a.smirnova@company.ru', full_name: 'Анна Смирнова', role: 'lawyer', failed_logins: 0, locked_until: null },
    { id: uid(), login: 'specialist', email: 'i.petrov@company.ru', full_name: 'Илья Петров', role: 'specialist', failed_logins: 0, locked_until: null },
  ];
  const profiles: MProfile[] = [
    { id: uid(), name: checklist.profile.name, short_name: checklist.profile.short_name, is_default: true, created_at: nowIso(-60 * DAY) },
    { id: uid(), name: '44-ФЗ, работы и услуги', short_name: '44-ФЗ, работы', is_default: false, created_at: nowIso(-50 * DAY) },
    { id: uid(), name: '223-ФЗ', short_name: '223-ФЗ', is_default: false, created_at: nowIso(-40 * DAY) },
    { id: uid(), name: 'Коммерческие закупки', short_name: 'Коммерческие', is_default: false, created_at: nowIso(-30 * DAY) },
  ];
  const state: MockState = {
    users, profiles, rules: seedRules(profiles, users[0].id), batches: [], documents: [], blocks: [], findings: [],
    events: [], exports: [], session: null,
  };

  for (const sb of SEED_BATCHES) {
    const author = users.find((u) => u.role === sb.author)!;
    const batch: MBatch = {
      id: uid(), name: sb.name, subject: sb.subject, meta: null, status: sb.status, profile_id: profiles[sb.profile].id,
      author_id: author.id, total_bytes: 0, created_at: nowIso(-sb.ago), finished_at: null, deleted: false, files: [],
    };
    state.batches.push(batch);
    const keysByTemplate = new Map<TemplateName, { doc: MDocument; keys: Map<string, MBlock> }>();
    for (const sd of sb.docs) {
      const template = TEMPLATES[sd.template]();
      const stage: Stage = sd.stage ?? (sd.fail ? 'error' : 'done');
      const doc: MDocument = {
        id: uid(), batch_id: batch.id, filename: sd.filename, original_path: sd.filename, kind: template.kind,
        pages: stage === 'done' || stage === 'analysis' || stage === 'ocr' ? template.pages : null,
        size_bytes: Math.round(sd.size), status: stage === 'done' ? 'done' : stage === 'error' ? 'error' : stage === 'queued' ? 'queued' : 'processing',
        stage, stage_detail: sd.detail ?? null, progress: stage === 'done' ? 100 : (sd.progress ?? 0),
        error_code: stage === 'error' ? sd.fail! : null, error_message: stage === 'error' ? ERROR_MESSAGES[sd.fail!] : null,
        ocr_confidence: template.kind === 'scan' && stage === 'done' ? (template.ocrConfidence ?? null) : null,
        low_quality_pages: template.kind === 'scan' && stage === 'done' ? (template.lowQualityPages ?? []) : [],
        worst_level: null, fail_with: sd.fail ?? null, template: sd.template, include_rules: sd.include ?? null,
        finished_at: stage === 'done' || stage === 'error' ? batch.created_at : null,
      };
      // Тип — по расширению файла; «скан» становится известен только после извлечения текста
      if (template.kind !== 'scan' || stage === 'queued' || stage === 'extracting' || stage === 'error') {
        const ext = sd.filename.split('.').pop()!.toLowerCase();
        doc.kind = (['docx', 'doc', 'rtf'].includes(ext) ? ext : 'pdf') as DocumentKind;
      }
      batch.total_bytes += doc.size_bytes;
      state.documents.push(doc);
      if (doc.status === 'done') {
        const { blocks, keys } = buildBlocks(doc.id, template);
        state.blocks.push(...blocks);
        state.findings.push(...buildFindings(state, doc, batch, template, keys, { reviewed: Boolean(sb.reviewed) }));
        keysByTemplate.set(sd.template, { doc, keys });
      }
    }
    if (sb.passport) batch.meta = { passport: { fields: passportFields(keysByTemplate) }, finance: MOCK_FINANCE };
    if (sb.status === 'done' || sb.status === 'partial_error') batch.finished_at = nowIso(-sb.ago + 3 * 60_000);
  }
  return state;
}

export const MOCK_FINANCE = {
  nmck: 48_600_000,
  securities: [
    { key: 'application_security', label: 'Обеспечение заявки', value: '1 % НМЦК', required: true, amount: 486_000, percent: 1, basis: 'percent_of_nmck' as const },
    { key: 'contract_security', label: 'Обеспечение исполнения', value: '30 % НМЦК', required: true, amount: 14_580_000, percent: 30, basis: 'percent_of_nmck' as const },
  ],
  total_security: 15_066_000,
  total_incomplete: false,
  cash_cycle: { delivery_days: 3, acceptance_days: 35, payment_days: 42, total_days: 80 },
  deadline_at: '2026-11-20T10:00',
};

export function passportFields(docs: Map<TemplateName, { doc: MDocument; keys: Map<string, MBlock> }>) {
  const out = [];
  for (const field of PASSPORT_FIELDS) {
    const entry = docs.get(field.doc === 'notice' ? 'notice' : 'contract');
    const block = entry?.keys.get(field.block);
    if (!entry || !block) continue;
    const start = block.text.indexOf(field.quote);
    if (start < 0) continue;
    out.push({
      key: field.key, label: field.label, value: field.value, quote: field.quote, document_id: entry.doc.id,
      filename: entry.doc.filename, block_id: block.id, start, end: start + field.quote.length, page: block.page,
      section_number: block.section_number ?? null,
    });
  }
  return out;
}

// --- Хранение --------------------------------------------------------------------------------

export function loadState(): MockState {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return JSON.parse(raw) as MockState;
  } catch {
    // Хранилище недоступно (приватное окно) — работаем на свежем сиде
  }
  return seedState();
}

let saveTimer: ReturnType<typeof setTimeout> | null = null;

export function saveState(state: MockState) {
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch {
      // Не сохранилось — данные останутся до перезагрузки
    }
  }, 200);
}

export function resetState() {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    // нечего удалять
  }
}
