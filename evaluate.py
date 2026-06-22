"""Оценка по формуле data/score.py + генерация results.json.

Использование:
    python evaluate.py                                # Precision@5/Hit@5
    python evaluate.py --assert-min 0.60              # гейт для CI
    python evaluate.py --no-bm25 --hyde --rerank      # переключатели каналов
    python evaluate.py --preset thinking              # all-LLM пресет

Флаги пайплайна:
    --bm25/--no-bm25  --multiquery/--no-multiquery  --hyde/--no-hyde
    --rerank/--no-rerank  --mmr/--no-mmr
    --k-cand N  --mmr-lambda F  --multiquery-n N

Формат сверки идентичен data/score.py: chunk_id = {path}:{name}:{line}, ±2 строки.
"""
import argparse
import json
import sys
from dotenv import load_dotenv
from src.factory import build
from src.retrieval.flags import SearchFlags


def parse_chunk_id(cid: str) -> tuple[str, str, int] | None:
    """Разбирает chunk_id в (path, name, line); None при неверном формате."""
    parts = cid.rsplit(":", 2)
    if len(parts) != 3:
        return None
    try:
        return parts[0], parts[1], int(parts[2])
    except ValueError:
        return None


def chunks_match(pred: str, ref: str, tol: int = 2) -> bool:
    """Проверяет совпадение pred и ref: path и name точно, line в пределах ±tol."""
    p, r = parse_chunk_id(pred), parse_chunk_id(ref)
    if not p or not r:
        return False
    return p[0] == r[0] and p[1] == r[1] and abs(p[2] - r[2]) <= tol


def score_question(top5: list[str], correct: list[str]) -> float:
    """Считает Precision@5 одного вопроса: matched / min(5, len(correct))."""
    matched, used = 0, set()
    for pred in dict.fromkeys(top5):          # dedup с сохранением порядка
        for i, ref in enumerate(correct):
            if i not in used and chunks_match(pred, ref):
                matched += 1
                used.add(i)
                break
    return matched / min(5, len(correct))


