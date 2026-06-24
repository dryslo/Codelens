"""Вкладка «Админка»: индекс (удаление источника, ingest ZIP/GitHub фоном)."""
from __future__ import annotations

from typing import TYPE_CHECKING

import streamlit as st

if TYPE_CHECKING:
    from frontend.session import Ctx

_ACTIVE = ("queued", "running")


def _panels(ctx: Ctx) -> None:
    """Ссылки на дашборды (Grafana/Adminer/Argo CD) - из cfg.ui.panels, пустые URL пропускаются.

    Панели гейтятся forward-auth по той же admin-сессии, что и Админка, поэтому ссылки видны
    только администратору. Источник URL - конфиг (Helm рендерит per-overlay, compose - env PANEL_*).
    """
    panels = (ctx.cfg.get("ui") or {}).get("panels") or {}
    links = [(name, url) for name, url in panels.items() if url]
    if not links:
        return
    st.subheader("📊 Дашборды")
    for col, (name, url) in zip(st.columns(len(links)), links):
        col.link_button(name, url, use_container_width=True)
    st.divider()


def _draw_jobs(ctx: Ctx) -> None:
    """Отрисовать прогресс-бары фоновых ingest-задач (снимок на текущий момент)."""
    jobs = ctx.backend.ingest_jobs()
    if not jobs:
        st.caption("Фоновых задач индексации нет.")
        return
    st.caption("Фоновые задачи индексации")
    for j in jobs:
        prog = j.get("progress", {})
        done, total = prog.get("chunks_indexed", 0), prog.get("chunks_total", 0)
        head = f"`{j.get('kind')}` · {j.get('source')} · {j.get('status')}"
        if j.get("status") in _ACTIVE:
            if total:
                st.progress(min(1.0, done / total), text=f"{head} - {done}/{total} чанков")
            else:
                st.progress(0.0, text=f"{head} - подготовка")
        else:
            st.write(f"{head} - {j.get('stats') or j.get('error') or 'готово'}")


@st.fragment(run_every="2s")
def _draw_jobs_polling(ctx: Ctx) -> None:
    """Тот же блок, но фрагмент сам перезапрашивает статусы каждые 2с (частичный rerun)."""
    _draw_jobs(ctx)


def _ingest_jobs(ctx: Ctx) -> None:
    """Блок задач индексации в Админке: поллим каждые 2с, пока есть активные, иначе снимок."""
    active = any(j.get("status") in _ACTIVE for j in ctx.backend.ingest_jobs())
    (_draw_jobs_polling if active else _draw_jobs)(ctx)


def render(ctx: Ctx) -> None:
    """Рисует вкладку администрирования: дашборды, управление индексом и фоновый ingest."""
    _panels(ctx)
    backend = ctx.backend
    s = backend.stats()
    st.metric("Чанков в индексе", s["chunks"])
    st.write("Источники:", s["sources"])
    src_del = st.selectbox("Удалить источник", s["sources"] or [""])
    if st.button("Удалить", type="primary") and src_del:
        st.warning(backend.remove(src_del))
        st.cache_resource.clear()

    st.divider()
    st.subheader("➕ Добавить код в индекс (фоном)")
    zt, gt = st.tabs(["📦 ZIP-загрузка", "🐙 GitHub-ссылка"])
    with zt:
        up = st.file_uploader("ZIP-архив с кодом", type=["zip"], key="ing_zip")
        zsrc = st.text_input("Имя источника", key="ing_zip_src")
        if st.button("Загрузить и индексировать", key="ing_zip_btn") and up and zsrc:
            res = backend.ingest_zip(up.getvalue(), zsrc)
            st.success(f"Запущено в фоне: job `{res.get('job_id')}`")
    with gt:
        gurl = st.text_input("GitHub URL (публичный)",
                             placeholder="https://github.com/owner/repo", key="ing_gh_url")
        gref = st.text_input("Ветка/тег (опц., по умолчанию main/master)", key="ing_gh_ref")
        gsrc = st.text_input("Имя источника", key="ing_gh_src")
        if st.button("Скачать и индексировать", key="ing_gh_btn") and gurl and gsrc:
            res = backend.ingest_github(gurl, gref or None, gsrc)
            st.success(f"Запущено в фоне: job `{res.get('job_id')}`")

    st.divider()
    st.subheader("📊 Фоновая индексация")
    _ingest_jobs(ctx)
