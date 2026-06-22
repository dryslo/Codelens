"""фильтры поиска по lang/source - нормализация, трансляция в стор, проброс в ретривере."""
import pytest

from src.clients.backend import LocalBackend
from src.factory import Components
from src.retrieval import filters as filt
from src.retrieval.hybrid import HybridRetriever
from src.stores.chroma import _chroma_where


# ---------- filters.normalize / match ----------

def test_normalize_drops_empty():
    assert filt.normalize(None) is None
    assert filt.normalize({"lang": [], "source": []}) is None
    assert filt.normalize({"lang": ["python"], "source": []}) == {"lang": ["python"]}
    assert filt.normalize({"lang": ["py"], "source": ["s"]}) == {"lang": ["py"], "source": ["s"]}


def test_match():
    assert filt.match({"lang": "python", "source": "s1"}, None) is True
    assert filt.match({"lang": "python"}, {"lang": ["python", "go"]}) is True
    assert filt.match({"lang": "javascript"}, {"lang": ["python"]}) is False
    assert filt.match({"lang": "python", "source": "s2"},
                      {"lang": ["python"], "source": ["s1"]}) is False


# ---------- трансляция в Chroma where ----------

def test_chroma_where():
    assert _chroma_where(None) is None
    assert _chroma_where({"lang": ["python"]}) == {"lang": {"$in": ["python"]}}
    assert _chroma_where({"lang": ["py"], "source": ["s"]}) == {
        "$and": [{"lang": {"$in": ["py"]}}, {"source": {"$in": ["s"]}}]}


# ---------- ретривер пробрасывает where в стор ----------

class _Emb:
    def encode(self, queries, is_query=False):
        return [[0.0] for _ in queries]


class _FilterStore:
    def __init__(self, chunks):
        self.chunks = chunks
        self.last_where = "UNSET"

    def query(self, emb, k=50, where=None):
        self.last_where = where
        items = [c for c in self.chunks if filt.match(c["meta"], where)]
        return [dict(c, distance=0.1) for c in items][:k]

    def iter_all(self):
        return iter(self.chunks)

    def get_embeddings(self, ids):
        return {}


_CHUNKS = [
    {"chunk_id": "a", "code": "def alpha(): pass", "meta": {"lang": "python", "source": "s1"}},
    {"chunk_id": "b", "code": "function beta(){}", "meta": {"lang": "javascript", "source": "s2"}},
]


def test_retriever_passes_where_to_store():
    store = _FilterStore(_CHUNKS)
    r = HybridRetriever(store, _Emb(), cache=None)
    out = r.search("x", k=5, flags={}, where={"lang": ["python"]})
    assert {c["chunk_id"] for c in out} == {"a"}
    assert store.last_where == {"lang": ["python"]}


def test_retriever_empty_filter_normalized_to_none():
    store = _FilterStore(_CHUNKS)
    r = HybridRetriever(store, _Emb(), cache=None)
    r.search("x", k=5, flags={}, where={"lang": [], "source": []})
    assert store.last_where is None


def test_bm25_post_filter():
    pytest.importorskip("bm25s")
    store = _FilterStore(_CHUNKS)
    r = HybridRetriever(store, _Emb(), cache=None)
    out = r.search("alpha beta", k=5, flags={"bm25": True}, where={"source": ["s1"]})
    assert out and all(c["meta"]["source"] == "s1" for c in out)


# ---------- backend: проброс filters + langs в stats ----------

class _Retr:
    def search(self, query, k=5, flags=None, mode="fast", where=None):
        self.where = where
        return []


def test_backend_search_passes_filters():
    retr = _Retr()
    lb = LocalBackend(Components(retriever=retr, cfg={}))
    lb.search("q", filters={"lang": ["python"]})
    assert retr.where == {"lang": ["python"]}


class _StatStore:
    def count(self):
        return 3

    def list_sources(self):
        return ["s1"]

    def list_langs(self):
        return ["python", "go"]


def test_stats_returns_langs():
    lb = LocalBackend(Components(store=_StatStore(), cfg={}))
    s = lb.stats()
    assert s["chunks"] == 3 and s["sources"] == ["s1"] and s["langs"] == ["python", "go"]


def test_stats_langs_fallback_to_registry():
    class _NoLangs(_StatStore):
        def list_langs(self):
            return []
    lb = LocalBackend(Components(store=_NoLangs(), cfg={}))
    assert lb.stats()["langs"]   # фолбэк из реестра парсеров - непустой


def test_citations_keep_score():
    from src.clients.backend import _citations
    chunks = [{"chunk_id": "a", "score": 0.83, "code": "...", "meta": {}},
              {"chunk_id": "b", "code": "..."}]            # без score - дефолт 0.0
    assert _citations(chunks) == [{"chunk_id": "a", "score": 0.83},
                                  {"chunk_id": "b", "score": 0.0}]
