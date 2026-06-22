# retrieval - hyde.py, multiquery.py, mmr.py

Расширители запроса для думающего режима и MMR-диверсификация. Все опциональны и degradable.

## hyde.py - `HyDEExpander`
```python
class HyDEExpander:
    def expand(self, query: str) -> str:
        try:
            hypo = cache_get_or_set(self.cache, f"hyde:{digest(query)}",
                                    lambda: self.llm.hyde(query), self.cache_ttl)
            return f"{query}\n{hypo}"
        except Exception:
            return query
```
- Идея HyDE: LLM генерирует гипотетический фрагмент кода под вопрос, эмбеддится `query + гипотеза`. Гипотетический "ответ" по форме ближе к реальному коду, чем короткий вопрос → лучше матч (особенно RU-вопрос против EN-кода).
- `self.llm.hyde(query)` - короткий промпт (реализован в `BaseLLM`).
- Конкатенация `query\n<гипотеза>` - обе части эмбеддятся вместе, чтобы не потерять исходные термины запроса.
- Выход LLM кэшируется по запросу (`hyde:<digest>`): вызов дорогой, промпт детерминирован.
- `try/except → return query`: degradable - при сбое или недоступности LLM откат к обычному запросу, поиск не ломается.

## multiquery.py - `MultiQueryExpander`
```python
def expand_list(self, query: str, n: int | None = None) -> list[str]:
    n = n or self.n
    try:
        variants = cache_get_or_set(self.cache, f"mq:{n}:{digest(query)}",
                                    lambda: self.llm.multiquery(query, n), self.cache_ttl)
    except Exception:
        return [query]
    # дедупликация: provider может уже включить query первым
    ...
    return out[: n + 1]
```
- LLM даёт `n` переформулировок (RU+EN, синонимы auth/login/token).
- `expand_list(q)` → `[query, var1, ...]` для отдельных dense-выдач, которые сливаются через `rrf()`. `expand(q)` склеивает то же в одну строку для одиночной выдачи.
- Дедупликация по исходному запросу и пустым строкам, срез до `n + 1` (оригинал + n вариантов).
- Выход кэшируется по `(n, query)`; вызов дорогой, промпт детерминирован.
- Degradable: при сбое LLM возвращается `[query]`.

## mmr.py - `mmr`
```python
def mmr(query_vec, cand_vecs, k=5, lambda_=0.7) -> list[int]:
    # возвращает индексы кандидатов в порядке MMR-отбора
```
- Maximal Marginal Relevance - диверсификация: балансирует релевантность к запросу и непохожесть на уже выбранное, убирает почти-дубли из топа.
- `MMR_i = λ · sim(q, d_i) - (1-λ) · max_{j∈S} sim(d_i, d_j)`. `λ=1` - чистая релевантность, `λ=0` - чистая новизна, дефолт `0.7`.
- Возвращает индексы (не сами объекты), порядок отбора. Подключается после rerank/фьюжна, если в выдаче дубли одного файла.

Где собираются: `factory.build()` создаёт `HyDEExpander(llms[fast])`, если задан `llm.fast`, и передаёт его в `HybridRetriever`. MultiQuery подключается аналогично; MMR вызывается внутри `HybridRetriever.search` при `flags.mmr`.
