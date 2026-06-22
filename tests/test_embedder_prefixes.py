from src.embeddings.local import LocalEmbedder, prefixes_for


def test_prefixes_for_e5():
    assert prefixes_for("intfloat/multilingual-e5-large") == ("query: ", "passage: ")
    # регистронезависимо
    assert prefixes_for("INTFLOAT/Multilingual-E5-Large") == ("query: ", "passage: ")


def test_prefixes_for_frida():
    assert prefixes_for("ai-forever/FRIDA") == ("search_query: ", "search_document: ")


def test_prefixes_for_other_none():
    assert prefixes_for("BAAI/bge-m3") is None
    assert prefixes_for("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2") is None


def _embedder(prefixes):
    # в обход __init__ (грузит модель) - только чистая логика _prep
    e = object.__new__(LocalEmbedder)
    e._prefixes = prefixes
    return e


def test_prep_e5_adds_prefix():
    e = _embedder(("query: ", "passage: "))
    assert e._prep(["x"], is_query=True) == ["query: x"]
    assert e._prep(["x"], is_query=False) == ["passage: x"]


def test_prep_no_prefix_passthrough():
    e = _embedder(None)
    assert e._prep(["x", "y"], is_query=True) == ["x", "y"]
