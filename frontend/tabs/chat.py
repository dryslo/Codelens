"""Вкладка «Чат»: навигация по чатам в сайдбаре (на всех разделах) и диалог с вводом снизу."""
from __future__ import annotations

from typing import TYPE_CHECKING

import streamlit as st

if TYPE_CHECKING:
    from frontend.session import Ctx


def _welcome() -> None:
    """Пустой чат: описание по центру, чтобы ввод и история уходили вниз."""
    for _ in range(3):
        st.write("")
    _, mid, _ = st.columns([1, 2, 1])
    with mid, st.container(border=True):
        st.markdown("#### 💬 Диалог с вашим кодом")
        st.write("Ассистент находит релевантные фрагменты в индексе и отвечает по ним, "
                 "со ссылками на файлы и строки. Новый чат начнётся с первого вопроса, "
                 "название придумает выбранная модель.")
        st.markdown("Можно спросить, например:")
        st.markdown("- как устроена аутентификация?\n"
                    "- где обрабатываются фоновые задачи индексации?\n"
                    "- что делает функция `index_path` и кто её вызывает?")


def _draw_history(msgs: list[dict]) -> None:
    """Отрисовать сообщения диалога.

    Источники модель выводит секцией `## Источники` прямо в markdown-ответе (точный список из
    контекста), отдельных карточек нет - просто и без рассинхрона с индексом.
    """
    for m in msgs:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])


def render_sidebar(ctx: Ctx, chat_section: str) -> None:
    """Список чатов в сайдбаре. Вызывать на каждом разделе, чтобы чаты были видны везде.

    Клик по чату/«Новый чат» переключает активный раздел на чат (`chat_section`).
    """
    backend = ctx.backend
    chat_id = st.session_state.get("chat_id")
    chats = backend.list_chats(ctx.user_id)
    # Список нужен и заголовку чата в render(); сайдбар рисуется раньше на каждом ране, поэтому
    # кладём его в session_state и переиспользуем - без второго запроса в backend за тот же ран.
    st.session_state["_chats"] = chats
    titles = {c["id"]: (c["title"] or "Без названия") for c in chats}

    st.sidebar.markdown("### 💬 Чаты")
    if st.sidebar.button("➕ Новый чат", key="new_chat", width="stretch"):
        st.session_state.chat_id = None
        st.session_state.section = chat_section
        st.rerun()
    for ch in chats:
        title = titles[ch["id"]]
        label = title if len(title) <= 26 else title[:25] + "…"
        active = ch["id"] == chat_id
        row = st.sidebar.columns([5, 1])
        if row[0].button(label, key="chat_" + ch["id"], width="stretch", help=title,
                         type="primary" if active else "secondary"):
            st.session_state.chat_id = ch["id"]
            st.session_state.section = chat_section
            st.rerun()
        if row[1].button("🗑", key="del_" + ch["id"], help="Удалить чат"):
            backend.delete_chat(ch["id"])
            if st.session_state.get("chat_id") == ch["id"]:
                st.session_state.chat_id = None
            st.rerun()


def render(ctx: Ctx) -> None:
    """Рисует область чата: заголовок, историю с источниками и поле ввода снизу."""
    backend = ctx.backend
    user = ctx.user_id
    chat_id = st.session_state.get("chat_id")
    msgs = backend.get_messages(chat_id) if chat_id else []

    # Полный (необрезанный) заголовок активного чата сверху. Список чатов уже получен
    # render_sidebar в этом же ране - берём из session_state, не запрашивая повторно.
    if chat_id:
        titles = {c["id"]: (c["title"] or "Без названия") for c in st.session_state.get("_chats", [])}
        if chat_id in titles:
            st.markdown(f"### {titles[chat_id]}")

    # Тело - st.empty (один слот): при отправке вопроса очищаем его сразу, чтобы welcome
    # исчез ещё до обращения к LLM, а не висел всю генерацию (старый кадр держится до перерисовки).
    body = st.empty()

    # Выбор модели - справа, прямо над прибитым к низу полем ввода. chat_input верхнего
    # уровня (не в колонке/контейнере) остаётся внизу окна при любой прокрутке истории.
    chat_llms = backend.list_llms()
    _, model_col = st.columns([4, 1])
    with model_col:
        chat_model = st.selectbox("Модель", chat_llms, key="chat_model",
                                  label_visibility="collapsed") if chat_llms else None
    prompt = st.chat_input("Задайте вопрос о коде")

    if prompt:
        body.empty()                               # welcome исчезает немедленно
        is_new = not chat_id
        if is_new:
            chat_id = backend.create_chat(user, "Новый чат")
            st.session_state.chat_id = chat_id
        with body.container():
            _draw_history(msgs)                    # уже накопленная история (для нового чата пусто)
            with st.chat_message("user"):
                st.markdown(prompt)
            with st.chat_message("assistant"):
                st.write_stream(backend.chat_stream(chat_id, prompt, mode="fast", model=chat_model))
        # rerun только на первом ходе нового чата (показать его в сайдбаре и имя);
        # в существующем чате сообщения уже на экране - без повторного рендера и мигания.
        if is_new:
            st.rerun()
    else:
        with body.container():
            _draw_history(msgs) if msgs else _welcome()
