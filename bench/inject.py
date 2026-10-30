"""Полнота на настоящих документах: риски вносит программа, эталон пишется вместе с правкой.

В трёх открытых пакетах одна обязательная находка: условия близки к типовым, поэтому полноту на них
не посчитать, а на синтетическом пакете автор проверки сам пишет и риски, и правила. Стенд берёт
настоящие документы, меняет в них значения условий (оплата за 60 рабочих дней вместо 7) и добавляет
пункты с нарушениями, записывая эталон в момент правки. Находки сверяются с этим эталоном:

- полнота — сколько внесённых рисков нашлось;
- лишние — находки вне эталона; в исходных пакетах таких условий почти нет, поэтому это почти всегда
  ложные срабатывания (известные настоящие условия из `expected.json` пакета в них не считаются).

    python -m bench.inject                                  # все пакеты из .cache/packages
    python -m bench.inject --package 0338200009926000090
    python -m bench.inject --only R-02,R-07 --save out.json

Эталон не подгоняется под движок: найденная ошибка эталона исправляется здесь, в правке. Около $0,01
на пакет, повторный прогон без изменений идёт из кеша LLM.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

from bench.pipeline import quotes_match
from contracts.analysis import FindingCandidate, LLMUsage
from ml.analysis.batch_meta import extract_batch_meta
from ml.analysis.blocks import blocks_from_extraction, chunk_blocks
from ml.analysis.embeddings import default_embedder
from ml.analysis.engine import analyze_rule_group, group_rules
from ml.analysis.search import InMemorySearch
from ml.checklist import load_default_checklist
from ml.convert import convert_to_docx
from ml.extract import extract_document

PACKAGES = Path(".cache/packages")
EXPECTED = Path("bench/packages")


@dataclass(frozen=True)
class Injection:
    """Одна правка. `find` — абзац, который меняется или после которого вставляется новый; `text` — новый
    текст абзаца; `quote` — что должно попасть в цитату. Если абзаца нет, `text` идёт после `fallback`."""

    rule: str
    doc: str
    find: str
    text: str
    quote: str
    replace: bool = True
    fallback: str = r"поставк|товар"


INJECTIONS: tuple[Injection, ...] = (
    Injection("R-02", "contract", r"обеспечени[ея]\s+исполнения.*\d\s*(%|процент)",
              "Обеспечение исполнения Контракта устанавливается в размере 25 % от начальной (максимальной) цены контракта.",
              "в размере 25 % от начальной (максимальной) цены контракта"),
    Injection("R-03", "contract", r"^Пеня начисляется за каждый день просрочки|пен[ия].*(одной трехсотой|одной трёхсотой|1/300).*Поставщик",
              "Пеня начисляется за каждый день просрочки исполнения Поставщиком обязательства в размере 1 % от цены "
              "Контракта.",
              "в размере 1 % от цены Контракта"),
    Injection("R-04", "contract", r"^Срок поставки",
              "Срок поставки Товара по настоящему Контракту — в течение 2 (двух) календарных дней с даты заключения "
              "настоящего Контракта.",
              "в течение 2 (двух) календарных дней с даты заключения"),
    Injection("R-05", "contract", r"принять решение об одностороннем отказе|одностороннем порядке",
              "Заказчик вправе в одностороннем порядке отказаться от исполнения Контракта при любом, в том числе "
              "однократном и незначительном, нарушении Поставщиком условий Контракта.",
              "при любом, в том числе однократном и незначительном, нарушении", replace=False),
    Injection("R-06", "requirements", r"заявк",
              "Участник закупки должен иметь статус авторизованного партнёра производителя товара и подтвердить его "
              "письмом производителя в составе заявки, а также опыт исполнения не менее пяти аналогичных контрактов.",
              "статус авторизованного партнёра производителя", replace=False, fallback=r"."),
    Injection("R-07", "contract", r"(оплат|расчет|Расчёт).*в течение.*рабочих дн",
              "Заказчик оплачивает Поставщику поставленный Товар в течение 60 (шестидесяти) рабочих дней с даты "
              "подписания Заказчиком документа о приемке.",
              "в течение 60 (шестидесяти) рабочих дней"),
    Injection("R-07", "contract", r"экспертиз",
              "Расходы на проведение экспертизы поставленного Товара во всех случаях несёт Поставщик независимо от её "
              "результатов.",
              "во всех случаях несёт Поставщик независимо от её результатов", replace=False),
    Injection("R-08", "contract", r"гаранти",
              "Гарантийный срок на Товар составляет 72 (семьдесят два) месяца с даты подписания документа о приемке.",
              "72 (семьдесят два) месяца", replace=False),
    Injection("R-01", "spec", r"товар|поставк|характеристик",
              "Кресло руководителя Chairman 420, артикул 6001234, производитель ООО «Чермет» — 10 штук; поставка "
              "товара другой марки не допускается.",
              "Кресло руководителя Chairman 420, артикул 6001234", replace=False, fallback=r"."),
    # Трудные: другими словами, без терминов из правила, и значения чуть выше порога
    Injection("R-02", "contract", r"банковск|независим|гаранти",
              "Поставщик до заключения Контракта перечисляет на счёт Заказчика в подтверждение надлежащего исполнения "
              "своих обязательств денежные средства в сумме 15 процентов цены Контракта.",
              "денежные средства в сумме 15 процентов цены Контракта", replace=False),
    Injection("R-03", "contract", r"ответственност|штраф",
              "За каждые сутки задержки передачи Товара Поставщик выплачивает Заказчику 0,3 % стоимости Контракта.",
              "выплачивает Заказчику 0,3 % стоимости Контракта", replace=False),
    Injection("R-04", "contract", r"заявк|партии|доставк",
              "Товар доставляется Заказчику на следующий день после получения его письменной заявки.",
              "на следующий день после получения его письменной заявки", replace=False),
    Injection("R-05", "contract", r"расторжени|расторгнут",
              "Если Поставщик хотя бы один раз задержит передачу партии Товара, Заказчик прекращает действие Контракта "
              "без обращения в суд.",
              "хотя бы один раз задержит передачу партии Товара", replace=False),
    Injection("R-07", "contract", r"приемк|приёмк",
              "Заказчик рассматривает поставленный Товар и документы на него в течение 35 рабочих дней, после чего "
              "подписывает документ о приемке.",
              "в течение 35 рабочих дней", replace=False),
    Injection("R-07", "contract", r"оплат|расчет",
              "Денежные средства перечисляются Поставщику не позднее 10 рабочих дней после подписания документа о "
              "приемке.",
              "не позднее 10 рабочих дней после подписания", replace=False),
    Injection("R-08", "contract", r"качеств|недостатк",
              "Поставщик отвечает за недостатки Товара в течение 5 лет с момента его передачи Заказчику.",
              "в течение 5 лет с момента его передачи", replace=False),
)


def _set_text(paragraph, text: str) -> None:
    runs = list(paragraph.iter(qn("w:r")))
    for extra in paragraph.iter(qn("w:ins"), qn("w:del")):
        extra.getparent().remove(extra)
        runs = list(paragraph.iter(qn("w:r")))
        break
    keep = runs[0] if runs else None
    for run in runs[1:]:
        run.getparent().remove(run)
    if keep is None:
        keep = paragraph.makeelement(qn("w:r"), {})
        paragraph.append(keep)
    for child in list(keep):
        if child.tag != qn("w:rPr"):
            keep.remove(child)
    node = keep.makeelement(qn("w:t"), {qn("xml:space"): "preserve"})
    node.text = text
    keep.append(node)


def _text(paragraph) -> str:
    return "".join(t.text or "" for t in paragraph.iter(qn("w:t"))).strip()


def _apply(body, item: Injection) -> str:
    """Правка в XML документа; возвращает, что сделано — для отчёта и разбора промахов."""
    paragraphs = [p for p in body.iter(qn("w:p")) if _text(p)]
    body_only = [p for p in paragraphs if not any(a.tag == qn("w:tc") for a in p.iterancestors())]
    target = next((p for p in body_only if re.search(item.find, _text(p), re.I)), None)
    if target is None:
        target = next((p for p in paragraphs if re.search(item.find, _text(p), re.I)), None)
    if target is not None and item.replace:
        before = _text(target)
        _set_text(target, item.text)
        return f"заменён: {before[:90]}"
    anchor = target if target is not None else next(
        (p for p in body_only if re.search(item.fallback, _text(p), re.I)), None)
    if anchor is None:
        anchor = paragraphs[0]
    fresh = copy.deepcopy(anchor)
    _set_text(fresh, item.text)
    anchor.addnext(fresh)
    return f"вставлен после: {_text(anchor)[:90]}"


def inject_package(package: Path, workdir: Path, *, only: set[str] | None = None) -> tuple[list[Path], list[dict]]:
    """Копии документов пакета с правками и эталон [{rule, doc, quote}]."""
    paths: dict[str, Path] = {}
    for src in sorted(package.iterdir()):
        docx = Path(convert_to_docx(src)) if src.suffix.lower() == ".doc" else src
        paths[src.stem] = docx
    expected: list[dict] = []
    opened: dict[str, Document] = {}
    for item in INJECTIONS:
        if only and item.rule not in only or item.doc not in paths:
            continue
        doc = opened.setdefault(item.doc, Document(str(paths[item.doc])))
        how = _apply(doc.element.body, item)
        print(f"  {item.rule} {item.doc}: {how}")
        expected.append({"rule": item.rule, "doc": item.doc, "quote": item.quote, "text": item.text})
    out = []
    for stem, path in paths.items():
        target = workdir / f"{stem}.docx"
        if stem in opened:
            opened[stem].save(str(target))
        else:
            target.write_bytes(path.read_bytes())
        out.append(target)
    return out, expected


def analyse(paths: list[Path], *, subject: str | None = None) -> tuple[dict[str, list[FindingCandidate]], LLMUsage, str | None]:
    embedder = default_embedder()
    search = InMemorySearch()
    docs = {}
    for path in paths:
        blocks = blocks_from_extraction(extract_document(path))
        docs[path.stem] = blocks
        search.add(path.stem, chunk_blocks(blocks), embedder=embedder)
    subject = subject or extract_batch_meta([(n, b) for n, b in docs.items()])
    _, rules = load_default_checklist()
    groups = group_rules(rules)
    usage = LLMUsage()
    found: dict[str, list[FindingCandidate]] = {}
    for name, blocks in docs.items():
        found[name] = []
        for group in groups:
            result = analyze_rule_group(blocks, group, subject=subject, document_id=name, search=search,
                                        embedder=embedder)
            found[name] += result.candidates
            usage.add(result.usage)
    return found, usage, subject


def report(package: str, found: dict[str, list[FindingCandidate]], expected: list[dict], known: list[dict]) -> dict:
    hits, missed = [], []
    for e in expected:
        ok = any(c.rule_code == e["rule"] and quotes_match(e["quote"], c.quote) for c in found.get(e["doc"], []))
        (hits if ok else missed).append(e)
    extra = []
    for doc, cands in found.items():
        for c in cands:
            # Цитата из другой части внесённого абзаца — тоже верная находка, не лишняя
            if any(c.rule_code == e["rule"] and e["doc"] == doc
                   and quotes_match(e.get("text") or e["quote"], c.quote) for e in [*expected, *known]):
                continue
            extra.append({"rule": c.rule_code, "doc": doc, "quote": c.quote[:140], "confidence": c.confidence})
    print(f"\n{package}: найдено {len(hits)} из {len(expected)}, лишних {len(extra)}")
    for e in missed:
        print(f"  ПРОПУЩЕНО {e['rule']} {e['doc']}: {e['quote']}")
    for e in extra:
        print(f"  лишняя {e['rule']} {e['doc']} [{e['confidence']:.2f}]: {e['quote']}")
    return {"package": package, "expected": len(expected), "found": len(hits), "missed": missed, "extra": extra}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--package", action="append", help="номер пакета; по умолчанию все")
    parser.add_argument("--only", help="только эти правила, через запятую: R-02,R-07")
    parser.add_argument("--save", type=Path, help="записать результат в JSON")
    args = parser.parse_args()
    only = set(args.only.split(",")) if args.only else None
    packages = [PACKAGES / p for p in args.package] if args.package else sorted(p for p in PACKAGES.iterdir() if p.is_dir())
    rows = []
    total = LLMUsage()
    started = time.perf_counter()
    for package in packages:
        print(f"\n=== {package.name}")
        with tempfile.TemporaryDirectory() as tmp:
            paths, expected = inject_package(package, Path(tmp), only=only)
            found, usage, subject = analyse(paths)
        known_path = EXPECTED / package.name / "expected.json"
        known = json.loads(known_path.read_text()) if known_path.exists() else []
        print(f"Предмет закупки: {subject or '—'}")
        rows.append(report(package.name, found, expected, known))
        total.add(usage)
    hits = sum(r["found"] for r in rows)
    expected_total = sum(r["expected"] for r in rows)
    extra = sum(len(r["extra"]) for r in rows)
    by_rule: dict[str, list[int]] = {}
    for r in rows:
        for e in r["missed"]:
            by_rule.setdefault(e["rule"], [0, 0])[0] += 1
    print(f"\nИтого: найдено {hits} из {expected_total}, лишних {extra}; пропуски по правилам: "
          f"{ {k: v[0] for k, v in sorted(by_rule.items())} }")
    print(f"LLM: вызовов {total.calls}, из кеша {total.cached_calls}, вход {total.prompt_tokens}, "
          f"выход {total.completion_tokens}, ${total.cost:.4f}, {time.perf_counter() - started:.0f} с")
    if args.save:
        args.save.write_text(json.dumps(rows, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
