import src.persistence.cache as cache_mod
from src.clients.backend import LocalBackend
from src.factory import Components
from src.persistence.cache import (
    InProcessCache,
    NullCache,
    bump_epoch,
    build_cache,
    current_epoch,
)
from src.persistence.registry_repo import CachingRegistry
from src.retrieval.flags import FlagsPolicy
from src.retrieval.hybrid import HybridRetriever
from src.retrieval.hyde import HyDEExpander
from src.retrieval.multiquery import MultiQueryExpander


# --- кэш-реализации ---

def test_inprocess_hit_miss():
    c = InProcessCache()
    assert c.get("k") is None
    c.set("k", {"a": 1})
    assert c.get("k") == {"a": 1}


def test_inprocess_ttl_expires(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(cache_mod.time, "monotonic", lambda: clock["t"])
    c = InProcessCache()
    c.set("k", "v", ttl=10)
    assert c.get("k") == "v"
    clock["t"] += 11
    assert c.get("k") is None


def test_null_cache_always_miss():
    c = NullCache()
    c.set("k", "v")
    assert c.get("k") is None
    assert c.enabled is False


def test_build_cache_empty_is_null():
    assert isinstance(build_cache(""), NullCache)
    assert isinstance(build_cache(None), NullCache)


def test_epoch_bump():
    c = InProcessCache()
    assert current_epoch(c) == 0
    assert bump_epoch(c) == 1
    assert current_epoch(c) == 1


# --- CachingRegistry ---

class _FakeRegistry:
    def __init__(self):
        self.store = {}
        self.get_calls = 0

    def get_hash(self, source, file):
        self.get_calls += 1
        return self.store.get((source, file))

    def set_hash(self, source, file, h):
        self.store[(source, file)] = h

    def files(self, source):
        return [f for (s, f) in self.store if s == source]

    def remove(self, source, file=None):
        for key in [k for k in self.store if k[0] == source and (file is None or k[1] == file)]:
            self.store.pop(key)


def test_caching_registry_caches_and_invalidates():
    base = _FakeRegistry()
    base.set_hash("src", "a.py", "h1")
    reg = CachingRegistry(base, InProcessCache())

    assert reg.get_hash("src", "a.py") == "h1"   # промах, база
    n = base.get_calls
    assert reg.get_hash("src", "a.py") == "h1"    # из кэша
    assert base.get_calls == n

    reg.set_hash("src", "a.py", "h2")             # write-through
    assert reg.get_hash("src", "a.py") == "h2"

    reg.remove("src", "a.py")                     # инвалидация (tombstone)
    assert reg.get_hash("src", "a.py") is None


# --- ретривер: cache-aside вокруг чистого ядра ---

class _FakeStore:
    def __init__(self):
        self.queries = 0

    def query(self, emb, k=20, where=None):
        self.queries += 1
        return [{"chunk_id": "f.py:foo:1", "code": "def foo(): ...",
                 "meta": {"file": "f.py", "name": "foo"}, "distance": 0.1}]

    def get_embeddings(self, ids):
        return {}

    def iter_all(self):
        return []

    def count(self):
        return 1


class _FakeEmbedder:
    def __init__(self):
        self.calls = 0

    def encode(self, texts, is_query=False):
        self.calls += 1
        return [[0.1, 0.2, 0.3] for _ in texts]


def _retriever(cache):
    return HybridRetriever(_FakeStore(), _FakeEmbedder(), policy=FlagsPolicy(), cache=cache)


def test_retriever_cache_hit_skips_pipeline():
    r = _retriever(InProcessCache())
    res1 = r.search("q", k=5, mode="fast")
    assert res1 and res1[0]["chunk_id"] == "f.py:foo:1"
    emb_calls, store_calls = r.embedder.calls, r.store.queries

    res2 = r.search("q", k=5, mode="fast")
    assert res2 == res1
    # на попадание чистое ядро не вызывалось: ни эмбеддер, ни стор
    assert r.embedder.calls == emb_calls
    assert r.store.queries == store_calls


def test_retriever_cache_invalidated_by_epoch():
    cache = InProcessCache()
    r = _retriever(cache)
    r.search("q", k=5, mode="fast")
    emb_calls = r.embedder.calls

    bump_epoch(cache)                              # имитация admin index/remove
    r.search("q", k=5, mode="fast")
    assert r.embedder.calls == emb_calls + 1       # ключ сменился, ядро отработало снова


def test_retriever_without_cache_runs_each_time():
    r = _retriever(None)
    r.search("q", k=5, mode="fast")
    r.search("q", k=5, mode="fast")
    assert r.embedder.calls == 2


def test_retriever_uses_precomputed_query_emb():
    r = _retriever(None)
    r.search("q", k=5, mode="fast", query_emb=[0.1, 0.2, 0.3])
    assert r.embedder.calls == 0     # вектор передан, encode не звался
    assert r.store.queries == 1


# --- кэш экспандеров (hyde / multiquery) ---

class _FakeLLM:
    def __init__(self):
        self.hyde_calls = 0
        self.mq_calls = 0

    def hyde(self, query):
        self.hyde_calls += 1
        return f"hypo:{query}"

    def multiquery(self, query, n):
        self.mq_calls += 1
        return [f"{query}#{i}" for i in range(n)]


def test_hyde_expander_caches_llm_output():
    llm = _FakeLLM()
    ex = HyDEExpander(llm, cache=InProcessCache())
    assert ex.expand("q") == ex.expand("q")
    assert llm.hyde_calls == 1       # второй раз из кэша


def test_multiquery_expander_caches_llm_output():
    llm = _FakeLLM()
    ex = MultiQueryExpander(llm, n=3, cache=InProcessCache())
    assert ex.expand_list("q") == ex.expand_list("q")
    assert llm.mq_calls == 1


# --- backend: condense + состояние чата ---

class _FakeHistory:
    def __init__(self):
        self.get_calls = 0
        self._msgs = [{"role": "user", "content": "hi", "retrieved_ids": []}]

    def get_messages(self, chat_id):
        self.get_calls += 1
        return list(self._msgs)


class _CondenseLLM:
    def __init__(self):
        self.calls = 0

    def chat(self, messages):
        self.calls += 1
        return "standalone query"


def test_backend_get_messages_cache_and_invalidation():
    hist = _FakeHistory()
    b = LocalBackend(Components(history=hist, cache=InProcessCache(), cfg={}))
    b.get_messages("c1")
    b.get_messages("c1")
    assert hist.get_calls == 1        # второй раз из кэша
    b._invalidate_chat("c1")
    b.get_messages("c1")
    assert hist.get_calls == 2        # после инвалидации снова в БД


def test_backend_condense_caches():
    llm = _CondenseLLM()
    b = LocalBackend(Components(fast="x", llms={"x": llm}, cache=InProcessCache(), cfg={}))
    history = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    assert b._condense(history, "follow") == b._condense(history, "follow")
    assert llm.calls == 1             # второй раз из кэша
