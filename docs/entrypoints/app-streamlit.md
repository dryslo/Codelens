# app.py - Streamlit UI (frontend)

Тонкий клиент: вся работа идёт через `backend` (`LocalBackend` при `role=all`, `HttpBackend` при `role=frontend`). Ретривинг- и индексинг-логики во фронте нет - только сборка форм, рендер карточек и проксирование вызовов. Сам `app.py` - точка входа в 50 строк; код вынесен в пакет `frontend/`.

## Структура пакета

- `app.py` - бутстрап и навигация.
- `frontend/session.py` - composition root, контекст `Ctx`, авторизация и cookie-персист.
- `frontend/components.py` - общие виджеты: карточка результата, панель флагов.
- `frontend/tabs/{search,chat,metrics,admin}.py` - разделы, каждый со своим `render(ctx)`.

## Бутстрап `app.py`

```python
import streamlit as st
from dotenv import load_dotenv

from frontend import session
from frontend.tabs import admin, chat, metrics, search

load_dotenv()
st.set_page_config(page_title="CodeLens", page_icon="🔍", layout="wide")

ctx = session.get_context()
session.ensure_authenticated(ctx)        # cookie-гейт и логин (st.stop при необходимости)
session.load_policy(ctx)
```

- `load_dotenv()` подхватывает `.env` с ключами LLM, эндпоинтами и т. п. Идёт до сборки backend.
- `set_page_config(layout="wide")` - широкий лейаут под две колонки карточек и сайдбар с чатами.
- `get_context()` собирает `Ctx`: composition root (кэш на процесс), конфиг, признак включённой авторизации.
- `ensure_authenticated(ctx)` - логин-гейт. Без авторизации пользователь `anon` с ролью `admin`; иначе cookie-refresh или экран входа с `st.stop()`.
- `load_policy(ctx)` поднимает политику флагов retrieval из конфига через backend, а не чтением `config.yaml` напрямую. Это сознательно: при `role=frontend` UI ходит по HTTP в backend-service и получает политику от него - сервер диктует, что можно крутить.

### Навигация

```python
st.markdown("## 🔍 CodeLens")
st.caption("Умный поиск по кодовой базе")

CHAT_SECTION = "💬 Чат"
views = {"🔍 Поиск": search, CHAT_SECTION: chat}
if ctx.role == "admin":               # метрики и админка - только для администраторов
    views["📊 Метрики"] = metrics
    views["⚙️ Админка"] = admin
labels = list(views)

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
```

- Шапка рисуется постоянно, вне разделов - заголовок виден на всех вкладках.
- Навигация - `segmented_control`, а не `st.tabs`. `st.tabs` держит панели всех вкладок в DOM и прячет неактивные через CSS; любой частичный rerun (авто-обновление прогресса ingest) сбрасывает скрытие, и содержимое «Поиска» всплывает поверх. Здесь рендерится только выбранный раздел - скрытых панелей нет.
- Метрики и Админка добавляются в `views` только для роли `admin`; обычный пользователь видит лишь Поиск и Чат.
- Раздел держится в `session_state.section`, виджет синхронизируется до создания. Иначе чтение возврата `segmented_control` с default на каждом ране давало двойной клик, а клик по активному разделу снимал выбор. `on_change` меняет раздел только при непустом выборе.
- `render_sidebar` зовётся до рендера раздела, чтобы список чатов был в сайдбаре везде; `render_logout` - последним, чтобы профиль и «Выйти» были внизу сайдбара.

## Контекст и авторизация `frontend/session.py`

```python
@dataclass
class Ctx:
    comp: Components
    backend: object
    auth: object
    cfg: dict
    auth_on: bool
    user_id: str = "anon"
    role: str = "admin"
    policy: object = None
```

