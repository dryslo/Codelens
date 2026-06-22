"""RemoteLLM-контракт == локальный, gateway-реестр, split-роль inference_app."""
import requests

from src import factory
from src.llm.remote import RemoteLLM, build_remote_llms


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


# ---------- RemoteLLM: URL / payload / разбор ответа ----------

def test_remote_llm_chat(monkeypatch):
    seen = {}

    def fake_post(url, json, timeout):
        seen["url"], seen["json"] = url, json
        return _FakeResp({"content": "hi"})

    monkeypatch.setattr(requests, "post", fake_post)
    out = RemoteLLM("http://llm:8001/", "Groq").chat([{"role": "user", "content": "q"}])
    assert out == "hi"
    assert seen["url"] == "http://llm:8001/chat"
    assert seen["json"] == {"provider": "Groq", "messages": [{"role": "user", "content": "q"}]}


def test_remote_llm_hyde(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda url, json, timeout: _FakeResp({"text": "def f(): ..."}))
    assert RemoteLLM("http://llm:8001", "P").hyde("how to sort") == "def f(): ..."


def test_remote_llm_multiquery(monkeypatch):
    seen = {}

    def fake_post(url, json, timeout):
        seen["json"] = json
        return _FakeResp({"variants": ["q", "v1", "v2"]})

    monkeypatch.setattr(requests, "post", fake_post)
    out = RemoteLLM("http://llm:8001", "P").multiquery("q", n=2)
    assert out == ["q", "v1", "v2"]
    assert seen["json"] == {"provider": "P", "query": "q", "n": 2}


# ---------- build_remote_llms: GET /llms возвращает {name: RemoteLLM} ----------

def test_build_remote_llms(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, timeout: _FakeResp({"names": ["A", "B"]}))
    llms = build_remote_llms("http://llm:8001/")
    assert set(llms) == {"A", "B"}
    assert all(isinstance(v, RemoteLLM) for v in llms.values())
    assert llms["A"].url == "http://llm:8001" and llms["A"].provider == "A"


# ---------- factory.build_llms: ветка kind=remote (контракт == local) ----------

def test_factory_build_llms_remote_branch(monkeypatch):
    called = {}

    def fake_build_remote(url):
        called["url"] = url
        return {"A": RemoteLLM(url, "A")}

    monkeypatch.setattr("src.llm.remote.build_remote_llms", fake_build_remote)
    llms = factory.build_llms({"kind": "remote", "llm_url": "http://llm:8001"})
    assert called["url"] == "http://llm:8001"
    assert set(llms) == {"A"}


def test_factory_build_llms_local_default(monkeypatch):
    # без kind - цикл по providers, remote-ветка не трогается
    llms = factory.build_llms({"providers": {}})
    assert llms == {}


# ---------- inference_app: split по INFERENCE_ROLE ----------

def _patch_models(monkeypatch):
    import src.util.model_cache as mc
    monkeypatch.setattr(mc, "cached_sentence_transformer", lambda name: object())
    monkeypatch.setattr(mc, "cached_cross_encoder", lambda name: object())
    monkeypatch.setattr("src.embeddings.local.prefixes_for", lambda name: None)


def test_inference_role_embed_only(monkeypatch):
    import services.inference_app as app
    _patch_models(monkeypatch)
    monkeypatch.setenv("INFERENCE_ROLE", "embed")
    monkeypatch.setenv("RERANKER_MODEL", "some/model")
    app._EMB = app._RER = None
    app._load()
    assert app._EMB is not None and app._RER is None


def test_inference_role_rerank_only(monkeypatch):
    import services.inference_app as app
    _patch_models(monkeypatch)
    monkeypatch.setenv("INFERENCE_ROLE", "rerank")
    monkeypatch.setenv("RERANKER_MODEL", "some/model")
    app._EMB = app._RER = None
    app._load()
    assert app._EMB is None and app._RER is not None
