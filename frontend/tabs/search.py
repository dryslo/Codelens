"""Вкладка «Поиск»: запрос - retrieval, опц. ответ LLM, карточки."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

import streamlit as st

from frontend.components import flags_panel, render_card

if TYPE_CHECKING:
    from frontend.session import Ctx


def render(ctx: Ctx) -> None:
    """Рисует вкладку поиска: ввод запроса, флаги, фильтры, опциональный ответ LLM, карточки."""
    backend = ctx.backend
    stats = backend.stats()
    if not stats.get("chunks"):
        st.info("Индекс пуст. Добавьте код во вкладке «Админка».")
        return
    q = st.text_input("Вопрос о коде (RU/EN)", placeholder="например: где валидируется JWT-токен?")
    llms = backend.list_llms()
    flags = flags_panel(ctx.policy, "search", bool(llms))

    fc1, fc2 = st.columns(2)
    langs = fc1.multiselect("Языки", stats.get("langs") or [], placeholder="все языки")
    sources = fc2.multiselect("Источники", stats.get("sources") or [], placeholder="все источники")
    filters = {"lang": langs, "source": sources}

    c1, c2 = st.columns([1, 2])
    use_llm = c1.toggle("Ответ LLM", value=False, disabled=not llms)
    model = c2.selectbox("Модель", llms, disabled=not (use_llm and llms)) if llms else None
    if not q:
        return
    t0 = time.time()
    results = backend.search(q, k=5, flags=flags, filters=filters)
    active = [n for n, v in flags.to_dict().items() if isinstance(v, bool) and v]
    flt = [f"язык: {', '.join(langs)}"] if langs else []
    flt += [f"источник: {', '.join(sources)}"] if sources else []
    st.caption(f"{' · '.join(active) or 'dense only'}"
               f"{' · ' + ' · '.join(flt) if flt else ''} · "
               f"{time.time() - t0:.2f} c · {len(results)} результатов")
    if not results:
        st.warning("Ничего не найдено - попробуйте ослабить фильтры или переформулировать запрос.")
    if use_llm and model:
        md = None
        with st.container(border=True):
            try:                                   # write_stream возвращает полный текст
                md = st.write_stream(backend.answer_stream(q, results, model))
            except Exception:  # noqa: BLE001
                st.warning("LLM недоступна - показаны только фрагменты.")
        if md:
            safe_q = "".join(c if c.isalnum() else "_" for c in q)[:40].strip("_") or "answer"
            st.download_button("⬇️ Скачать ответ (.md)", data=md,
                               file_name=f"{safe_q}.md", mime="text/markdown")
    for r in results:
        render_card(r)