- `Ctx` - единый контекст сессии, прокидывается во вкладки. Собирается из `Components` (composition root) - dataclass из [factory](../factory.md), а не словарь.
- `_build_comp()` под `@st.cache_resource` строит composition root один раз на процесс. Без кэша на каждом нажатии тумблера заново поднимались бы e5-эмбеддер (~1 ГБ VRAM), BM25-индекс, открывался Chroma/Qdrant. Чистится явно через `st.cache_resource.clear()` после индексации/удаления источника.

### Cookie-персист и refresh

```python
REFRESH_COOKIE = "codelens_rt"
_cookies = CookieController()
```

- Refresh-токен живёт в cookie `codelens_rt`, access - в `st.session_state` и Bearer-заголовке backend.
- `_wait_for_cookies()` ждёт, пока cookie-компонент отдаст значения: до первого round-trip стор `None`, `set`/`get` падают, и логин не переживает F5. Ограничено пятью ранами, дальше работа без cookie.
- `_apply_session()` сохраняет выданную пару: память, Bearer и cookie с refresh. Cookie ставится `SameSite=Strict` против CSRF; `Secure` - в prod (HTTPS), управляется `auth.cookie_secure`.
- `ensure_authenticated()`: без авторизации - `anon`/`admin`. Иначе при отсутствии сессии читается refresh из cookie и обновляется (ротированный refresh переписывает cookie); протухший или отозванный - cookie удаляется и показывается экран входа.
- `auth is not None` (HTTP-режим, отдельный `AuthClient`) против fallback на методы backend - один и тот же контракт login/register/refresh/logout.

## Карточка результата `render_card` (`frontend/components.py`)

```python
def render_card(r: dict) -> None:
    m = r["meta"]
    pct = max(0, min(100, int(r.get("score", 0) * 100)))
    with st.container(border=True):
        head, badge = st.columns([5, 1], vertical_alignment="center")
        head.markdown(f"**`{m.get('file')}`** · {m.get('type')} `{m.get('name')}` · "
                      f"строки {m.get('start_line')}-{m.get('end_line')} · "
                      f"источник `{m.get('source')}`")
        badge.markdown(f"`{m.get('lang', '?')}` · **{pct}%**")
        st.code(r["code"], language=m.get("lang", "python"))
```

- `pct` = `score * 100`, обрезанный в `[0, 100]`. Срез защищает от выхода за границы при численном шуме (cosine-ветка в `HybridRetriever`, где `score = 1 - distance` может слегка вылезти).
- Бордер контейнера отделяет карточки друг от друга. Заголовок - одна markdown-строка с разделителями `·`: путь, тип сущности, имя, строки `A-B`, источник; язык и процент релевантности вынесены в badge справа.
- Тело - `st.code(..., language=...)` для подсветки. Язык из метаданных чанка, fallback `python` - большая часть индексируемого кода в проекте на Python (см. [docs/indexing/parsers.md](../indexing/parsers.md)).
- Откуда корректный `score`: шаг 4.5 в [docs/retrieval/hybrid.md](../retrieval/hybrid.md).

## Панель флагов `flags_panel` (`frontend/components.py`)

```python
_FLAG_LABELS = {"bm25": "BM25", "multiquery": "MultiQuery", "hyde": "HyDE",
                "rerank": "Rerank", "mmr": "MMR"}
_FLAG_HELP = {
    "bm25": "лексический канал, фьюзится с dense через RRF",
    "multiquery": "LLM генерит N переформулировок, каждая ищется отдельно",
    "hyde": "LLM генерит гипотетический код, добавляется к запросу",
    "rerank": "кросс-энкодер по топ-N кандидатам (тяжелее)",
    "mmr": "диверсификация финальной выдачи",
}
_LLM_DEPENDENT = {"multiquery", "hyde"}
```

- `_FLAG_LABELS` - подписи тумблеров, `_FLAG_HELP` - тултипы по `?`.
- `_LLM_DEPENDENT` - каналы, бессмысленные без LLM-провайдера. Отключаются (`disabled`), а не скрываются - видно, что фича есть, но недоступна без конфигурации.

