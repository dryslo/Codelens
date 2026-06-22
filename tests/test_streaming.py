"""стриминг ответа LLM по токенам - порт, провайдеры, gateway, backend."""
import types

import requests
from fastapi.testclient import TestClient

from src.clients.backend import LocalBackend
from src.domain.interfaces import LLMProvider
from src.factory import Components
from src.llm.openai_compatible import OpenAICompatibleLLM
from src.llm.remote import RemoteLLM
from src.util import sse


class _OneShot(LLMProvider):
    def chat(self, messages):
        return "ABC"

    def hyde(self, query):
        return ""

    def multiquery(self, query, n=3):
        return []


class _Streamer(LLMProvider):
    def chat(self, messages):
        return "Hello"

    def hyde(self, query):
        return ""

    def multiquery(self, query, n=3):
        return []

    def chat_stream(self, messages):
        yield "Hel"
        yield "lo"


# ---------- SSE-обёртка ----------

def test_sse_roundtrip_with_newline():
    packed = sse.pack("line1\nline2")          # \n внутри токена переживает за счёт JSON-экранирования
    assert packed.count("\n\n") == 1
    out = list(sse.parse_lines(iter(packed.splitlines() + ["data: [DONE]"])))
    assert out == ["line1\nline2"]


def test_sse_stream_emits_done_even_on_source_error():
    def good():
        yield "A"
        yield "B"

    def boom():
        yield "A"
        raise RuntimeError("источник упал")

    # нормальный путь: дельты + сентинел
    assert list(sse.parse_lines(iter("".join(sse.stream(good())).splitlines()))) == ["A", "B"]
    # ошибка в середине: поток всё равно завершается [DONE], клиент не виснет
    out = "".join(sse.stream(boom()))
    assert out.endswith(sse.done())
    assert list(sse.parse_lines(iter(out.splitlines()))) == ["A"]


# ---------- порт: дефолт = один чанк ----------

def test_default_chat_stream_yields_full_once():
    assert list(_OneShot().chat_stream([{"role": "user", "content": "q"}])) == ["ABC"]


# ---------- openai_compatible: stream=True даёт дельты ----------

def test_openai_compatible_stream(monkeypatch):
    deltas = ["a", "b", None, "c"]   # None-дельты пропускаются
    chunks = [types.SimpleNamespace(choices=[types.SimpleNamespace(
        delta=types.SimpleNamespace(content=d))]) for d in deltas]
    seen = {}

    class FakeCompletions:
        def create(self, model, messages, stream=False):
            seen["stream"] = stream
            return iter(chunks)

    fake = types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions()))
    monkeypatch.setattr(OpenAICompatibleLLM, "_client", lambda self: fake)
    llm = OpenAICompatibleLLM(model="m")
    assert list(llm.chat_stream([{"role": "user", "content": "q"}])) == ["a", "b", "c"]
    assert seen["stream"] is True


# ---------- RemoteLLM: читает SSE из gateway ----------

def test_remote_llm_chat_stream(monkeypatch):
    lines = ['data: {"t": "He"}', 'data: {"t": "llo"}', "data: [DONE]"]
    seen = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_lines(self, decode_unicode=False):
            return iter(lines)

    def fake_post(url, json, stream, timeout):
        seen["url"], seen["stream"] = url, stream
        return FakeResp()

    monkeypatch.setattr(requests, "post", fake_post)
    out = list(RemoteLLM("http://llm:8001", "P").chat_stream([{"role": "user", "content": "q"}]))
    assert out == ["He", "llo"]
    assert seen["url"] == "http://llm:8001/chat/stream" and seen["stream"] is True


# ---------- gateway /chat/stream (SSE) ----------

def test_gateway_chat_stream_endpoint():
    from services import llm_app
    with TestClient(llm_app.app) as client:
        llm_app._LLMS = {"P": _Streamer()}      # lifespan уже отработал, подмена реестра
        r = client.post("/chat/stream",
                        json={"provider": "P", "messages": [{"role": "user", "content": "q"}]})
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        assert list(sse.parse_lines(iter(r.text.splitlines()))) == ["Hel", "lo"]


# ---------- LocalBackend.chat_stream: стрим + персист истории ПОСЛЕ ----------

class _FakeHist:
    def __init__(self):
        self.msgs, self.renamed = [], None

    def get_messages(self, chat_id):
        return list(self.msgs)

    def append(self, chat_id, role, content, **kw):
        self.msgs.append({"role": role, "content": content, "chunk_id": None})

    def rename(self, chat_id, title):
        self.renamed = title


class _FakeRetr:
    def search(self, q, k=5, mode="fast", flags=None):
        return [{"chunk_id": "c1", "code": "x", "meta": {"file": "f", "name": "n"}}]


class _FakeCache:
    enabled = True

    def __init__(self):
        self.data = {}

    def get(self, k):
        return self.data.get(k)

    def set(self, k, v, ttl=3600):
        self.data[k] = v


def test_local_answer_stream_no_cache():
    lb = LocalBackend(Components(llms={"M": _Streamer()}, cfg={}))   # без cache: стрим напрямую
    chunks = [{"chunk_id": "c1", "code": "x", "meta": {}}]
    assert list(lb.answer_stream("q", chunks, "M")) == ["Hel", "lo"]


def test_local_answer_stream_caches_then_hits():
    cache = _FakeCache()
    lb = LocalBackend(Components(llms={"M": _Streamer()}, cfg={}, cache=cache))
    chunks = [{"chunk_id": "c1", "code": "x", "meta": {}}]
    assert list(lb.answer_stream("q", chunks, "M")) == ["Hel", "lo"]   # miss: стрим и сохранение
    assert list(lb.answer_stream("q", chunks, "M")) == ["Hello"]       # hit: один чанк


def test_local_backend_chat_stream_persists_after():
    comp = Components(history=_FakeHist(), llms={"M": _Streamer()},
                      retriever=_FakeRetr(), cfg={}, fast=None)
    lb = LocalBackend(comp)
    out = list(lb.chat_stream("chat1", "hi", model="M"))
    assert out == ["Hel", "lo"]
    # история записана после стрима: user и assistant (склейка дельт)
    assert comp.history.msgs[-2:] == [
        {"role": "user", "content": "hi", "chunk_id": None},
        {"role": "assistant", "content": "Hello", "chunk_id": None},
    ]
    assert comp.history.renamed is not None      # первый ход, название сгенерировано
