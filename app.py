"""Frontend (Streamlit) - точка входа. Вся логика в пакете frontend/."""
import streamlit as st
from dotenv import load_dotenv

from frontend import session
from frontend.tabs import admin, chat, metrics, search

load_dotenv()
st.set_page_config(page_title="CodeLens", page_icon="🔍", layout="wide")

ctx = session.get_context()
session.ensure_authenticated(ctx)        # cookie-гейт и логин (st.stop при необходимости)
session.load_policy(ctx)

# Постоянная шапка: заголовок не привязан к разделу, виден на всех вкладках.
st.markdown("## 🔍 CodeLens")
st.caption("Умный поиск по кодовой базе")

# Навигация - segmented_control, а не st.tabs: st.tabs держит панели всех вкладок в DOM и
# прячет неактивные через CSS, а любой частичный rerun (авто-обновление прогресса ingest)
# сбрасывает это скрытие, и содержимое вкладки «Поиск» всплывает поверх. Рендерим только
# выбранный раздел - скрытых панелей нет, ломаться нечему.
CHAT_SECTION = "💬 Чат"
views = {"🔍 Поиск": search, CHAT_SECTION: chat}
if ctx.role == "admin":               # метрики и админка - только для администраторов
    views["📊 Метрики"] = metrics
    views["⚙️ Админка"] = admin
labels = list(views)

# Раздел держим в session_state, виджет синхронизируем до его создания. Без этого
# чтение возврата segmented_control с default на каждом ране приводило к двойному клику,
# а клик по активному разделу снимал выбор. on_change меняет раздел только при непустом выборе.
if st.session_state.get("section") not in labels:
    st.session_state.section = labels[0]
st.session_state.nav = st.session_state.section


def _on_nav() -> None:
    if st.session_state.nav:                 # клик по активному даёт None - не сбрасываем
        st.session_state.section = st.session_state.nav


st.segmented_control("Раздел", labels, key="nav",
                     label_visibility="collapsed", on_change=_on_nav)
st.divider()

chat.render_sidebar(ctx, CHAT_SECTION)   # список чатов в сайдбаре на всех разделах
views[st.session_state.section].render(ctx)

session.render_logout(ctx)               # профиль и «Выйти» внизу сайдбара