```python
def flags_panel(policy: object, key_prefix: str, llms_available: bool,
                mode: str = "fast") -> SearchFlags:
    ui_flags = policy.ui_visible()
    forced = policy.forced_for(mode)
    defaults = policy.defaults(mode=mode)
```

- `policy` приходит из `ctx.policy`, заполненного `load_policy` (а не из глобали).
- `key_prefix` - префикс для `key=` виджетов. Одна и та же панель рисуется на двух вкладках (Поиск и Метрики); без префикса Streamlit ругался бы на дубликаты ключей или делил состояние между вкладками.
- `llms_available` вычисляется снаружи через `bool(backend.list_llms())`.
- `mode` - `"fast"` или `"thinking"`. В UI не выбирается (упрощение), всегда `"fast"`.
- `ui_visible()` - флаги, помеченные в `config.yaml` как `ui`. `forced_for(mode)` - словарь `{flag: bool}` каналов, зафиксированных политикой. `defaults(mode)` - `SearchFlags` со стартовыми значениями.

```python
    values: dict[str, bool] = dict(forced)  # forced - каркас
    if ui_flags:
        cols = st.columns(len(ui_flags))
        for col, name in zip(cols, ui_flags):
            disabled = name in _LLM_DEPENDENT and not llms_available
            values[name] = col.toggle(
                _FLAG_LABELS[name],
                value=st.session_state.get(f"{key_prefix}_{name}", getattr(defaults, name)),
                key=f"{key_prefix}_{name}", disabled=disabled, help=_FLAG_HELP[name])
    else:
        st.caption("Все каналы зафиксированы политикой - UI-тумблеров нет.")
```

- `values` стартует с принудительных каналов - даже не попав в `ui_flags`, они уходят наружу.
- Тумблеры рисуются в линию из `len(ui_flags)` колонок. Значение берётся из `session_state` (переживает перерисовку), fallback на `defaults.{name}`. `disabled` - только для HyDE/MultiQuery без LLM.
- Числовые параметры (`k_cand`, MMR λ, MultiQuery N) берутся из конфигурации (дефолты `SearchFlags`) и в UI не настраиваются - оставлены только тумблеры каналов. Итоговый `SearchFlags` собирается из `values` и этих дефолтов и уходит в backend.
- Плашка `Принудительно политикой: …` показывает зафиксированные каналы; если включён HyDE/MultiQuery без LLM - отдельный caption, что канал будет проигнорирован.

## Вкладка «Поиск» (`frontend/tabs/search.py`)

```python
def render(ctx: Ctx) -> None:
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
```

- При пустом индексе - подсказка и выход, без формы.
- `flags_panel(ctx.policy, "search", ...)` - префикс `search` отделяет состояние от вкладки Метрик.
- Фильтры по языку и источнику - `multiselect` из `stats`, передаются в `search` отдельным словарём (пустой = без фильтра).

```python
    c1, c2 = st.columns([1, 2])
    use_llm = c1.toggle("Ответ LLM", value=False, disabled=not llms)
    model = c2.selectbox("Модель", llms, disabled=not (use_llm and llms)) if llms else None
    if not q:
        return
    t0 = time.time()
    results = backend.search(q, k=5, flags=flags, filters=filters)
    active = [n for n, v in flags.to_dict().items() if isinstance(v, bool) and v]
```

- Тумблер «Ответ LLM» и селектор модели: модель `disabled`, пока тумблер не включён.
- Поиск только при непустом запросе. `k=5` зашит - UI рассчитан на 5 карточек.
- Caption перечисляет реально работавшие каналы: фильтрация `flags.to_dict()` по булевым значениям (числовые параметры отсекаются), добавляются активные фильтры и latency. Пусто - «dense only» (dense есть всегда).

```python
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
```

