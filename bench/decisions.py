"""Модели решений (Jev, Clef) перед LLM: качество на размеченных пунктах, скорость, стоимость.

Модель решений не пишет текст: на вход — фрагмент документа и вопросы «да/нет», на выход —
вероятность «да» по каждому вопросу (`POST /api/alpha/decisions` OpenRouter). Вопрос по
правилу строится из полей правила — новое правило из интерфейса проверяется без правки кода.

Стенд прогоняет пункты из `bench.clauses`:

- модели решений — один вызов на пункт со всеми правилами: ROC AUC, полнота и точность при
  пороге, доля пар «пункт — правило», которую фильтр пропустит к LLM, не потеряв ни одного риска;
- LLM — тот же движок, что в анализе (`analyze_rule_group`, окна около 6000 токенов), находки
  сводятся к пунктам: полнота и точность;
- связка — LLM оставляет только находки, которые модель решений пропускает при пороге: так
  видно, что даёт фильтр перед LLM и проверка находок после неё.

    python -m bench.decisions
    python -m bench.decisions --decision cloudflare/clef-flash --llm "" --context 4
    python -m bench.decisions --set package --out result.json

`--context N` окружает пункт N служебными пунктами с каждой стороны: фрагмент около 400
токенов, как у `chunk_blocks`, — проверка, не теряется ли риск в длинном фрагменте.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx

from bench.clauses import CLAUSES, Clause, package_clauses
from contracts.analysis import LLMUsage, RuleSpec
from ml.analysis.blocks import SimpleBlock
from ml.analysis.engine import analyze_rule_group, group_rules
from ml.analysis.llm import LLMClient
from ml.analysis.templates import render_template, rule_query_text
from ml.checklist import load_default_checklist
from ml.config import get_settings

DECISION_MODELS = ("typesafe/jev-1.14", "typesafe/jev-1.13", "cloudflare/clef", "cloudflare/clef-flash")
THRESHOLDS = (0.2, 0.5)
_RETRY_STATUS = {408, 429, 500, 502, 503, 504, 524, 529}


def question_name(rule: RuleSpec) -> str:
    return re.sub(r"\W", "_", rule.code)


def rule_question(rule: RuleSpec, variant: str) -> dict:
    """Вопрос «да/нет» по правилу: `prompt` — инструкция правила с переменными, `desc` — название и описание."""
    if variant == "prompt":
        text = render_template(rule.prompt_template, rule, fragment="этот фрагмент", subject=None)
    else:
        text = rule_query_text(rule)
    return {
        "type": "noul",
        "instructions": f"Правило «{rule.title}». {text} Есть ли в этом фрагменте закупочной документации "
                        "риск для поставщика по этому правилу?",
        "criteria": {
            "true": f"Во фрагменте есть условие, описанное в правиле «{rule.title}».",
            "false": "Такого условия во фрагменте нет, или условие соответствует закону и обычной практике.",
        },
    }


@dataclass
class Call:
    probs: dict[str, float] | None
    seconds: float
    cost: float = 0.0
    input_tokens: int = 0
    provider: str = ""
    error: str = ""


def decide(http: httpx.Client, model: str, state: str, questions: dict, *, attempts: int = 3) -> Call:
    settings = get_settings()
    url = settings.llm_base_url.rstrip("/").removesuffix("/v1") + "/alpha/decisions"
    headers = {"Authorization": f"Bearer {settings.llm_api_key}", "Content-Type": "application/json"}
    started = time.perf_counter()
    error = ""
    for attempt in range(attempts):
        try:
            response = http.post(url, headers=headers, timeout=60,
                                 json={"model": model, "state": state, "questions": questions})
        except httpx.HTTPError as exc:
            error = type(exc).__name__
        else:
            if response.status_code == 200 and "answers" in (body := response.json()):
                usage = body.get("usage") or {}
                probs = {name: float(a.get("noul", 0.0)) for name, a in body["answers"].items()}
                return Call(probs, time.perf_counter() - started, float(usage.get("cost") or 0),
                            int(usage.get("input_tokens") or 0), body.get("provider", ""))
            error = f"HTTP {response.status_code}: {response.text[:200]}"
            if response.status_code not in _RETRY_STATUS:
                break
        time.sleep(2**attempt)
    return Call(None, time.perf_counter() - started, error=error)


def with_context(clauses: list[Clause], n: int, seed: int = 7) -> list[str]:
    """Состояние для модели решений: пункт и по n служебных пунктов до и после."""
    if n <= 0:
        return [c.text for c in clauses]
    from bench.samples import FILLER

    pool = [c.text for c in CLAUSES if not c.rules and not c.also][-10:] + [i.text for i in FILLER if i.level > 0]
    rng = random.Random(seed)
    states = []
    for clause in clauses:
        others = [t for t in pool if t != clause.text]
        before, after = rng.sample(others, n), rng.sample(others, n)
        states.append("\n".join([*before, clause.text, *after]))
    return states


@dataclass
class DecisionRun:
    model: str
    variant: str
    calls: list[Call]
    wall: float


def run_decisions(model: str, states: list[str], rules: list[RuleSpec], variant: str, concurrency: int) -> DecisionRun:
    questions = {question_name(r): rule_question(r, variant) for r in rules}
    started = time.perf_counter()
    with httpx.Client() as http, ThreadPoolExecutor(max_workers=concurrency) as pool:
        calls = list(pool.map(lambda s: decide(http, model, s, questions), states))
    return DecisionRun(model, variant, calls, time.perf_counter() - started)


@dataclass
class LLMRun:
    model: str
    reasoning: str
    found: list[dict[str, float]]  # по пункту: правило → уверенность LLM
    usage: LLMUsage
    dropped: int
    wall: float
    error: str = ""
    providers: list[str] = field(default_factory=list)


def run_llm(model: str, clauses: list[Clause], rules: list[RuleSpec], reasoning: str, seed: int = 7) -> LLMRun:
    """Пункты в случайном порядке — один документ; все правила читают его окнами, как тип `llm`."""
    order = list(range(len(clauses)))
    random.Random(seed).shuffle(order)
    blocks = [SimpleBlock(id=f"k{i}", order=pos, text=clauses[i].text, number=f"{pos + 1}.")
              for pos, i in enumerate(order)]
    client = LLMClient(replace(get_settings(), llm_model=model, llm_reasoning=reasoning, llm_cache=False))
    groups = group_rules([r.model_copy(update={"kind": "llm"}) for r in rules])
    found: list[dict[str, float]] = [{} for _ in clauses]
    usage = LLMUsage()
    started = time.perf_counter()
    try:
        with ThreadPoolExecutor(max_workers=len(groups)) as pool:
            results = list(pool.map(lambda g: analyze_rule_group(blocks, g, llm=client), groups))
    except Exception as exc:  # noqa: BLE001 — сбой модели — тоже результат стенда
        return LLMRun(model, reasoning, found, usage, 0, time.perf_counter() - started, error=str(exc)[:300])
    wall = time.perf_counter() - started
    dropped = 0
    for result in results:
        usage.add(result.usage)
        dropped += len(result.dropped)
        for cand in result.candidates:
            i = int(cand.block_id[1:])
            found[i][cand.rule_code] = max(found[i].get(cand.rule_code, 0.0), cand.confidence)
    return LLMRun(model, reasoning, found, usage, dropped, wall)


# --- Метрики ------------------------------------------------------------------------------


def pairs(clauses: list[Clause], rules: list[RuleSpec]):
    """Пары (пункт, правило, есть ли риск) без спорных."""
    for i, clause in enumerate(clauses):
        for rule in rules:
            if rule.code not in clause.also:
                yield i, rule, rule.code in clause.rules


def auc(pos: list[float], neg: list[float]) -> float | None:
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def pr(predicted: list[tuple[bool, bool]]) -> tuple[float | None, float | None, int, int, int]:
    """[(предсказан риск, есть риск)] → точность, полнота, TP, FP, FN."""
    tp = sum(p and t for p, t in predicted)
    fp = sum(p and not t for p, t in predicted)
    fn = sum(t and not p for p, t in predicted)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return precision, recall, tp, fp, fn


def decision_metrics(run: DecisionRun, clauses: list[Clause], rules: list[RuleSpec]) -> dict:
    rows = []
    for i, rule, truth in pairs(clauses, rules):
        probs = run.calls[i].probs
        if probs is not None:
            rows.append((rule.code, probs.get(question_name(rule), 0.0), truth))
    ok = [c for c in run.calls if c.probs is not None]
    pos = [p for _, p, t in rows if t]
    neg = [p for _, p, t in rows if not t]
    per_rule = {r.code: auc([p for c, p, t in rows if c == r.code and t], [p for c, p, t in rows if c == r.code and not t])
                for r in rules}
    out = {
        "model": run.model, "variant": run.variant, "calls": len(run.calls), "errors": len(run.calls) - len(ok),
        "error_sample": next((c.error for c in run.calls if c.error), ""),
        "auc": auc(pos, neg), "auc_by_rule": per_rule,
        "auc_macro": statistics.mean(v for v in per_rule.values() if v is not None) if any(per_rule.values()) else None,
        "latency_p50": statistics.median(c.seconds for c in ok) if ok else None,
        "latency_p95": sorted(c.seconds for c in ok)[int(0.95 * (len(ok) - 1))] if ok else None,
        "wall": run.wall, "cost": sum(c.cost for c in ok),
        "input_tokens": statistics.mean(c.input_tokens for c in ok) if ok else None,
        "providers": sorted({c.provider for c in ok}),
    }
    for t in THRESHOLDS:
        precision, recall, *_ = pr([(p >= t, truth) for _, p, truth in rows])
        out[f"at_{t}"] = {"precision": precision, "recall": recall,
                          "pass": sum(p >= t for _, p, _ in rows) / len(rows) if rows else None}
    if pos:
        t_all = min(pos)
        out["no_miss"] = {"threshold": t_all, "pass": sum(p >= t_all for _, p, _ in rows) / len(rows)}
    return out


def llm_metrics(run: LLMRun, clauses: list[Clause], rules: list[RuleSpec],
                gate: DecisionRun | None = None, threshold: float = 0.0) -> dict:
    """Полнота и точность LLM по пунктам; с `gate` — только находки, которые модель решений пропускает."""
    predicted = []
    for i, rule, truth in pairs(clauses, rules):
        hit = rule.code in run.found[i]
        if gate is not None and hit:
            probs = gate.calls[i].probs
            hit = probs is None or probs.get(question_name(rule), 0.0) >= threshold
        predicted.append((hit, truth))
    precision, recall, tp, fp, fn = pr(predicted)
    return {"precision": precision, "recall": recall, "tp": tp, "fp": fp, "fn": fn}


def _f(v: float | None, digits: int = 2) -> str:
    return "—" if v is None else f"{v:.{digits}f}"


def report(decisions: list[DecisionRun], llms: list[LLMRun], clauses: list[Clause], rules: list[RuleSpec]) -> dict:
    n_pos = sum(t for *_, t in pairs(clauses, rules))
    n_all = sum(1 for _ in pairs(clauses, rules))
    print(f"\nПунктов {len(clauses)}, пар «пункт — правило» {n_all}, из них с риском {n_pos}")
    result: dict = {"clauses": len(clauses), "pairs": n_all, "positive_pairs": n_pos, "decisions": [], "llm": []}

    if decisions:
        print(f"\n{'модель решений':<24}{'вопрос':<8}{'AUC':>7}{'AUC ср.':>8}"
              + "".join(f"{'P@' + str(t):>7}{'R@' + str(t):>7}{'пропуск':>8}" for t in THRESHOLDS)
              + f"{'без потерь':>11}{'p50, с':>8}{'p95, с':>8}{'всего, с':>9}{'$ на 1000':>10}{'ошибок':>7}")
        for run in decisions:
            m = decision_metrics(run, clauses, rules)
            result["decisions"].append(m)
            cells = "".join(f"{_f(m[f'at_{t}']['precision']):>7}{_f(m[f'at_{t}']['recall']):>7}"
                            f"{_f(m[f'at_{t}']['pass']):>8}" for t in THRESHOLDS)
            no_miss = m.get("no_miss", {})
            per_1000 = m["cost"] / max(1, m["calls"] - m["errors"]) * 1000
            print(f"{run.model:<24}{run.variant:<8}{_f(m['auc'], 3):>7}{_f(m['auc_macro'], 3):>8}{cells}"
                  f"{_f(no_miss.get('pass')):>11}{_f(m['latency_p50']):>8}{_f(m['latency_p95']):>8}"
                  f"{m['wall']:>9.1f}{per_1000:>10.4f}{m['errors']:>7}")
            if m["errors"]:
                print(f"  ошибка: {m['error_sample']}")
        print("  «пропуск» — доля пар, которую фильтр отправит в LLM; «без потерь» — доля при пороге,"
              " на котором не теряется ни один риск")
        print(f"\nAUC по правилам\n{'модель':<24}{'вопрос':<8}" + "".join(f"{r.code:>7}" for r in rules))
        for m in result["decisions"]:
            print(f"{m['model']:<24}{m['variant']:<8}" + "".join(f"{_f(m['auc_by_rule'][r.code]):>7}" for r in rules))

    if llms:
        print(f"\n{'LLM':<36}{'рассужд.':<9}{'точность':>9}{'полнота':>8}{'TP':>4}{'FP':>4}{'FN':>4}"
              f"{'время, с':>9}{'вызовов':>8}{'$':>9}{'отброш.':>8}")
        for run in llms:
            if run.error:
                print(f"{run.model:<36}{run.reasoning:<9}  ошибка: {run.error}")
                continue
            m = llm_metrics(run, clauses, rules)
            by_rule = {r.code: llm_metrics(run, clauses, [r]) for r in rules}
            result["llm"].append({"model": run.model, "reasoning": run.reasoning, **m, "wall": run.wall,
                                  "usage": run.usage.model_dump(), "dropped": run.dropped, "by_rule": by_rule,
                                  "gated": {}})
            print(f"{run.model:<36}{run.reasoning:<9}{_f(m['precision']):>9}{_f(m['recall']):>8}{m['tp']:>4}"
                  f"{m['fp']:>4}{m['fn']:>4}{run.wall:>9.1f}{run.usage.calls:>8}{run.usage.cost:>9.4f}{run.dropped:>8}")
            print("  по правилам (точность/полнота): " + ", ".join(
                f"{c} {_f(v['precision'])}/{_f(v['recall'])}" for c, v in by_rule.items()))
            for gate in decisions:
                for t in THRESHOLDS:
                    g = llm_metrics(run, clauses, rules, gate, t)
                    result["llm"][-1]["gated"][f"{gate.model}|{gate.variant}|{t}"] = g
                    print(f"  + {gate.model} ({gate.variant}) ≥ {t}: точность {_f(g['precision'])}, "
                          f"полнота {_f(g['recall'])} (TP {g['tp']}, FP {g['fp']}, FN {g['fn']})")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--decision", default=",".join(DECISION_MODELS), help="модели решений через запятую")
    parser.add_argument("--llm", default=None, help="LLM через запятую; по умолчанию LLM_MODEL, пусто — без LLM")
    parser.add_argument("--reasoning", default="none", help="режимы рассуждений LLM через запятую")
    parser.add_argument("--variant", default="prompt", help="вопрос по правилу: prompt, desc или оба через запятую")
    parser.add_argument("--context", type=int, default=0, help="служебных пунктов вокруг пункта с каждой стороны")
    parser.add_argument("--set", choices=("clauses", "package", "all"), default="clauses")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--out", type=Path, help="сохранить метрики в JSON")
    args = parser.parse_args()

    clauses = {"clauses": CLAUSES, "package": package_clauses(), "all": CLAUSES + package_clauses()}[args.set]
    _, rules = load_default_checklist()
    rules = [r for r in rules if r.enabled]
    states = with_context(clauses, args.context)
    decision_models = [m.strip() for m in args.decision.split(",") if m.strip()]
    llm_models = [m.strip() for m in (args.llm if args.llm is not None else get_settings().llm_model).split(",")
                  if m.strip()]

    decisions = []
    for model in decision_models:
        for variant in [v.strip() for v in args.variant.split(",") if v.strip()]:
            print(f"модель решений {model}, вопрос {variant}…", flush=True)
            decisions.append(run_decisions(model, states, rules, variant, args.concurrency))
    llms = []
    for model in llm_models:
        for mode in [m.strip() for m in args.reasoning.split(",") if m.strip()]:
            print(f"LLM {model}, рассуждения {mode}…", flush=True)
            llms.append(run_llm(model, clauses, rules, mode))

    result = report(decisions, llms, clauses, rules)
    if args.out:
        result["args"] = {k: str(v) for k, v in vars(args).items()}
        result["raw"] = {
            "decisions": [{"model": d.model, "variant": d.variant,
                           "probs": [c.probs for c in d.calls]} for d in decisions],
            "llm": [{"model": r.model, "reasoning": r.reasoning, "found": r.found} for r in llms],
            "labels": [{"text": c.text[:80], "rules": sorted(c.rules), "also": sorted(c.also)} for c in clauses],
        }
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
