// Типы ответов API по «Требованиям к бэкенду» (6 октября) и слою данных в db/.
// Когда API опубликует /api/openapi.json, этот файл заменяется сгенерированными типами
// (openapi-typescript), имена полей уже совпадают.

export type Level = 'critical' | 'warning';
export type WorstLevel = Level | 'ok' | 'not_checked' | null;
export type Role = 'specialist' | 'lawyer' | 'methodologist';

export interface Me {
  id: string;
  full_name: string;
  initials: string;
  role: Role;
  role_label: string;
  email: string | null;
}

export interface AppConfig {
  upload: { allowed_extensions: string[]; max_batch_bytes: number };
  confidence_thresholds: { high: number; medium: number };
}

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export interface Person {
  id?: string;
  full_name: string;
}

export interface FindingsCount {
  critical: number;
  warning: number;
}

export interface Review {
  checked: number;
  total: number;
}

// --- Пакеты ---------------------------------------------------------------------------------

export type BatchStatus = 'draft' | 'queued' | 'processing' | 'done' | 'partial_error';

export interface BatchListItem {
  id: string;
  name: string;
  subject: string | null;
  created_at: string;
  author: Person | null;
  profile: { id: string; short_name: string } | null;
  documents_count: number;
  status: BatchStatus;
  error_documents_count: number;
  worst_level: WorstLevel;
  findings: FindingsCount;
}

export interface BatchList extends Page<BatchListItem> {
  processing_count: number;
}

export type BatchStatusFilter = 'all' | 'processing' | 'done' | 'error';
export type LevelFilter = 'any' | 'critical' | 'warning' | 'ok';
export type PeriodFilter = '7d' | '30d' | 'all';
export type OwnerFilter = 'me' | 'all';

export interface BatchListParams {
  search?: string;
  status?: BatchStatusFilter;
  level?: LevelFilter;
  period?: PeriodFilter;
  owner?: OwnerFilter;
  page?: number;
  page_size?: number;
}

export type FileCheck = 'ok' | 'unsupported' | 'encrypted' | 'corrupted';

export interface ArchiveEntry {
  path: string;
  size_bytes: number;
  check: FileCheck;
}

export interface UploadedFile {
  id: string;
  filename: string;
  type_label: string;
  size_bytes: number;
  check: FileCheck;
  message: string | null;
  archive_entries: ArchiveEntry[] | null;
}

export interface Draft {
  id: string;
  status: 'draft';
  name: string;
  total_bytes: number;
  max_batch_bytes: number;
  files: UploadedFile[];
}

export interface DraftFiles {
  files: UploadedFile[];
  total_bytes: number;
}

// zip — только у архива, который не распаковался: документ сразу в статусе «Ошибка»
export type DocumentKind = 'docx' | 'doc' | 'rtf' | 'pdf' | 'scan' | 'zip';
export type DocumentStatus = 'queued' | 'processing' | 'done' | 'error';
export type Stage = 'queued' | 'extracting' | 'ocr' | 'analysis' | 'done' | 'error';

export interface StageDetail {
  page?: number;
  pages?: number;
  rules_done?: number;
  rules_total?: number;
}

export interface DocumentError {
  code: string | null;
  message: string | null;
}

export interface DocumentCard {
  id: string;
  filename: string;
  original_path?: string;
  kind: DocumentKind;
  pages: number | null;
  size_bytes: number;
  status: DocumentStatus;
  stage: Stage;
  stage_label: string;
  stage_detail: StageDetail | null;
  next_stage_label: string | null;
  progress: number;
  worst_level: WorstLevel;
  findings: FindingsCount;
  review: Review;
  ocr_confidence?: number | null;
  ocr_low_quality_pages: number[];
  error: DocumentError | null;
}

export interface BatchCounters {
  total: number;
  ok: number;
  warning: number;
  critical: number;
  errors: number;
}

export interface RuleTriggered {
  rule_id: string;
  code: string;
  title: string;
  level: Level;
  count: number;
}

export interface PassportField {
  key: string;
  label: string;
  value: string;
  quote: string;
  document_id: string;
  filename?: string | null;
  block_id: string;
  start: number;
  end: number;
  page?: number | null;
  section_number?: string | null;
}

export interface BatchMeta {
  passport?: { fields: PassportField[] };
  brief?: PackageBrief;
  [key: string]: unknown;
}

export interface BatchProgress {
  done: number;
  total: number;
  errors: number;
  percent: number;
}

