// Форматирование для интерфейса: даты в часовом поясе пользователя, размеры, склонения.

const dateFmt = new Intl.DateTimeFormat('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' });
const timeFmt = new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit' });

export function formatDate(iso: string | null | undefined): string {
  return iso ? dateFmt.format(new Date(iso)) : '—';
}

export function formatTime(iso: string | null | undefined): string {
  return iso ? timeFmt.format(new Date(iso)) : '';
}

export function formatDateTime(iso: string | null | undefined): string {
  return iso ? `${formatDate(iso)}, ${formatTime(iso)}` : '—';
}

/** «сегодня, 11:02», «вчера, 16:20» или «03.10.2026, 16:20». */
export function formatWhen(iso: string | null | undefined): string {
  if (!iso) return '';
  const date = new Date(iso);
  const today = new Date();
  const yesterday = new Date(today.getTime() - 86400_000);
  const same = (a: Date, b: Date) => a.toDateString() === b.toDateString();
  if (same(date, today)) return `сегодня, ${formatTime(iso)}`;
  if (same(date, yesterday)) return `вчера, ${formatTime(iso)}`;
  return formatDateTime(iso);
}

const numberFmt = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 });

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${numberFmt.format(bytes / 1024)} КБ`;
  return `${numberFmt.format(bytes / (1024 * 1024))} МБ`;
}

/** Мегабайты без единицы: «34,2» для счётчика «34,2 МБ из 100». */
export function formatMegabytes(bytes: number): string {
  const mb = bytes / (1024 * 1024);
  return mb > 0 && mb < 0.1 ? '< 0,1' : numberFmt.format(mb);
}

/** plural(3, ['файл', 'файла', 'файлов']) → «файла». */
export function plural(n: number, forms: [string, string, string]): string {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return forms[0];
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return forms[1];
  return forms[2];
}

export function countLabel(n: number, forms: [string, string, string]): string {
  return `${n} ${plural(n, forms)}`;
}

/** «осталось около 40 секунд», «осталось около 3 минут». */
export function formatEta(seconds: number | null | undefined): string | null {
  if (!seconds || seconds <= 0) return null;
  if (seconds < 60) {
    const rounded = Math.max(5, Math.round(seconds / 5) * 5);
    return `осталось около ${countLabel(rounded, ['секунды', 'секунд', 'секунд'])}`;
  }
  const minutes = Math.round(seconds / 60);
  return `осталось около ${countLabel(minutes, ['минуты', 'минут', 'минут'])}`;
}

export function fileExtension(filename: string): string {
  const i = filename.lastIndexOf('.');
  return i >= 0 ? filename.slice(i + 1).toLowerCase() : '';
}
