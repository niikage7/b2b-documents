import { request, upload } from './http';
import type {
  AppConfig,
  BatchFinding,
  BatchFindingsParams,
  BatchList,
  BatchListParams,
  BatchView,
  Block,
  DocumentView,
  Draft,
  DraftFiles,
  ExportJob,
  ExportStatus,
  FindingMutation,
  FindingPatch,
  HistoryEvent,
  Me,
  NewUserFinding,
  PackageAnswer,
  PackageBrief,
  Page,
  Recalc,
  RecentDocument,
  Rule,
  RuleDraft,
  RuleInput,
  RuleListItem,
  RuleProfile,
  RuleTestResult,
  SearchMatch,
  SystemVariable,
} from './types';

// Один вызов — один эндпоинт из «Требований к бэкенду». Пути без префикса /api.

export const api = {
  // Авторизация и настройки
  login: (login: string, password: string) =>
    request<Me>('POST', '/auth/login', { body: { login, password }, skipAuthRedirect: true }),
  logout: () => request<void>('POST', '/auth/logout', { skipAuthRedirect: true }),
  me: () => request<Me>('GET', '/me', { skipAuthRedirect: true }),
  config: () => request<AppConfig>('GET', '/config'),

  // Пакеты
  listBatches: (params: BatchListParams) => request<BatchList>('GET', '/batches', { query: { ...params } }),
  createDraft: () => request<Draft>('POST', '/batches'),
  uploadFiles: (batchId: string, files: File[], onProgress?: (loaded: number, total: number) => void) =>
    upload<DraftFiles>(`/batches/${batchId}/files`, files, onProgress),
  removeFile: (batchId: string, fileId: string) =>
    request<DraftFiles>('DELETE', `/batches/${batchId}/files/${fileId}`),
  startBatch: (batchId: string, name: string, profileId: string) =>
    request<{ id: string; status: string }>('POST', `/batches/${batchId}/start`, {
      body: { name, profile_id: profileId },
    }),
  getBatch: (batchId: string) => request<BatchView>('GET', `/batches/${batchId}`),
  updateBatch: (batchId: string, patch: { name?: string; subject?: string }) =>
    request<BatchView>('PATCH', `/batches/${batchId}`, { body: patch }),
  deleteBatch: (batchId: string) => request<void>('DELETE', `/batches/${batchId}`),
  rerunBatch: (batchId: string) => request<void>('POST', `/batches/${batchId}/rerun`),
  batchFindings: (batchId: string, params: BatchFindingsParams) =>
    request<Page<BatchFinding>>('GET', `/batches/${batchId}/findings`, { query: { ...params } }),

  askBatch: (batchId: string, question: string) =>
    request<PackageAnswer>('POST', `/batches/${batchId}/ask`, { body: { question } }),
  batchBrief: (batchId: string) => request<PackageBrief>('POST', `/batches/${batchId}/brief`),
  clarification: (batchId: string) => request<ExportJob>('POST', `/batches/${batchId}/clarification`),

  // Документы
  getDocument: (documentId: string) => request<DocumentView>('GET', `/documents/${documentId}`),
  documentBlocks: (documentId: string, fromPage: number, toPage: number) =>
    request<{ blocks: Block[] }>('GET', `/documents/${documentId}/blocks`, {
      query: { from_page: fromPage, to_page: toPage },
    }),
  searchDocument: (documentId: string, q: string) =>
    request<{ matches: SearchMatch[] }>('GET', `/documents/${documentId}/search`, { query: { q } }),
  retryDocument: (documentId: string) => request<void>('POST', `/documents/${documentId}/retry`),
  recentDocuments: () =>
    request<RecentDocument[]>('GET', '/documents', { query: { status: 'done', limit: 20 } }),

  // Находки
  updateFinding: (findingId: string, patch: FindingPatch) =>
    request<FindingMutation>('PATCH', `/findings/${findingId}`, { body: patch }),
  createFinding: (documentId: string, body: NewUserFinding) =>
    request<FindingMutation>('POST', `/documents/${documentId}/findings`, { body }),
  deleteFinding: (findingId: string) => request<Recalc | undefined>('DELETE', `/findings/${findingId}`),
  findingHistory: (findingId: string) =>
    request<{ events: HistoryEvent[] }>('GET', `/findings/${findingId}/history`),

  // Экспорт
  exportDocument: (documentId: string) =>
    request<ExportJob>('POST', `/documents/${documentId}/exports`, { body: { include_rejected: false } }),
  exportBatch: (batchId: string) =>
    request<ExportJob>('POST', `/batches/${batchId}/exports`, { body: { include_rejected: false } }),
  getExport: (exportId: string) => request<ExportStatus>('GET', `/exports/${exportId}`),

  // Профили и правила
  listProfiles: () => request<RuleProfile[]>('GET', '/rule-profiles'),
  createProfile: (body: { name: string; short_name: string; copy_from_profile_id?: string }) =>
    request<RuleProfile>('POST', '/rule-profiles', { body }),
  updateProfile: (profileId: string, body: { name?: string; short_name?: string; is_default?: boolean }) =>
    request<RuleProfile>('PATCH', `/rule-profiles/${profileId}`, { body }),
  deleteProfile: (profileId: string) => request<void>('DELETE', `/rule-profiles/${profileId}`),
  listRules: (params: { profile_id: string; search?: string; kind?: string }) =>
    request<RuleListItem[]>('GET', '/rules', { query: params }),
  getRule: (ruleId: string) => request<Rule>('GET', `/rules/${ruleId}`),
  createRule: (body: RuleInput) => request<Rule>('POST', '/rules', { body }),
  updateRule: (ruleId: string, body: Partial<RuleInput> & { version: number }) =>
    request<Rule>('PATCH', `/rules/${ruleId}`, { body }),
  deleteRule: (ruleId: string) => request<void>('DELETE', `/rules/${ruleId}`),
  draftRule: (body: { description: string; profile_id?: string }) => request<RuleDraft>('POST', '/rules/draft', { body }),
  systemVariables: () => request<SystemVariable[]>('GET', '/rules/system-variables'),
  testRule: (body: { rule: RuleInput; document_id?: string; text?: string }, signal?: AbortSignal) =>
    request<RuleTestResult>('POST', '/rules/test', { body, signal }),
};