export interface BatchView {
  id: string;
  name: string;
  subject: string | null;
  meta?: BatchMeta | null;
  status: BatchStatus;
  created_at: string;
  author: Person | null;
  profile: { id: string; name: string; short_name: string } | null;
  progress?: BatchProgress;
  eta_seconds?: number | null;
  worst_level?: WorstLevel;
  counters?: BatchCounters;
  review?: Review;
  findings?: FindingsCount;
  documents: DocumentCard[];
  rules_triggered?: RuleTriggered[];
  // Только у черновика
  files?: UploadedFile[];
  total_bytes?: number;
  max_batch_bytes?: number;
}

// --- Документ и находки -----------------------------------------------------------------------

export type BlockKind = 'heading' | 'paragraph' | 'list_item' | 'table_cell';

export interface Block {
  id: string;
  order: number;
  kind: BlockKind;
  level: number | null;
  number: string | null;
  text: string;
  page: number;
  low_ocr_quality: boolean;
  section_block_id?: string;
  section_number?: string;
  table_id?: number | string;
  row?: number;
  col?: number;
  align?: 'left' | 'center' | 'right' | 'justify';
  bold?: boolean;
}

export type FindingStatus = 'new' | 'accepted' | 'rejected' | 'edited';
export type FindingSource = 'ai' | 'user';
export type ConfidenceLevel = 'high' | 'medium' | 'low';

export interface FindingRef {
  document_id: string;
  filename?: string | null;
  block_id: string;
  start: number;
  end: number;
  quote: string;
  value?: string | null;
  page?: number | null;
  section_number?: string | null;
}

export interface PackageAnswer {
  found: boolean;
  answer: string;
  citations: FindingRef[];
}

export interface PackageBrief {
  decision: 'go' | 'caution' | 'clarify';
  decision_label: string;
  summary: string;
  top_risks: { finding_id?: string | null; document_id?: string | null; title: string; level: Level; document?: string | null; section_number?: string | null; page?: number | null; quote: string }[];
  actions: string[];
  not_checked: number;
}

export interface RuleDraft {
  code: string;
  title: string;
  description: string;
  level: Level;
  kind: RuleInput['kind'];
  prompt_template: string;
  variables: RuleInput['variables'];
  risk_example: string;
  safe_example: string;
  warnings: string[];
}

export interface Finding {
  id: string;
  document_id: string;
  order: number | null;
  source: FindingSource;
  status: FindingStatus;
  level: Level;
  original_level: Level;
  /** У встроенной проверки пакета (`check_code`) id нет: правило нельзя открыть */
  rule: { id: string | null; code: string; title: string } | null;
  check_code?: string | null;
  /** Другие места того же условия: второй документ противоречия */
  related?: FindingRef[];
  /** Текст распознан со скана: значения стоит сверить с оригиналом */
  from_scan?: boolean;
  block_id: string;
  start: number;
  end: number;
  quote: string;
  section_block_id: string | null;
  section_number: string | null;
  section_title: string | null;
  page: number | null;
  explanation: string;
  recommendation?: string | null;
  confidence: number | null;
  confidence_level: ConfidenceLevel | null;
  comment: string | null;
  author: Person | null;
  decided_by: Person | null;
  decided_at: string | null;
  can_delete: boolean;
  version: number;
  created_at?: string;
}

export interface DocumentView {
  id: string;
  batch: { id: string; name: string; subject?: string | null };
  filename: string;
  kind: DocumentKind;
  pages: number | null;
  status: DocumentStatus;
  stage: Stage;
  stage_label: string;
  progress: number;
  worst_level: WorstLevel;
  findings_summary: FindingsCount;
  review: Review;
  ocr_confidence?: number | null;
  ocr_low_quality_pages: number[];
  error: DocumentError | null;
  blocks_complete: boolean;
  blocks: Block[];
  findings: Finding[];
}

export interface DocumentRecalc {
  id?: string;
  worst_level: WorstLevel;
  findings_summary: FindingsCount;
  review: Review;
}

export interface BatchRecalc {
  id?: string;
  worst_level: WorstLevel;
  counters: BatchCounters;
  review: Review;
  findings?: FindingsCount;
}

export interface Recalc {
  document: DocumentRecalc;
  batch: BatchRecalc;
}

export interface FindingMutation extends Recalc {
  finding: Finding;
}

export interface FindingPatch {
  version: number;
  status?: FindingStatus;
  level?: Level;
  explanation?: string;
  comment?: string;
}

export interface NewUserFinding {
  block_id: string;
  start: number;
  end: number;
  level: Level;
  comment?: string;
}

