# retrieval/hybrid.py - `HybridRetriever`, RRF и нормализация score

Оркестратор поиска. Собирает dense, BM25, MultiQuery, HyDE, реранкер и MMR в один пайплайн, управляемый флагами на каждый запрос и финальной политикой из `config.yaml`. Возвращает кандидатов с полем `score ∈ [0, 1]`, которое UI отображает как «релевантность %».

## Внешние зависимости и контракт

```python
from src.domain.interfaces import Retriever
from src.retrieval.bm25 import BM25Index
from src.retrieval.flags import FlagsPolicy, SearchFlags
```

- `Retriever` - интерфейс с `search(query, k, flags=None, mode=None)`. Все клиенты (UI, чат, evaluate, REST) ходят через него.
- `BM25Index` - ленивый sparse-индекс по `store.iter_all()`, токенизирует код (snake_case/camelCase разбивается на части), переиспользуется между запросами.
- `SearchFlags` / `FlagsPolicy` - единое представление «какие каналы включены и с какими параметрами»:
  - `SearchFlags` - рабочий вектор флагов на запрос: `bm25`, `multiquery`, `hyde`, `rerank`, `mmr` (булевы) + параметры `k_cand` (10..200), `mmr_lambda` (0..1), `multiquery_n` (1..6).
  - `FlagsPolicy` - политика из `config.yaml → retrieval.flags`: каждому каналу выставляется `off` / `ui` / `fast` / `thinking`. Она же даёт `ui_visible()` (что показывать в тумблерах), `forced_for(mode)` (что зафиксировать) и `defaults(mode)` (стартовые значения).

## `rrf(rank_lists, k=60) -> dict[str, float]`

```python
def rrf(rank_lists, k=60) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranks in rank_lists:
        for pos, cid in enumerate(ranks):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + pos + 1)
    return scores
```

- Reciprocal Rank Fusion - слияние любого числа ранжированных списков id (dense-каналы, BM25, переформулировки MultiQuery).
- Контракт: возвращает `dict[id -> накопленный score]`, не отсортированный список. Сортировку выполняет вызывающая сторона: `sorted(rrf_scores, key=rrf_scores.get, reverse=True)`. Это нужно, чтобы потом тот же словарь использовать для UI-score (см. ниже «4.5»).
- Формула вклада: `1/(k + pos + 1)`. При `k=60` (стандарт из статьи RRF, 2009) - первая позиция даёт `1/61 ≈ 0.0164`, пятая - `1/65 ≈ 0.0154`. Зачем `k=60`: смягчает влияние абсолютной позиции - id, появившийся первым в одном списке и третьим в другом, обгоняет id, появившийся только первым в одном списке.
- Почему RRF, а не сумма косинусов и BM25-оценок: шкалы dense/BM25 несравнимы (cosine ∈ [-1, 1], BM25 не ограничен сверху, multiquery даёт разные кластеры значений). RRF работает только с рангами, не требует калибровки и устойчив к выбросам.

## Класс `HybridRetriever`

```python
class HybridRetriever(Retriever):
    def __init__(self, store, embedder, reranker=None, hyde=None, multiquery=None,
                 policy: FlagsPolicy | None = None):
        self.store = store
        self.embedder = embedder
        self.reranker = reranker
        self.hyde = hyde
        self.multiquery = multiquery
        self.bm25 = BM25Index(store)
        self.policy = policy or FlagsPolicy()
```

- DI всех зависимостей. `reranker / hyde / multiquery` опциональны (`None` = канал недоступен). Если их нет, соответствующие флаги молча игнорируются ниже.
- `BM25Index(store)` - индекс строится лениво на первый запрос с `flags.bm25=True`, держится в памяти процесса.
- `policy` - fallback на пустую политику (все каналы видны в UI, ничего не зафиксировано), чтобы `HybridRetriever` мог работать в тестах без конфига.

### `search(query, k=5, flags=None, mode=None)` - пошагово

#### Шаг 0 - нормализация флагов и политика

```python
if flags is None:
    flags = SearchFlags.from_mode(mode)
else:
    flags = SearchFlags.from_any(flags)
flags = self.policy.apply(flags, mode=mode)
```

- `flags` может прийти как `SearchFlags`, `dict` или `None`. `from_mode` строит дефолты для `fast` / `thinking`, `from_any` принимает любое из трёх.
- `policy.apply` - последнее слово: канал с `off` форсится в `False`, с `fast`/`thinking` - в `True` при соответствующем mode. UI-каналы остаются как есть.

#### Шаг 1 - расширение запроса (для dense)