- Ответ стримится через `answer_stream`/`st.write_stream` в `st.container(border=True)` - нейтральная подложка, на которой markdown (заголовки, fenced-code, списки) рисуется стандартно, в отличие от `st.info`-callout. `write_stream` отдаёт полный текст для кнопки скачивания.
- Ответ - сразу валидный markdown-файл (контракт LLM, см. [backend-client](../clients/backend-client.md)); те же байты идут в «Скачать .md» без пост-процессинга.
- Имя файла - slug запроса: буквы/цифры остаются, прочее → `_`, обрезка до 40 символов, fallback `answer`.
- Degradable LLM: при недоступном провайдере рисуется `warning`, карточки показываются всё равно. Retrieval не должен падать из-за LLM.
- Карточки идут после ответа - сперва резюме, потом доказательная база.

## Вкладка «Чат» (`frontend/tabs/chat.py`)

Навигация по чатам рисуется в сайдбаре на всех разделах (`render_sidebar`), сам диалог - в области раздела (`render`).

```python
def render_sidebar(ctx: Ctx, chat_section: str) -> None:
    backend = ctx.backend
    chat_id = st.session_state.get("chat_id")
    chats = backend.list_chats(ctx.user_id)
    ...
    if st.sidebar.button("➕ Новый чат", key="new_chat", width="stretch"):
        st.session_state.chat_id = None
        st.session_state.section = chat_section
        st.rerun()
```

- Список чатов берётся по `ctx.user_id`. Клик по чату или «Новый чат» переключает активный раздел на чат (`chat_section`) и проставляет `chat_id`.
- Длинные названия обрезаются с многоточием; активный чат подсвечивается `type="primary"`. Рядом кнопка удаления - при удалении текущего чата сбрасывается `chat_id`.

```python
def render(ctx: Ctx) -> None:
    ...
    body = st.empty()

    chat_llms = backend.list_llms()
    _, model_col = st.columns([4, 1])
    with model_col:
        chat_model = st.selectbox("Модель", chat_llms, key="chat_model",
                                  label_visibility="collapsed") if chat_llms else None
    prompt = st.chat_input("Задайте вопрос о коде")
```

- Тело диалога - один слот `st.empty()`: при отправке вопроса он очищается сразу, чтобы welcome исчез до обращения к LLM, а не висел всю генерацию (старый кадр держится до перерисовки).
- Выбор модели - над полем ввода. `chat_input` верхнего уровня (не в колонке/контейнере) остаётся прибит к низу окна при любой прокрутке истории.

```python
    if prompt:
        body.empty()                               # welcome исчезает немедленно
        is_new = not chat_id
        if is_new:
            chat_id = backend.create_chat(user, "Новый чат")
            st.session_state.chat_id = chat_id
        with body.container():
            _draw_history(msgs)                    # уже накопленная история
            with st.chat_message("user"):
                st.markdown(prompt)
            with st.chat_message("assistant"):
                st.write_stream(backend.chat_stream(chat_id, prompt, mode="fast", model=chat_model))
        if is_new:
            st.rerun()
    else:
        with body.container():
            _draw_history(msgs) if msgs else _welcome()
```

- Новый чат заводится с первого вопроса; название придумывает модель.
- Ответ стримится через `chat_stream` (всегда `mode="fast"` - «думающий» режим зарезервирован для матричных прогонов).
- `st.rerun()` только на первом ходе нового чата - показать его в сайдбаре и имя. В существующем чате сообщения уже на экране, без повторного рендера и мигания.
- Источники модель выводит секцией `## Источники` прямо в markdown-ответе (точный список из контекста) - отдельных карточек нет, без рассинхрона с индексом.
- Пустой чат показывает welcome с примерами вопросов по центру.

## Вкладка «Метрики» (`frontend/tabs/metrics.py`)

```python
def render(ctx: Ctx) -> None:
    st.subheader("📊 Оценка качества поиска")
    _single_eval(ctx)
    st.divider()
    _matrix_eval(ctx)
```

### Разовый прогон

```python
def _single_eval(ctx: Ctx) -> None:
    backend = ctx.backend
    flags = flags_panel(ctx.policy, "eval", bool(llms))
    if not st.button("Прогнать Precision@5"):
        return
    from evaluate import load_questions, run_eval
    questions = load_questions()
    bar = st.progress(0.0, text=f"0 / {len(questions)}")
    ...
    results, precision, hit = run_eval(backend, questions, flags=flags, progress=_on_progress)
```

