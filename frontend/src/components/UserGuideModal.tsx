import {
  ArrowLeft,
  ArrowRight,
  Briefcase,
  Check,
  CheckCircle2,
  Download,
  FileSpreadsheet,
  FileText,
  Layers,
  RefreshCw,
  Scale,
  ShieldAlert,
  Sparkles,
  UploadCloud,
  X,
} from 'lucide-react';
import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate } from 'react-router';
import { api } from '../api/endpoints';
import { Button } from './ui';

export interface UserGuideModalProps {
  isOpen: boolean;
  onClose: () => void;
  initialStep?: number;
}

interface GuideStep {
  tag: string;
  title: string;
  subtitle: string;
  icon: typeof Sparkles;
  render: () => React.ReactNode;
}

export function UserGuideModal({ isOpen, onClose, initialStep = 0 }: UserGuideModalProps) {
  const [step, setStep] = useState(initialStep);
  const [loadingDemo, setLoadingDemo] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    if (isOpen) {
      setStep(initialStep);
    }
  }, [isOpen, initialStep]);

  useEffect(() => {
    if (!isOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
      if (e.key === 'ArrowRight' && step < 6) setStep((s) => s + 1);
      if (e.key === 'ArrowLeft' && step > 0) setStep((s) => s - 1);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [isOpen, step, onClose]);

  if (!isOpen) return null;

  const openDemoBatch = async () => {
    setLoadingDemo(true);
    try {
      const list = await api.listBatches({ page: 1, page_size: 10 });
      const demo = list.items.find((b) => b.name.includes('0001')) ?? list.items[0];
      if (demo) {
        navigate(`/batches/${demo.id}`);
      } else {
        navigate('/batches');
      }
    } catch {
      navigate('/batches');
    } finally {
      setLoadingDemo(false);
      onClose();
    }
  };

  const steps: GuideStep[] = [
    {
      tag: 'Знакомство',
      title: 'Добро пожаловать в B2B Tender',
      subtitle: 'Интеллектуальная система проверки закупочной документации на правовые и финансовые риски.',
      icon: Sparkles,
      render: () => (
        <div className="guide-slide">
          <p className="guide-slide__lead">
            Система автоматически извлекает текст, проводит OCR сканированных файлов, сверяет условия по чек-листу правил 44-ФЗ и
            выделяет риски в формате наглядного светофора.
          </p>
          <div className="guide-roles">
            <div className="guide-role-card">
              <div className="guide-role-card__header">
                <Briefcase size={20} className="text-primary" />
                <b>Специалист</b>
              </div>
              <p>Быстрая загрузка пакетов документации, контроль стадий конвейера и скачивание готовых отчётов.</p>
            </div>
            <div className="guide-role-card">
              <div className="guide-role-card__header">
                <Scale size={20} className="text-primary" />
                <b>Юрист</b>
              </div>
              <p>Анализ выносок на полях, принятие или отклонение рисков, правовые комментарии и ручная пометка.</p>
            </div>
            <div className="guide-role-card">
              <div className="guide-role-card__header">
                <Layers size={20} className="text-primary" />
                <b>Методолог</b>
              </div>
              <p>Настройка профилей правил (44-ФЗ, 223-ФЗ), изменение шаблонов промптов и переменных порогов.</p>
            </div>
          </div>
        </div>
      ),
    },
    {
      tag: 'Принцип светофора',
      title: 'Цветовая оценка условий и рисков',
      subtitle: 'Каждый найденный пункт и документ оцениваются по строгому правилу светофора.',
      icon: ShieldAlert,
      render: () => (
        <div className="guide-slide">
          <div className="guide-levels">
            <div className="guide-level-card guide-level-card--critical">
              <div className="guide-level-card__badge">
                <span className="dot dot--critical" /> Критично
              </div>
              <p>
                Блокирующие риски: угроза попадания в РНП, кабальные неустойки (пени 1% в день), нереальные сроки поставки (1–3 дня).
              </p>
            </div>
            <div className="guide-level-card guide-level-card--warning">
              <div className="guide-level-card__badge">
                <span className="dot dot--warning" /> Внимание
              </div>
              <p>
                Спорные условия для оценки юристом: расходы на экспертизу на поставщике, гарантийный срок более 36 месяцев, сложная приёмка.
              </p>
            </div>
            <div className="guide-level-card guide-level-card--ok">
              <div className="guide-level-card__badge">
                <span className="dot dot--ok" /> Без рисков
              </div>
              <p>
                Условия соответствуют типовым нормам 44-ФЗ и стандартной практике закупок.
              </p>
            </div>
          </div>
          <div className="note note--card" style={{ marginTop: 14 }}>
            <b>Правило худшего уровня:</b> цвет документа равен наихудшему активному риску внутри него. Если юрист отклоняет находку, цвет пересчитывается автоматически!
          </div>
        </div>
      ),
    },
    {
      tag: 'Загрузка',
      title: 'Быстрая загрузка пакета документов',
      subtitle: 'Поддержка архивов и отдельных файлов до 100 МБ в один клик.',
      icon: UploadCloud,
      render: () => (
        <div className="guide-slide">
          <div className="guide-features">
            <div className="guide-feature">
              <div className="guide-feature__icon">📁</div>
              <div>
                <b>Все форматы закупки</b>
                <p>Поддерживаются DOCX, PDF (включая сканированные копии), DOC, RTF и единые ZIP-архивы со структурой папок.</p>
              </div>
            </div>
            <div className="guide-feature">
              <div className="guide-feature__icon">⚙️</div>
              <div>
                <b>Профили правил</b>
                <p>Выберите нужный чек-лист при загрузке: «44-ФЗ, поставка товаров», «44-ФЗ, работы и услуги» или «223-ФЗ».</p>
              </div>
            </div>
            <div className="guide-feature">
              <div className="guide-feature__icon">🛡️</div>
              <div>
                <b>Упреждающая валидация</b>
                <p>Система мгновенно проверяет лимит 100 МБ и сообщает о повреждённых или запароленных файлах до старта.</p>
              </div>
            </div>
          </div>
        </div>
      ),
    },
    {
      tag: 'Конвейер',
      title: 'Ход обработки и отказоустойчивость',
      subtitle: 'Прозрачное отслеживание в реальном времени и изолированные сбои.',
      icon: RefreshCw,
      render: () => (
        <div className="guide-slide">
          <div className="guide-features">
            <div className="guide-feature">
              <div className="guide-feature__icon">⚡</div>
              <div>
                <b>Прогресс в реальном времени (SSE)</b>
                <p>Наглядные этапы: «В очереди» → «Извлечение текста» → «OCR сканов (постранично)» → «Анализ правил ИИ».</p>
              </div>
            </div>
            <div className="guide-feature">
              <div className="guide-feature__icon">🛡️</div>
              <div>
                <b>Изоляция ошибок</b>
                <p>Сбой или повреждение одного файла не останавливает пакет: остальные документы продолжают обрабатываться в штатном режиме.</p>
              </div>
            </div>
            <div className="guide-feature">
              <div className="guide-feature__icon">🔄</div>
              <div>
                <b>Повтор в один клик</b>
                <p>Кнопки «Повторить» и «Повторить все с ошибкой» перезапускают сбойные файлы ровно с того этапа, на котором произошла ошибка.</p>
              </div>
            </div>
          </div>
        </div>
      ),
    },
    {
      tag: 'Разбор пакета',
      title: 'Сводка пакета и Паспорт закупки',
      subtitle: 'Полная картина рисков закупки на едином экране.',
      icon: Layers,
      render: () => (
        <div className="guide-slide">
          <div className="guide-features">
            <div className="guide-feature">
              <div className="guide-feature__icon">📊</div>
              <div>
                <b>Умная сортировка документов</b>
                <p>Документы автоматически ранжируются от опасных к безопасным: критичные файлы всегда отображаются первыми в списке.</p>
              </div>
            </div>
            <div className="guide-feature">
              <div className="guide-feature__icon">📑</div>
              <div>
                <b>Паспорт закупки</b>
                <p>Автоматически извлекаются предмет закупки, НМЦК, размер обеспечения заявки и контракта, требования к гарантии.</p>
              </div>
            </div>
            <div className="guide-feature">
              <div className="guide-feature__icon">🎯</div>
              <div>
                <b>Сработавшие правила и все находки</b>
                <p>Фильтрация документов по конкретным статьям и правилам в 1 клик для быстрой подготовки протокола разногласий.</p>
              </div>
            </div>
          </div>
        </div>
      ),
    },
    {
      tag: 'Редактор',
      title: 'Редактор документа — рабочее место юриста',
      subtitle: 'Точные цитаты, выноски на полях и принятие решений.',
      icon: FileText,
      render: () => (
        <div className="guide-slide">
          <div className="guide-features">
            <div className="guide-feature">
              <div className="guide-feature__icon">🔍</div>
              <div>
                <b>Синхронные выноски на полях</b>
                <p>Цветовая подсветка цитат в тексте с точной ссылкой на пункт и страницу (например, «п. 3.1, стр. 5»).</p>
              </div>
            </div>
            <div className="guide-feature">
              <div className="guide-feature__icon">⚖️</div>
              <div>
                <b>Принятие и отклонение находок</b>
                <p>Кнопки «Принять» (галочка) и «Отклонить» (крестик). При отклонении цвет документа пересчитывается мгновенно.</p>
              </div>
            </div>
            <div className="guide-feature">
              <div className="guide-feature__icon">✏️</div>
              <div>
                <b>Ручная пометка и горячие клавиши</b>
                <p>Выделите текст мышкой для ручной пометки риска. Горячие клавиши: <span className="kbd">J</span> / <span className="kbd">K</span> — навигация, <span className="kbd">Ctrl+Enter</span> — сохранение.</p>
              </div>
            </div>
          </div>
        </div>
      ),
    },
    {
      tag: 'Отчёты и Демо',
      title: 'Выгрузка отчётов и Демонстрационный пакет',
      subtitle: 'Готовые документы для работы и быстрое тестирование прямо сейчас.',
      icon: FileSpreadsheet,
      render: () => (
        <div className="guide-slide">
          <div className="guide-features" style={{ marginBottom: 16 }}>
            <div className="guide-feature">
              <div className="guide-feature__icon">📄</div>
              <div>
                <b>Размеченный Word (DOCX) и PDF</b>
                <p>Экспорт документа с сохранением форматирования, цветной заливкой и встроенными комментариями Word на полях.</p>
              </div>
            </div>
            <div className="guide-feature">
              <div className="guide-feature__icon">📦</div>
              <div>
                <b>Пакет ZIP со сводкой Excel</b>
                <p>Архив со всеми файлами закупки и аналитической таблицей «Сводка.xlsx» для руководства и тендерного комитета.</p>
              </div>
            </div>
          </div>
          <div className="guide-demo-box">
            <div>
              <b>Попробуйте систему прямо сейчас:</b>
              <p className="muted" style={{ margin: '4px 0 10px', fontSize: 13 }}>
                В системе уже подготовлен пакет «Закупка_0001» (поставка серверного оборудования) с готовыми рисками.
              </p>
            </div>
            <div className="row" style={{ gap: 10, flexWrap: 'wrap' }}>
              <Button variant="primary" onClick={() => void openDemoBatch()} loading={loadingDemo}>
                <CheckCircle2 size={16} /> Открыть демо-пакет
              </Button>
              <a href="/demo-package.zip" download="demo-package.zip" className="btn btn--secondary">
                <Download size={16} /> Скачать тестовый архив (.zip)
              </a>
            </div>
          </div>
        </div>
      ),
    },
  ];

  const current = steps[step];
  const StepIcon = current.icon;

  return createPortal(
    <div className="guide-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="guide-modal" role="dialog" aria-modal aria-labelledby="guide-title">
        <header className="guide-header">
          <div className="row" style={{ gap: 10 }}>
            <div className="guide-header__icon">
              <StepIcon size={20} />
            </div>
            <div>
              <span className="badge badge--neutral">{current.tag}</span>
              <span className="guide-header__counter">Шаг {step + 1} из {steps.length}</span>
            </div>
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Закрыть руководство">
            <X size={20} />
          </button>
        </header>

        <div className="guide-body">
          <h2 id="guide-title" className="guide-title">
            {current.title}
          </h2>
          <p className="guide-subtitle">{current.subtitle}</p>
          <div className="guide-content">{current.render()}</div>
        </div>

        <footer className="guide-footer">
          <div className="guide-dots" role="tablist" aria-label="Шаги руководства">
            {steps.map((s, idx) => (
              <button
                key={s.title}
                type="button"
                className={idx === step ? 'guide-dot guide-dot--active' : 'guide-dot'}
                onClick={() => setStep(idx)}
                aria-label={`Перейти к шагу ${idx + 1}: ${s.title}`}
                aria-pressed={idx === step}
              />
            ))}
          </div>

          <div className="guide-actions">
            {step > 0 && (
              <Button onClick={() => setStep((s) => s - 1)}>
                <ArrowLeft size={16} /> Назад
              </Button>
            )}
            {step < steps.length - 1 ? (
              <>
                <Button variant="ghost" onClick={onClose}>
                  Пропустить
                </Button>
                <Button variant="primary" onClick={() => setStep((s) => s + 1)}>
                  Далее <ArrowRight size={16} />
                </Button>
              </>
            ) : (
              <Button variant="primary" onClick={onClose}>
                <Check size={16} /> Завершить и начать работу
              </Button>
            )}
          </div>
        </footer>
      </div>
    </div>,
    document.body,
  );
}
