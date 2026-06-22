"""Вкладка «Метрики»: разовый прогон Precision@5 и матричный прогон каналов."""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import streamlit as st

from frontend.components import flags_panel

if TYPE_CHECKING:
    from frontend.session import Ctx


def render(ctx: Ctx) -> None:
    """Рисует вкладку метрик: разовый прогон по выбранной конфигурации и матрица каналов."""
    st.subheader("📊 Оценка качества поиска")
    _single_eval(ctx)
    st.divider()
    _matrix_eval(ctx)


def _single_eval(ctx: Ctx) -> None:
    """Прогон Precision@5/Hit@5 по одной конфигурации каналов (из тумблеров)."""
    backend = ctx.backend
    st.markdown("#### Прогон с выбранной конфигурацией")
    llms = backend.list_llms()
    flags = flags_panel(ctx.policy, "eval", bool(llms))
    if not st.button("Прогнать Precision@5"):
        return
    try:
        from evaluate import load_questions, run_eval
        questions = load_questions()
        bar = st.progress(0.0, text=f"0 / {len(questions)}")

        def _on_progress(done: int, total: int) -> None:
            bar.progress(done / total, text=f"{done} / {total}")

        t0 = time.time()
        results, precision, hit = run_eval(backend, questions, flags=flags, progress=_on_progress)
        bar.empty()
        c1, c2, c3 = st.columns(3)
        c1.metric("Precision@5", f"{precision:.0%}")
        c2.metric("Hit@5", f"{hit:.0%}")
        c3.metric("⏱ c", f"{time.time() - t0:.1f}")
        st.caption(f"Конфиг: `{flags.to_dict()}`")
        st.download_button("Скачать results.json",
                           data=json.dumps(results, ensure_ascii=False, indent=2),
                           file_name="results.json", mime="application/json")
    except FileNotFoundError:
        st.warning("Нет data/eval_questions.json")


def _matrix_eval(ctx: Ctx) -> None:
    """Матричный прогон всех конфигураций каналов из EVAL_MATRIX."""
    backend = ctx.backend
    st.markdown("#### 🧪 Матричный прогон retrieval-каналов")
    st.caption("11 конфигураций из [docs/retrieval-eval.md](docs/retrieval-eval.md): "
               "dense baseline и все комбинации BM25/MMR/HyDE/MultiQuery (без реранкера). "
               "LLM-каналы (HyDE/MultiQuery) дают суммарное время прогона десятки минут.")
    forced_keys = list(ctx.policy.forced_for("fast"))
    if forced_keys:
        st.caption("⚠️ Часть каналов зафиксирована политикой: " + ", ".join(forced_keys)
                   + ". Метки конфигов в матрице могут расходиться с фактически прогнанными "
                   "флагами - поменяйте mode в config.yaml → retrieval.flags на `ui`.")
    only_baseline = st.checkbox("Только дешёвые конфиги (без HyDE/MultiQuery, 6 шт.)",
                                value=True,
                                help="Снять галку - будут гоняться все 11 конфигов "
                                     "(включая LLM-каналы, минуты на каждый).")
    if not st.button("Прогнать матрицу"):
        return
    try:
        from evaluate import EVAL_MATRIX, load_questions, run_matrix
        questions = load_questions()
        cfgs = [(lbl, f) for lbl, f in EVAL_MATRIX
                if (not only_baseline) or not (f.get("hyde") or f.get("multiquery"))]
        outer = st.progress(0.0, text=f"конфиг 0 / {len(cfgs)}")
        inner = st.progress(0.0, text="вопрос 0")
        status = st.empty()

        def _on_cfg(label: str, i: int, n: int) -> None:
            status.write(f"▶️ {label}")
            outer.progress((i - 1) / n, text=f"конфиг {i} / {n}: {label}")

        def _on_q(done: int, total: int) -> None:
            inner.progress(done / total, text=f"вопрос {done} / {total}")

        t0 = time.time()
        matrix = run_matrix(backend, questions, configs=cfgs, on_config=_on_cfg, on_progress=_on_q)
        outer.empty()
        inner.empty()
        status.empty()
        st.success(f"Готово за {time.time() - t0:.1f} c")

        rows = [{"конфиг": m["label"], "P@5": f"{m['precision']:.1%}", "Hit@5": f"{m['hit']:.1%}",
                 "время, c": f"{m['time']:.1f}", "ошибок": len(m["failures"])} for m in matrix]
        st.dataframe(rows, width="stretch", hide_index=True)

        st.markdown("#### Ошибки по конфигам")
        for m in matrix:
            if not m["failures"]:
                continue
            with st.expander(f"❌ {m['label']} - {len(m['failures'])} ошибок "
                             f"(P@5 {m['precision']:.1%})"):
                for f in m["failures"]:
                    kind_emoji = "🚫" if f["kind"] == "miss" else "⚠️"
                    st.markdown(f"{kind_emoji} **{f['question_id']}** "
                                f"({f.get('language', '?')}, {f.get('difficulty', '?')}) - "
                                f"P@5 {f['precision']:.0%}")
                    st.markdown(f"> {f['query']}")
                    c1, c2 = st.columns(2)
                    c1.caption("Ожидалось:")
                    c1.code("\n".join(f["expected"]), language="text")
                    c2.caption("Получено (top-5):")
                    c2.code("\n".join(f["got"]), language="text")

        st.download_button("Скачать matrix.json",
                           data=json.dumps(matrix, ensure_ascii=False, indent=2),
                           file_name="matrix.json", mime="application/json")
    except FileNotFoundError:
        st.warning("Нет data/eval_questions.json")