export type HistoryAction = 'created' | 'accepted' | 'rejected' | 'edited' | 'commented' | 'reopened' | 'deleted';

export interface HistoryEvent {
  action: HistoryAction;
  actor: { full_name: string };
  old: Partial<Pick<Finding, 'status' | 'level' | 'explanation' | 'comment'>> | null;
  new: Partial<Pick<Finding, 'status' | 'level' | 'explanation' | 'comment'>> | null;
  created_at: string;
}

export interface BatchFinding extends Finding {
  document: { id: string; filename: string } | null;
}

export interface BatchFindingsParams {
  level?: Level;
  rule_id?: string;
  status?: FindingStatus;
  document_id?: string;
  page?: number;
  page_size?: number;
}

export interface SearchMatch {
  block_id: string;
  start: number;
  end: number;
  page: number;
}

// --- Экспорт --------------------------------------------------------------------------------

export interface ExportJob {
  export_id: string;
  format?: string;
}

export interface ExportStatus {
  export_id?: string;
  status: 'processing' | 'done' | 'error';
  download_url: string | null;
  filename: string | null;
  error_message: string | null;
}

// --- Правила --------------------------------------------------------------------------------

export interface RuleProfile {
  id: string;
  name: string;
  short_name: string;
  is_default: boolean;
  enabled_rules_count: number;
}

export type RuleKind = 'semantic_llm' | 'llm';
export type VariableType = 'text' | 'number' | 'list';

export interface RuleVariable {
  name: string;
  type: VariableType;
  value: string;
}

export interface SystemVariable {
  name: string;
  type: 'auto';
  description: string;
}

export interface RuleListItem {
  id: string;
  code: string;
  title: string;
  kind: RuleKind;
  level: Level;
  enabled: boolean;
  hits_30d: number;
}

export interface Rule extends RuleListItem {
  profile_id: string;
  description: string;
  prompt_template: string;
  variables: RuleVariable[];
  updated_at: string | null;
  updated_by: { full_name: string } | null;
  version: number;
  warnings?: string[];
}

export interface RuleInput {
  profile_id: string;
  code: string;
  title: string;
  description: string;
  level: Level;
  kind: RuleKind;
  prompt_template: string;
  variables: RuleVariable[];
  enabled: boolean;
}

export interface RuleTestFinding {
  quote: string;
  block_id: string;
  start: number;
  end: number;
  section_number: string | null;
  page?: number | null;
  level: Level;
  explanation: string;
  recommendation?: string | null;
  confidence_level: ConfidenceLevel | null;
}

export interface RuleTestResult {
  findings: RuleTestFinding[];
  duration_ms: number;
  /** Что модель рассмотрела и почему не отметила */
  considered?: { quote: string; reason: string; note?: string | null }[];
}

export interface RecentDocument {
  id: string;
  filename: string;
  batch: { id: string; name: string };
}

// --- SSE ------------------------------------------------------------------------------------

export interface DocumentProgressEvent {
  document_id: string;
  status: DocumentStatus;
  stage: Stage;
  stage_label: string;
  stage_detail: StageDetail | null;
  next_stage_label: string | null;
  progress: number;
}

export interface DocumentDoneEvent {
  document_id: string;
  pages: number | null;
  worst_level: WorstLevel;
  findings: FindingsCount;
  review: Review;
  ocr_low_quality_pages: number[];
}

export interface DocumentErrorEvent {
  document_id: string;
  error: DocumentError;
  retryable: boolean;
}

export type BatchEvent =
  | { type: 'batch.snapshot'; data: BatchView }
  | { type: 'batch.progress'; data: { status: BatchStatus; progress: BatchProgress; eta_seconds: number | null } }
  | { type: 'batch.meta'; data: { subject: string | null } }
  | { type: 'document.progress'; data: DocumentProgressEvent }
  | { type: 'document.done'; data: DocumentDoneEvent }
  | { type: 'document.error'; data: DocumentErrorEvent }
  | { type: 'batch.done'; data: { status: BatchStatus; worst_level: WorstLevel; counters: BatchCounters; review: Review } }
  | { type: 'export.done'; data: ExportStatus & { export_id: string } };

export const BATCH_EVENT_TYPES: BatchEvent['type'][] = [
  'batch.snapshot',
  'batch.progress',
  'batch.meta',
  'document.progress',
  'document.done',
  'document.error',
  'batch.done',
  'export.done',
];

// --- Ошибки ---------------------------------------------------------------------------------

export interface ApiErrorBody {
  error: { code: string; message: string; details?: Record<string, unknown> };
}
