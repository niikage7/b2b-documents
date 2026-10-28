// Проверка правила, как в ml/analysis/templates.py (validate_rule): редактор подсвечивает ошибки
// до сохранения, а мок API отвечает теми же 422. Окончательно правило проверяет бэкенд.

import type { RuleInput } from '../api/types';

export const SYSTEM_VARIABLE_NAMES = ['фрагмент', 'предмет_закупки'];

const VAR = /\{([^{}\s]{1,40})\}/g;
const NAME = /^[\wа-яА-ЯёЁ]{1,40}$/;
const NUMBER = /^-?\d+(?:[.,]\d+)?$/;

export function templateVariables(template: string): string[] {
  return [...new Set([...template.matchAll(VAR)].map((m) => m[1]))];
}

export interface RuleCheck {
  fields: Record<string, string>;
  warnings: string[];
}

export function validateRule(rule: Pick<RuleInput, 'code' | 'title' | 'description' | 'level' | 'kind' | 'prompt_template' | 'variables'>): RuleCheck {
  const fields: Record<string, string> = {};
  const set = (field: string, message: string) => {
    if (!(field in fields)) fields[field] = message;
  };
  if (!rule.code.trim()) set('code', 'Поле нужно заполнить');
  if (!rule.title.trim()) set('title', 'Поле нужно заполнить');
  if (!rule.description.trim()) set('description', 'Поле нужно заполнить');

  const seen = new Set<string>();
  rule.variables.forEach((v, i) => {
    if (!NAME.test(v.name)) set(`variables[${i}].name`, 'Имя — буквы, цифры и «_», от 1 до 40 символов');
    else if (SYSTEM_VARIABLE_NAMES.includes(v.name)) set(`variables[${i}].name`, `Имя «${v.name}» занято системной переменной`);
    else if (seen.has(v.name)) set(`variables[${i}].name`, `Переменная «${v.name}» уже описана`);
    seen.add(v.name);
    const value = v.value.trim();
    if (v.type === 'number' && !NUMBER.test(value)) set(`variables[${i}].value`, 'Нужно число, например 10 или 0,5');
    else if (v.type === 'list' && !value.split(';').some((x) => x.trim())) set(`variables[${i}].value`, 'Список — значения через точку с запятой');
    else if (v.type === 'text' && v.value.length > 2000) set(`variables[${i}].value`, 'Текст — не длиннее 2000 символов');
  });

  const used = templateVariables(rule.prompt_template);
  if (!rule.prompt_template.trim()) set('prompt_template', 'Инструкция для ИИ не заполнена');
  for (const name of used) {
    if (!seen.has(name) && !SYSTEM_VARIABLE_NAMES.includes(name)) set('prompt_template', `Переменная {${name}} не описана`);
  }
  if (rule.kind === 'semantic_llm' && !used.includes('фрагмент')) {
    set('prompt_template', 'В инструкции смыслового правила нужна переменная {фрагмент}');
  }
  const warnings = [...seen].filter((name) => !used.includes(name)).map((name) => `Переменная {${name}} описана, но не используется в инструкции`);
  return { fields, warnings };
}