- Второй экземпляр панели флагов - префикс `eval`, состояние отделено от Поиска (можно проверять другую конфигурацию, не сбивая поисковую).
- `evaluate.load_questions`/`run_eval` - внешний модуль ([evaluate.py](../../evaluate.py)). `run_eval` принимает коллбэк `progress(done, total)` → обновляется Streamlit-`progress`.
- Три метрики рядом: `Precision@5`, `Hit@5`, общее время; `f"{x:.0%}"` форматирует `0.42 → '42%'`.
- `results.json` отдаётся `download_button` (UTF-8, для анализа в pandas/jq). Отсутствие датасета ловится как `FileNotFoundError` и переводится в предупреждение.

### Матричный прогон

```python
def _matrix_eval(ctx: Ctx) -> None:
    forced_keys = list(ctx.policy.forced_for("fast"))
    if forced_keys:
        st.caption("⚠️ Часть каналов зафиксирована политикой: " + ", ".join(forced_keys) + ...)
    only_baseline = st.checkbox("Только дешёвые конфиги (без HyDE/MultiQuery, 6 шт.)", value=True, ...)
    if not st.button("Прогнать матрицу"):
        return
    from evaluate import EVAL_MATRIX, load_questions, run_matrix
    cfgs = [(lbl, f) for lbl, f in EVAL_MATRIX
            if (not only_baseline) or not (f.get("hyde") or f.get("multiquery"))]
```

- Матрица гоняет фиксированные комбинации флагов из `EVAL_MATRIX` ([evaluate.py](../../evaluate.py)), измеряя P@5/Hit@5 и время. 11 конфигов: dense baseline и комбинации BM25/MMR/HyDE/MultiQuery без реранкера. Подробности - в [docs/retrieval-eval.md](../retrieval-eval.md).
- Предупреждение про политику появляется при наличии `off`/`fast`/`thinking`-флагов: метка конфига (`+bm25 +mmr`) тогда расходится с реально прогнанным набором (политика перебивает). Для честной матрицы - перевести флаги в `ui`.
- `only_baseline` по умолчанию on: HyDE/MultiQuery дают десятки LLM-вызовов на вопрос, минуты на конфиг.

```python
    outer = st.progress(0.0, text=f"конфиг 0 / {len(cfgs)}")
    inner = st.progress(0.0, text="вопрос 0")
    status = st.empty()
    ...
    matrix = run_matrix(backend, questions, configs=cfgs, on_config=_on_cfg, on_progress=_on_q)
    outer.empty()
    inner.empty()
    status.empty()
```

- Два прогресс-бара: внешний по конфигам, внутренний по вопросам внутри конфига. `status = st.empty()` - плейсхолдер под имя текущего конфига.
- После окончания все три виджета чистятся `.empty()`, чтобы не дублировать с финальным `success`.

```python
    rows = [{"конфиг": m["label"], "P@5": f"{m['precision']:.1%}", ...} for m in matrix]
    st.dataframe(rows, width="stretch", hide_index=True)
    ...
    for m in matrix:
        if not m["failures"]:
            continue
        with st.expander(f"❌ {m['label']} - {len(m['failures'])} ошибок ..."):
            for f in m["failures"]:
                kind_emoji = "🚫" if f["kind"] == "miss" else "⚠️"
                ...
                c1.code("\n".join(f["expected"]), language="text")
                c2.code("\n".join(f["got"]), language="text")
```

- Сводка - `st.dataframe`, `hide_index=True` (имена конфигов уже в колонке).
- Разбор ошибок: на конфиг с провалами - `st.expander`. По ошибке: `kind` `miss` (🚫, не нашли) или `partial` (⚠️, нашли часть), цитата вопроса, две колонки «Ожидалось / Получено» со списками `chunk_id`; `language="text"` - это идентификаторы, не код.
- `matrix.json` - сырой дамп для оффлайн-анализа, тот же JSON, что выводит `make eval-matrix`.