```python
queries = [query]
exp = []
if flags.hyde and self.hyde is not None:
    exp.append(("hyde", lambda: self.hyde.expand(query)))
if flags.multiquery and self.multiquery is not None:
    exp.append(("mq", lambda: self.multiquery.expand_list(query, n=flags.multiquery_n)))
if exp:
    res = dict(zip([n for n, _ in exp], run_parallel([t for _, t in exp])))
    if "hyde" in res:
        queries[0] = res["hyde"]            # «query\nгипотеза»
    for v in res.get("mq", []):
        if v and v != query:
            queries.append(v)
```

- HyDE заменяет исходный запрос на «запрос + гипотетический код-фрагмент» (один LLM-вызов). Результат - одна строка с `\n`-разделителем.
- MultiQuery добавляет до `multiquery_n` независимых переформулировок (один LLM-вызов, дешевле чем `n` отдельных). Пустые строки и дубликаты исходного запроса отфильтровываются.
- HyDE и MultiQuery - независимые LLM-вызовы, выполняются параллельно через `run_parallel`.
- Итог - список из 1..(1+N) текстовых вариантов. BM25 идёт по исходному запросу (см. шаг 3).

#### Шаг 2 - dense-каналы

```python
q_embs = self.embedder.encode(queries, is_query=True)
rank_lists: list[list[str]] = []
by_id: dict[str, dict] = {}
for emb in q_embs:
    dense = self.store.query(emb, k=flags.k_cand)
    rank_lists.append([c["chunk_id"] for c in dense])
    for c in dense:
        by_id.setdefault(c["chunk_id"], c)
```

- Все варианты эмбеддятся одним батчем (e5 быстрее на батче, чем по одному).
- Для каждого эмбеддинга вытягиваем `k_cand` ближайших чанков. Каждый вытащенный чанк имеет поле `distance` (cosine distance ∈ [0, 2], при идентичных векторах = 0).
- `rank_lists` - для RRF: только id, без score.
- `by_id` - мэппинг для последующего поднятия полного объекта по id. `setdefault` гарантирует, что первый встреченный экземпляр чанка (со всеми его полями, включая `distance`) победит - это критично для шага 4.5.

#### Шаг 3 - BM25-канал

```python
if flags.bm25:
    lex = self.bm25.search(query, k=flags.k_cand)
    rank_lists.append([c["chunk_id"] for c in lex])
    for c in lex:
        by_id.setdefault(c["chunk_id"], c)
```

- Лексический канал работает по исходному запросу - расширения HyDE/MultiQuery ему не нужны: BM25 чувствителен к точным токенам, а HyDE-гипотезы их размывают.
- `setdefault` означает: если тот же chunk_id уже пришёл из dense, его dense-объект останется в `by_id` (с полем `distance`), а bm25-объект (с собственным `score`) будет проигнорирован - это намеренно: позже на шаге 4.5 выставляется единый RRF-score.

#### Шаг 4 - RRF-фьюжн

```python
if len(rank_lists) > 1:
    rrf_scores = rrf(rank_lists)
    fused_ids = sorted(rrf_scores, key=rrf_scores.get, reverse=True)
else:
    rrf_scores = None
    fused_ids = rank_lists[0] if rank_lists else []
cands = [by_id[i] for i in fused_ids if i in by_id][: flags.k_cand]
```

- Если каналов больше одного (dense + bm25, или dense + multiquery, …) - выполняется фьюжн. RRF-score сохраняется для шага 4.5.
- Если канал один (типично - чистый dense) - сохраняется исходный порядок, `rrf_scores = None` (на шаге 4.5 происходит переключение на cosine).
- Срез до `k_cand` - далее идут более тяжёлые шаги (rerank/MMR), нет смысла тащить весь хвост.

#### Шаг 4.5 - нормализация score для UI

```python
if rrf_scores:
    max_s = max(rrf_scores.values()) or 1.0
    for c in cands:
        c["score"] = rrf_scores.get(c["chunk_id"], 0.0) / max_s
else:
    for c in cands:
        if "distance" in c:
            c["score"] = max(0.0, min(1.0, 1.0 - float(c["distance"])))
        else:
            c.setdefault("score", 0.0)
```

- Зачем шаг существует: до этой правки UI всем строкам показывал `1%`. Dense-стор кладёт в кандидата `distance`, а не `score`. Старый код в конце метода ставил `c.setdefault("score", 1/(60+pos+1))` - это RRF-вклад первой позиции ≈ `0.0164` → `int(0.0164*100) = 1%`. Шаг 4.5 выставляет осмысленный `score ∈ [0, 1]` ещё до реранка/MMR, чтобы UI получил настоящую релевантность.
- Ветка `rrf_scores` (включён хотя бы один доп. канал): накопленный RRF нормируется на максимум по словарю → топ всегда даёт `1.0`, остальные пропорционально ниже. Почему min-max, а не raw RRF: raw-значения малы (десятые-сотые) и непредсказуемо зависят от `k=60` и числа каналов; нормализованный вариант стабильно ложится на шкалу процентов.
- Ветка cosine (только dense, RRF не применялся): `score = 1 - distance`, обрезанный в `[0, 1]`. При cosine-метрике `distance = 1 - cos_sim`, поэтому `1 - distance = cos_sim`. Срез снизу нулём защищает от отрицательной cos_sim (бывает при `-1`-нормализованных эмбеддингах), сверху единицей - от численного шума.
- `setdefault("score", 0.0)` - защитный случай: если кандидат пришёл из источника, не выставившего `distance`, метод не падает (но и не завышает релевантность).

