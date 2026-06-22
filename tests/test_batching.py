from src.indexing import pipeline


class _CountingEmbedder:
    def __init__(self):
        self.calls = 0
        self.total = 0

    def encode(self, texts, is_query=False):
        self.calls += 1
        self.total += len(texts)
        return [[0.0] for _ in texts]


class _MemStore:
    def __init__(self):
        self.added = 0

    def delete_where(self, **conds):
        pass

    def add(self, ids, embs, metas, codes):
        self.added += len(ids)

    def count(self):
        return self.added


class _MemRegistry:
    def __init__(self):
        self.h = {}

    def get_hash(self, source, file):
        return self.h.get((source, file))

    def set_hash(self, source, file, x):
        self.h[(source, file)] = x

    def files(self, source):
        return [f for (s, f) in self.h if s == source]

    def remove(self, source, file=None):
        for key in [k for k in self.h if k[0] == source and (file is None or k[1] == file)]:
            self.h.pop(key)


def _make_corpus(tmp_path, n_files=3):
    for i in range(n_files):
        (tmp_path / f"m{i}.py").write_text(
            f"def a{i}():\n    return 1\n\ndef b{i}():\n    return 2\n", encoding="utf-8")


def test_index_path_batches_encode_across_files(tmp_path):
    _make_corpus(tmp_path, 3)
    emb, store, reg = _CountingEmbedder(), _MemStore(), _MemRegistry()
    stats = pipeline.index_path(str(tmp_path), "src", store, emb, reg, batch=256)
    assert emb.total == 6        # 3 файла * 2 функции
    assert emb.calls == 1        # один батч на весь корпус, а не по вызову на файл
    assert store.added == 6
    assert stats["added"] == 3


def test_index_path_respects_batch_size(tmp_path):
    _make_corpus(tmp_path, 3)
    emb, store, reg = _CountingEmbedder(), _MemStore(), _MemRegistry()
    pipeline.index_path(str(tmp_path), "src", store, emb, reg, batch=2)
    assert emb.total == 6
    assert emb.calls == 3        # 6 чанков при батче 2 дают 3 вызова encode
    assert store.added == 6


def test_index_path_incremental_skips_unchanged(tmp_path):
    _make_corpus(tmp_path, 2)
    emb, store, reg = _CountingEmbedder(), _MemStore(), _MemRegistry()
    pipeline.index_path(str(tmp_path), "src", store, emb, reg)
    calls_after_first = emb.calls
    stats = pipeline.index_path(str(tmp_path), "src", store, emb, reg)  # повтор без изменений
    assert emb.calls == calls_after_first    # повторного кодирования нет
    assert stats["skipped"] == 2