## Вкладка «Админка» (`frontend/tabs/admin.py`)

Раздел доступен только роли `admin`. Управление индексом и фоновый ingest.

```python
def render(ctx: Ctx) -> None:
    backend = ctx.backend
    s = backend.stats()
    st.metric("Чанков в индексе", s["chunks"])
    st.write("Источники:", s["sources"])
    src_del = st.selectbox("Удалить источник", s["sources"] or [""])
    if st.button("Удалить", type="primary") and src_del:
        st.warning(backend.remove(src_del))
        st.cache_resource.clear()
```

- `stats()` отдаёт `{chunks, sources, langs}`.
- Удаление источника помечено `type="primary"` (красная) как предупреждение. После удаления - `st.cache_resource.clear()`: иначе кешированный backend держит старое представление BM25-индекса (Chroma пишет на диск сразу, BM25 в памяти - нет).

### Фоновый ingest

```python
    zt, gt = st.tabs(["📦 ZIP-загрузка", "🐙 GitHub-ссылка"])
    with zt:
        up = st.file_uploader("ZIP-архив с кодом", type=["zip"], key="ing_zip")
        zsrc = st.text_input("Имя источника", key="ing_zip_src")
        if st.button("Загрузить и индексировать", key="ing_zip_btn") and up and zsrc:
            res = backend.ingest_zip(up.getvalue(), zsrc)
            st.success(f"Запущено в фоне: job `{res.get('job_id')}`")
    with gt:
        gurl = st.text_input("GitHub URL (публичный)", ...)
        gref = st.text_input("Ветка/тег (опц., по умолчанию main/master)", ...)
        gsrc = st.text_input("Имя источника", key="ing_gh_src")
        if st.button("Скачать и индексировать", key="ing_gh_btn") and gurl and gsrc:
            res = backend.ingest_github(gurl, gref or None, gsrc)
```

- Два источника кода: ZIP-загрузка и публичная GitHub-ссылка. Оба запускают индексацию фоном через `ingest_zip`/`ingest_github` и возвращают `job_id` (см. [docs/indexing/pipeline.md](../indexing/pipeline.md)).

```python
_ACTIVE = ("queued", "running")


@st.fragment(run_every="2s")
def _draw_jobs_polling(ctx: Ctx) -> None:
    _draw_jobs(ctx)


def _ingest_jobs(ctx: Ctx) -> None:
    active = any(j.get("status") in _ACTIVE for j in ctx.backend.ingest_jobs())
    (_draw_jobs_polling if active else _draw_jobs)(ctx)
```

- Блок задач индексации показывает прогресс-бары `ingest_jobs()`. Пока есть активные (`queued`/`running`), рисуется `@st.fragment(run_every="2s")` - фрагмент сам перезапрашивает статусы каждые 2с частичным rerun, не перерисовывая страницу. Активных нет - разовый снимок без поллинга.

## Почему всё через `backend.*`

При замене Streamlit на React/Next.js пишется новый фронт, но контракт `BackendClient` и `LocalBackend` не меняются. То же с заменой `LocalBackend` на `HttpBackend`: переключение «в процессе / по HTTP» происходит в [factory](../factory.md), а фронт про это не знает.

## Связанные файлы

- [src/factory.py](../factory.md) - собирает `Components`, откуда фронт берёт backend и auth.
- [src/clients/backend.py](../clients/backend-client.md) - `answer_stream`/`chat_stream`, чьи markdown-ответы здесь рендерятся.
- [src/retrieval/hybrid.py](../retrieval/hybrid.md) - выставляет `score`, который превращается в `pct`.
- [src/retrieval/flags.py](../../src/retrieval/flags.py) - `SearchFlags`, `FlagsPolicy`.
- [evaluate.py](../../evaluate.py) - `load_questions`, `run_eval`, `EVAL_MATRIX`, `run_matrix`.
