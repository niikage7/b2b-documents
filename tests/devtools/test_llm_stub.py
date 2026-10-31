"""Заглушка LLM для docker-compose: ответы по схеме запроса через тот же адаптер `LLMClient`, без сети."""

from __future__ import annotations

import json
import threading

import httpx
import pytest

from bench import samples
from devtools.llm_stub import server
from ml.analysis.batch_meta import extract_batch_meta
from ml.analysis.blocks import blocks_from_extraction, chunk_blocks
from ml.analysis.engine import analyze_rule_group, group_rules
from ml.analysis.llm import LLMClient
from ml.analysis.passport import extract_passport
from ml.analysis.search import InMemorySearch
from ml.checklist import load_default_checklist
from ml.config import Settings
from ml.extract.docx_reader import extract_docx
from tests.fakes import FakeEmbedder, make_client

EMBEDDER = FakeEmbedder()


def stub_client() -> LLMClient:
    return make_client(lambda request: httpx.Response(200, json=server.respond(json.loads(request.content))))


@pytest.fixture(scope="module")
def package(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("stub")
    docs = {name: blocks_from_extraction(extract_docx(make(tmp / f"{name}.docx")))
            for name, make in (("contract", samples.make_contract_docx), ("notice", samples.make_notice_docx))}
    store = InMemorySearch()
    for name, blocks in docs.items():
        store.add(name, chunk_blocks(blocks), embedder=EMBEDDER)
    return docs, store


def test_rule_groups_get_anchored_findings(package):
    docs, store = package
    _, rules = load_default_checklist()
    client = stub_client()
    found, dropped = [], []
    for name, blocks in docs.items():
        for group in group_rules(rules):
            result = analyze_rule_group(blocks, group, subject=samples.SUBJECT, document_id=name, search=store,
                                        llm=client, embedder=EMBEDDER)
            found += result.candidates
            dropped += [d for d in result.dropped if d.reason == "quote_not_found"]
            # В одном ответе предложение достаётся одному правилу
            assert len({(c.block_id, c.start) for c in result.candidates}) == len(result.candidates)
    assert len(found) >= 6 and not dropped  # цитаты заглушки всегда есть в тексте блока
    assert {c.level for c in found} == {"critical", "warning"}
    assert all("заглушки" in c.explanation for c in found)


def test_no_risk_when_nothing_matches():
    prompt = ("Предмет закупки: не указан\n\nФрагменты документа:\n\n[1] стр. 1\nОбщие положения.\n\n"
              "Правила проверки:\n\n### R-03. Неустойки и штрафы\nСуть риска: пени выше закона\n"
              "Инструкция: проверь\nГде искать: фрагмент [1].\n\nВерни JSON")
    assert server.risk_findings(prompt) == {"rules": [{"rule_code": "R-03", "findings": []}]}


def test_subject_and_passport(package):
    docs, store = package
    client = stub_client()
    # Предмет из контракта шаблон ml не находит — вызов LLM, у заглушки предмета там тоже нет
    assert extract_batch_meta([("contract.docx", docs["contract"])], llm=client) is None
    assert server.batch_meta("Наименование объекта закупки: поставка бумаги.\n") == {"subject": "поставка бумаги"}
    passport = extract_passport([(n, f"{n}.docx", b) for n, b in docs.items()], search=store, llm=client,
                                embedder=EMBEDDER)
    values = {f.key: f.value for f in passport.fields}
    assert values["contract_security"].startswith("30 (тридцать) процентов")
    assert values["delivery_term"].startswith("Поставка Товара осуществляется в течение 3")
    assert values["subject"] == samples.SUBJECT


def test_http_server_speaks_openai_protocol():
    httpd = server.serve(port=0, host="127.0.0.1")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        assert httpx.get(f"{base}/health").json() == {"status": "ok"}
        settings = Settings(llm_base_url=f"{base}/v1", llm_model=server.MODEL, llm_api_key="", llm_cache=False,
                            llm_max_retries=0)
        from pydantic import BaseModel

        class Meta(BaseModel):
            subject: str | None

        answer = LLMClient(settings).chat(
            [{"role": "user", "content": "Наименование объекта закупки: поставка бумаги.\nВерни JSON: {\"subject\"}"}],
            Meta, schema_name="batch_meta")
        assert answer.data.subject == "поставка бумаги" and answer.usage.calls == 1
        assert httpx.post(f"{base}/v1/other", json={}).status_code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
