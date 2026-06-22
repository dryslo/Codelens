# evaluate.py - метрика и results.json

Считает Precision@5/Hit@5 той же логикой, что официальный `score.py`, и пишет `results.json` для официальной проверки.

```python
def parse_chunk_id(cid):
    parts = cid.rsplit(":", 2)
    if len(parts) != 3:
        return None
    try:
        return parts[0], parts[1], int(parts[2])
    except ValueError:
        return None
```
- Разбор `chunk_id` = `{path}:{name}:{line}`. `rsplit(":", 2)` (не `split`) - режет максимум 2 раза справа, потому что `path` может содержать `:` (редко, но и `name` нет) - так последние две части гарантированно `name` и `line`, остальное - путь. Идентично официальному скореру.
- Возвращается `(path, name, line:int)` или `None` при кривом формате (защита).

```python
def chunks_match(pred, ref, tol=2):
    p, r = parse_chunk_id(pred), parse_chunk_id(ref)
    if not p or not r:
        return False
    return p[0] == r[0] and p[1] == r[1] and abs(p[2] - r[2]) <= tol
```
- Совпадение: путь и имя - точно, строка - с допуском `±2` (как в `score.py`: код мог сдвинуться на пару строк). Без допуска точные эталоны не сошлись бы при малейшем расхождении нумерации.

```python
def score_question(top5, correct):
    matched, used = 0, set()
    for pred in dict.fromkeys(top5):
        for i, ref in enumerate(correct):
            if i not in used and chunks_match(pred, ref):
                matched += 1
                used.add(i)
                break
    return matched / min(5, len(correct))
```
- `dict.fromkeys(top5)` - дедуп предсказаний с сохранением порядка (один и тот же чанк не засчитывается дважды).
- Жадное сопоставление: каждый предсказанный чанк матчит не более одного эталона (`used` помечает занятые эталоны; `break` после первого совпадения).
- Знаменатель `min(5, len(correct))` - как в скорере: если эталонов 2, максимум 2/2=1.0; топ ограничен 5.

```python
def run_eval(backend, questions, mode="fast", flags=None, progress=None,
             embedder=None, retriever=None):
    q_embs = None
    if embedder is not None and retriever is not None:
        q_embs = embedder.encode([q["query"] for q in questions], is_query=True)
    results, p_sum, h_sum, n_scored = [], 0.0, 0.0, 0
    for i, q in enumerate(questions, 1):
        if q_embs is not None:
            hits = retriever.search(q["query"], k=5, mode=mode, flags=flags, query_emb=q_embs[i - 1])
        else:
            hits = backend.search(q["query"], k=5, mode=mode, flags=flags)
        top5 = [r["chunk_id"] for r in hits][:5]
        results.append({"question_id": q["question_id"], "top_5_chunks": top5})
        correct = q.get("correct_chunk_ids", [])
        if correct:
            p_sum += score_question(top5, correct)
            h_sum += 1.0 if any(chunks_match(t, c) for t in top5 for c in correct) else 0.0
            n_scored += 1
    n = max(1, n_scored)
    return results, p_sum / n, h_sum / n
```
- По каждому вопросу ищется top-5, берётся их `chunk_id` (формат scorer). Поля `query`/`question_id`/`correct_chunk_ids` - точно как в `eval_questions.json`. Накапливается Precision (`score_question`) и Hit (нашёлся ли хоть один эталон в топе); среднее - по вопросам с эталоном (`n_scored`).
- Локальный прогон (`embedder`+`retriever` заданы): все запросы эмбеддятся одним батчем, поиск идёт по готовым векторам через `query_emb` - быстрее, чем гонять каждый запрос через backend по очереди. Ретривер игнорирует `query_emb`, когда запрос меняется hyde/multiquery, поэтому путь безопасен при любых флагах.
- `flags` - `SearchFlags | dict | None`; при `None` берётся пресет из `mode`.

```python
EVAL_MATRIX = [
    ("01_dense", dict()),
    ("02_bm25",  dict(bm25=True)),
    ...
    ("11_all_no_rerank", dict(bm25=True, mmr=True, hyde=True, multiquery=True)),
]

def run_matrix(backend, questions, configs=None, mode="fast",
               on_config=None, on_progress=None):
    ...
```
- `run_matrix` гоняет матрицу конфигов из `docs/retrieval-eval.md` (по умолчанию `EVAL_MATRIX` - 11 комбинаций без реранкера) и для каждого возвращает precision/hit/время и `failures`. Используется вкладкой метрик для пер-компонентного замера вклада каждого канала.
- `collect_failures` собирает вопросы с `precision < 1.0`: ожидаемые/полученные chunk_id и тип (`miss` - ни одного эталона, `partial` - часть). Для разбора провалов retrieval.

```python
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default="data/eval_questions.json")
    ap.add_argument("--out", default="results.json")
    ap.add_argument("--preset", choices=["fast", "thinking"], default="fast")
    ap.add_argument("--assert-min", type=float, default=None)
    # --bm25/--no-bm25, --hyde, --rerank, --mmr, --multiquery, --k-cand, --mmr-lambda, ...
    args = ap.parse_args()
    questions = load_questions(args.questions)
    comp = build()
    backend = comp.backend
    flags = _build_flags_from_args(args)
    results, precision, hit = run_eval(backend, questions, mode=args.mode, flags=flags,
                                       embedder=comp.embedder, retriever=comp.retriever)
    ...
    if args.assert_min is not None and precision < args.assert_min:
        sys.exit(1)
```
- Аргументы CLI: вопросы, выходной файл, базовый `--preset` (`fast`/`thinking`) и индивидуальные `--xxx/--no-xxx`, которые его переопределяют (`_build_flags_from_args`). `--assert-min` - порог гейта.
- `main` гонит локальный прогон с батч-эмбеддингом (передаёт `embedder`/`retriever`), пишет `results.json` (`ensure_ascii=False` - кириллица читаемо; `indent=2`).
- `--assert-min`: если задан и Precision ниже порога - `sys.exit(1)` (ненулевой код → CI-гейт «красный», PR не вмёржится). Защита оценки от регрессий.

Перекрёстная проверка: после `evaluate.py` можно запустить официальный `python data/score.py --predictions results.json --questions data/eval_questions.json` - числа должны совпасть, т.к. логика сверки идентична.
