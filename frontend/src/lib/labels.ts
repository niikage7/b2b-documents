// Подписи кодов API. Бэкенд отдаёт коды, подписи и цвета выбирает фронтенд.

import type {
  ConfidenceLevel,
  DocumentKind,
  FindingStatus,
  HistoryAction,
  Level,
  RuleKind,
  VariableType,
  WorstLevel,
} from '../api/types';

export const LEVEL_LABELS: Record<Level, string> = { critical: 'Критично', warning: 'Внимание' };

export type Tone = 'critical' | 'warning' | 'ok' | 'neutral';

export function worstLevelTone(level: WorstLevel): Tone {
  if (level === 'critical' || level === 'warning' || level === 'ok') return level;
  return 'neutral';
}

export function worstLevelLabel(level: WorstLevel): string {
  switch (level) {
    case 'critical':
      return 'Критично';
    case 'warning':
      return 'Внимание';
    case 'ok':
      return 'Без рисков';
    case 'not_checked':
      return 'Не проверен';
    default:
      return 'Обрабатывается';
  }
}

export const FINDING_STATUS_LABELS: Record<FindingStatus, string> = {
  new: 'Новая',
  accepted: 'Принята',
  rejected: 'Отклонена',
  edited: 'Изменена',
};

export const RULE_KIND_LABELS: Record<RuleKind, string> = {
  semantic_llm: 'Смысловой поиск + проверка ИИ',
  llm: 'Проверка ИИ',
};

export const RULE_KIND_HINTS: Record<RuleKind, string> = {
  semantic_llm: 'Система находит в документе похожие по смыслу фрагменты, ИИ проверяет каждый из них по инструкции.',
  llm: 'ИИ читает документ целиком. Медленнее и дороже: держите таких правил не больше пяти.',
};

export const VARIABLE_TYPE_LABELS: Record<VariableType | 'auto', string> = {
  auto: 'Авто',
  text: 'Текст',
  number: 'Число',
  list: 'Список',
};

export const CONFIDENCE_LABELS: Record<ConfidenceLevel, string> = { high: 'высокая', medium: 'средняя', low: 'низкая' };

export const CONFIDENCE_HINTS: Record<ConfidenceLevel, string> = {
  high: 'ИИ уверен в находке: цитата прямо подходит под правило',
  medium: 'ИИ сомневается: проверьте фрагмент внимательнее',
  low: 'ИИ почти не уверен: возможна ложная находка',
};

export function documentTypeLabel(kind: DocumentKind): string {
  return kind === 'scan' ? 'Скан' : kind.toUpperCase();
}

export const HISTORY_ACTION_LABELS: Record<HistoryAction, string> = {
  created: 'создал',
  accepted: 'принял',
  rejected: 'отклонил',
  edited: 'изменил',
  commented: 'прокомментировал',
  reopened: 'отменил решение',
  deleted: 'удалил',
};