#### Шаг 5 - реранкинг (опционально)

```python
if flags.rerank and self.reranker is not None:
    cands = self.reranker.rerank(query, cands,
                                 k=max(k * 4, k) if flags.mmr else k)
    for c in cands:
        c["score"] = 1.0 / (1.0 + math.exp(-float(c["score"])))
```

- Cross-encoder (`bge-reranker-v2-m3`) пересчитывает score по парам `(query, code)`. Это финальный сигнал релевантности, он заменяет RRF/cosine-оценку.
- Если дальше MMR - остаётся запас в `k * 4` кандидатов (MMR'у есть из чего диверсифицировать); иначе сразу `k`.
- Сигмоида: cross-encoder возвращает логит (может быть отрицательным и иметь произвольный масштаб). `sigmoid(logit)` приводит к `[0, 1]` и хорошо коррелирует с вероятностью релевантности у обученных reranker-моделей (для bge-reranker-v2-m3 это `~ P(relevant)`).

#### Шаг 6 - MMR-диверсификация (опционально)

```python
if flags.mmr:
    pool = cands[: max(k * 4, k)]
    emb_map = self.store.get_embeddings([c["chunk_id"] for c in pool])
    pairs = [(c, emb_map.get(c["chunk_id"])) for c in pool]
    pairs = [(c, v) for c, v in pairs if v is not None]
    if pairs:
        from src.retrieval.mmr import mmr as mmr_fn
        vecs = [v for _, v in pairs]
        idxs = mmr_fn(q_embs[0], vecs, k=k, lambda_=flags.mmr_lambda)
        cands = [pairs[i][0] for i in idxs]
    else:
        cands = pool[:k]
else:
    cands = cands[:k]
```

- Пул: топ `k*4` (тех же кандидатов, что переживут rerank, если он был). MMR делает trade-off «релевантность vs разнообразие» c параметром `λ = mmr_lambda`:
  - `λ = 1.0` - чистая релевантность (как `cands[:k]`),
  - `λ = 0.0` - чистое разнообразие (риск низкой релевантности),
  - типично `0.5..0.7`.
- Эмбеддинги тянутся из стора одним батчем (chroma делает один `get(where={"chunk_id": {"$in": ...}})`). Если по каким-то id эмбеддингов нет (был пересчёт модели, но не индекса) - MMR деградирует в `pool[:k]`.
- MMR ничего не делает с `score`: остаётся то, что выставил шаг 4.5 или 5.

#### Шаг 7 - возврат

```python
return cands
```

- Контракт ответа - список словарей с полями: `chunk_id`, `code`, `meta` (dict с `file/name/type/lang/start_line/end_line/source/...`), `score ∈ [0, 1]`. Поле `distance` может остаться (только не используется снаружи).

## Сводная таблица «какой score откуда»

| Сценарий                          | Источник `score`                                           |
| --------------------------------- | ---------------------------------------------------------- |
| Только dense                      | `1 - distance` (cosine similarity, clamped в [0, 1])       |
| Любой фьюжн (RRF)                 | `rrf_score / max(rrf_scores)` ∈ [0, 1]                      |
| После реранкера                   | `sigmoid(cross_encoder_logit)` ∈ [0, 1] (перекрывает выше) |
| MMR                               | не меняет score, только порядок                            |

## Почему класс, а не функция

`HybridRetriever` держит «дорогие» зависимости (`embedder`, `store`, `bm25` индекс, `reranker`) между вызовами. На каждый запрос выполняется только `search(...)` - без пересборки графа.

## Связанные файлы

- [src/retrieval/flags.py](../../src/retrieval/flags.py) - `SearchFlags`, `FlagsPolicy`, режимы.
- [src/retrieval/bm25.py](../../src/retrieval/bm25.py) - токенайзер и индекс.
- [src/retrieval/mmr.py](../../src/retrieval/mmr.py) - диверсификация.
- [src/retrieval/multiquery.py](../../src/retrieval/multiquery.py), [src/retrieval/hyde.py](../../src/retrieval/hyde.py) - расширители запроса (LLM-зависимые).
- [src/reranking/local.py](../../src/reranking/local.py) - cross-encoder, выставляет raw-логит, к которому здесь применяется сигмоида.
- [docs/retrieval-eval.md](../retrieval-eval.md) - матрица конфигов и P@5.