def load_questions(path: str = "data/eval_questions.json") -> list:
    """Читает JSON-файл с вопросами оценки."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run_eval(backend: object, questions: list, mode: str = "fast", flags: object = None,
             progress: object = None, embedder: object = None, retriever: object = None) -> tuple:
    """Прогоняет вопросы через backend, считает Precision@5 / Hit@5.

    progress: callback(done, total) для прогресс-бара в UI.
    flags: SearchFlags | dict | None; при None берётся пресет из mode.
    embedder/retriever: при локальном прогоне все запросы эмбеддятся одним батчем,
        поиск идёт по готовым векторам. Ретривер игнорирует query_emb, если запрос
        меняется hyde/multiquery, поэтому путь безопасен при любых флагах.
    Возвращает (results_for_json, precision, hit_rate).
    """
    q_embs = None
    if embedder is not None and retriever is not None:
        q_embs = embedder.encode([q["query"] for q in questions], is_query=True)

    results, p_sum, h_sum, n_scored = [], 0.0, 0.0, 0
    total = len(questions)
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
        if progress:
            progress(i, total)
    n = max(1, n_scored)
    return results, p_sum / n, h_sum / n


def collect_failures(questions: list, results: list) -> list:
    """Вопросы, на которых retrieval ошибся (Hit@5 = 0 или Precision < 1).

    Для каждого: query, expected chunk_ids, top-5, тип ошибки (miss/partial), доля попаданий.
    """
    failures = []
    by_qid = {q["question_id"]: q for q in questions}
    for r in results:
        q = by_qid.get(r["question_id"])
        if not q:
            continue
        correct = q.get("correct_chunk_ids", [])
        if not correct:
            continue
        top5 = r["top_5_chunks"]
        hit = any(chunks_match(t, c) for t in top5 for c in correct)
        prec = score_question(top5, correct)
        if prec < 1.0:
            failures.append({
                "question_id": q["question_id"],
                "query": q["query"],
                "language": q.get("language"),
                "category": q.get("category"),
                "difficulty": q.get("difficulty"),
                "expected": correct,
                "got": top5,
                "precision": prec,
                "kind": "miss" if not hit else "partial",
            })
    return failures


# Матрица из docs/retrieval-eval.md: 11 конфигов без реранкера.
EVAL_MATRIX = [
    ("01_dense",                    dict()),
    ("02_bm25",                     dict(bm25=True)),
    ("03_mmr",                      dict(mmr=True)),
    ("04_hyde",                     dict(hyde=True)),
    ("05_multiquery",               dict(multiquery=True)),
    ("06_bm25_mmr",                 dict(bm25=True, mmr=True)),
    ("07_bm25_hyde",                dict(bm25=True, hyde=True)),
    ("08_bm25_multiquery",          dict(bm25=True, multiquery=True)),
    ("09_hyde_multiquery",          dict(hyde=True, multiquery=True)),
    ("10_bm25_hyde_multiquery",     dict(bm25=True, hyde=True, multiquery=True)),
    ("11_all_no_rerank",            dict(bm25=True, mmr=True, hyde=True, multiquery=True)),
]


def run_matrix(backend: object, questions: list, configs: object = None, mode: str = "fast",
               on_config: object = None, on_progress: object = None) -> list:
    """Прогон матрицы конфигов из docs/retrieval-eval.md.

    configs: список (label, flags_dict); по умолчанию EVAL_MATRIX (11 конфигов).
    on_config(label, idx, total) - callback на старте каждого конфига.
    on_progress(done_q, total_q) - callback по вопросам внутри текущего конфига.
    Возвращает [{label, flags, precision, hit, time, results, failures}, ...].
    """
    import time

    configs = configs or EVAL_MATRIX
    out = []
    for i, (label, fdict) in enumerate(configs, 1):
        if on_config:
            on_config(label, i, len(configs))
        flags = SearchFlags(**fdict)
        t0 = time.time()
        results, precision, hit = run_eval(backend, questions, mode=mode,
                                            flags=flags, progress=on_progress)
        out.append({
            "label": label,
            "flags": flags.to_dict(),
            "precision": precision,
            "hit": hit,
            "time": time.time() - t0,
            "results": results,
            "failures": collect_failures(questions, results),
        })
    return out


def _build_flags_from_args(args: argparse.Namespace) -> SearchFlags:
    flags = SearchFlags.from_mode(args.preset)
    for name in ("bm25", "multiquery", "hyde", "rerank", "mmr"):
        v = getattr(args, name)
        if v is not None:
            setattr(flags, name, v)
    if args.k_cand is not None:
        flags.k_cand = args.k_cand
    if args.mmr_lambda is not None:
        flags.mmr_lambda = args.mmr_lambda
    if args.multiquery_n is not None:
        flags.multiquery_n = args.multiquery_n
    return flags


def main() -> None:
    """Парсит флаги, прогоняет оценку, пишет results.json и применяет CI-гейт."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default="data/eval_questions.json")
    ap.add_argument("--out", default="results.json")
    ap.add_argument("--mode", default="fast",
                    help="legacy режим (только для отчёта; пресет задаётся через --preset)")
    ap.add_argument("--preset", choices=["fast", "thinking"], default="fast",
                    help="базовый набор флагов; индивидуальные --no-xxx/--xxx переопределяют")
    ap.add_argument("--assert-min", type=float, default=None)
    for name in ("bm25", "multiquery", "hyde", "rerank", "mmr"):
        ap.add_argument(f"--{name}", action=argparse.BooleanOptionalAction, default=None)
    ap.add_argument("--k-cand", type=int, default=None)
    ap.add_argument("--mmr-lambda", type=float, default=None)
    ap.add_argument("--multiquery-n", type=int, default=None)
    args = ap.parse_args()

    questions = load_questions(args.questions)
    comp = build()
    backend = comp.backend
    flags = _build_flags_from_args(args)
    print(f"Flags: {flags.to_dict()}")
    # Локальный прогон: батч-эмбеддинг всех запросов одним вызовом.
    results, precision, hit = run_eval(backend, questions, mode=args.mode, flags=flags,
                                       embedder=comp.embedder, retriever=comp.retriever)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"Precision@5: {precision:.1%}   Hit@5: {hit:.1%}   (вопросов: {len(questions)})")
    print(f"results.json -> {args.out}")

    if args.assert_min is not None and precision < args.assert_min:
        print(f"FAIL: Precision@5 ниже порога {args.assert_min:.0%}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    load_dotenv()
    main()
