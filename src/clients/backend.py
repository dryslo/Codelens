"""BackendClient: LocalBackend (в процессе, role=all/backend) и HttpBackend (frontend->backend)."""
from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from src.domain.interfaces import BackendClient
from src.persistence.cache import bump_epoch, cache_get_or_set, digest

if TYPE_CHECKING:
    from src.domain.interfaces import SessionStore
    from src.factory import Components

_SYS = (
    "Ты отвечаешь на вопросы по коду ТОЛЬКО на основе предоставленных фрагментов.\n"
    "Никогда не выдумывай файлы, функции или сигнатуры, которых нет в контексте.\n"
    "Если ответа в контексте нет - честно скажи об этом в первой строке.\n"
    "\n"
    "Формат ответа - валидный GitHub-Flavored Markdown:\n"
    "1. `## Разбор` - основной разбор; каждое утверждение поддерживай ссылками "
    "вида `[1]`, `[2]` на нужный фрагмент.\n"
    "2. Любой код - внутри fenced-блоков с указанием языка (```python, ```ts и т.п.).\n"
    "3. В конце `## Источники` - нумерованный список `[N] file::name (строки A–B)`.\n"
    "Не оборачивай весь ответ в один code-блок, не используй HTML."
)


def _citations(chunks: list[dict]) -> list[dict]:
    """Сжать результаты поиска до [{chunk_id, score}] для записи в историю чата.

    score сохраняем, чтобы карточки источников показывали ту же релевантность, что в поиске;
    code/meta не дублируем в БД - они подтягиваются из индекса по chunk_id при отрисовке.
    Дедуп по chunk_id (с сохранением порядка) - чтобы один фрагмент не попал в источники дважды.
    """
    out, seen = [], set()
    for c in chunks:
        cid = c.get("chunk_id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        out.append({"chunk_id": cid, "score": c.get("score", 0.0)})
    return out


def _ctx(chunks: list[dict]) -> str:
    """Собрать пронумерованный контекст из фрагментов для промпта."""
    parts = []
    for i, c in enumerate(chunks):
        m = c["meta"]
        head = (f"[{i + 1}] {m.get('file')}::{m.get('name')} "
                f"(строки {m.get('start_line')}–{m.get('end_line')})")
        parts.append(f"{head}\n{c['code']}")
    return "\n\n".join(parts)


class LocalBackend(BackendClient):
    """Backend в процессе (role=all/backend): поиск, чат, LLM, ingest, админка."""

    def __init__(self, comp: Components) -> None:
        """Запомнить компоненты приложения."""
        self.c = comp

    # --- кэш ---
    def _cache(self) -> SessionStore | None:
        """Вернуть активный кэш или None, если он выключен."""
        cache = self.c.cache
        return cache if (cache is not None and getattr(cache, "enabled", False)) else None

    def _cache_ttl(self) -> int:
        """Вернуть TTL кэша из конфига (секунды)."""
        return int((self.c.cfg.get("cache") or {}).get("ttl", 3600))

    def _invalidate_search(self) -> None:
        """Сдвинуть index-epoch - осиротить кэш поиска (после изменения индекса)."""
        cache = self._cache()
        if cache is not None:
            bump_epoch(cache)

    @staticmethod
    def _chat_key(chat_id: str) -> str:
        """Вернуть ключ кэша для списка сообщений чата."""
        return f"chat:{chat_id}:messages"

    def _invalidate_chat(self, chat_id: str) -> None:
        """Сбросить кэш сообщений чата (после append)."""
        cache = self._cache()
        if cache is not None:
            cache.set(self._chat_key(chat_id), None, ttl=1)   # tombstone: следующий get идёт в БД

    # --- поиск / LLM ---
    def search(self, query: str, k: int = 5, mode: str = "fast",
               flags: object | None = None, filters: dict | None = None) -> list[dict]:
        """Выполнить поиск через ретривер-оркестратор (опц. фильтр по lang/source)."""
        # поисковый кэш - внутри ретривера-оркестратора
        return self.c.retriever.search(query, k, flags=flags, mode=mode, where=filters)

    def list_llms(self) -> list[str]:
        """Вернуть список имён доступных LLM."""
        return list(self.c.llms.keys())

    def answer(self, query: str, chunks: list[dict], model: str) -> str:
        """Сгенерировать ответ по фрагментам с кэшированием результата."""
        key = f"answer:{digest({'q': query, 'ids': [c.get('chunk_id') for c in chunks], 'm': model})}"

        def produce() -> str:
            msgs = [{"role": "system", "content": _SYS},
                    {"role": "user", "content": f"{query}\n\nКонтекст:\n{_ctx(chunks)}"}]
            return self.c.llms[model].chat(msgs)

        return cache_get_or_set(self._cache(), key, produce, self._cache_ttl())

    def answer_stream(self, query: str, chunks: list[dict], model: str) -> Iterator[str]:
        """Стриминг одиночного ответа (вкладка Поиск).

        На hit - готовый текст одним чанком, иначе стрим с записью полной склейки в кэш.
        """
        key = f"answer:{digest({'q': query, 'ids': [c.get('chunk_id') for c in chunks], 'm': model})}"
        cache = self._cache()
        if cache is not None:
            hit = cache.get(key)
            if hit:
                yield hit
                return
        msgs = [{"role": "system", "content": _SYS},
                {"role": "user", "content": f"{query}\n\nКонтекст:\n{_ctx(chunks)}"}]
        parts: list[str] = []
        for delta in self.c.llms[model].chat_stream(msgs):
            parts.append(delta)
            yield delta
        if cache is not None:
            cache.set(key, "".join(parts), self._cache_ttl())

    # --- чат (conversational RAG) ---
    def _condense(self, history: list[dict], user_msg: str) -> str:
        """Свернуть историю и фоллоу-ап в один самостоятельный поисковый запрос."""
        fast = self.c.fast
        if not history or not fast or fast not in self.c.llms:
            return user_msg
        convo = "\n".join(f"{m['role']}: {m['content']}" for m in history[-6:])
        prompt = (f"Сформулируй ОДИН самостоятельный поисковый запрос.\n{convo}\n"
                  f"Фоллоу-ап: {user_msg}\nЗапрос:")
        return cache_get_or_set(
            self._cache(), f"condense:{digest({'c': convo, 'u': user_msg})}",
            lambda: self.c.llms[fast].chat([{"role": "user", "content": prompt}]),
            self._cache_ttl())

    def _gen_title(self, text: str, model: str | None) -> str:
        """Короткое название чата по первому сообщению (1 запрос к выбранной модели)."""
        llms = self.c.llms
        fallback = (text or "").strip()[:40] or "Новый чат"
        if not model or model not in llms:
            return fallback
        prompt = ("Придумай короткое название чата на русском, 3–5 слов, без кавычек, "
                  "эмодзи и финальной точки - по первому сообщению пользователя:\n" + text)
        try:
            out = llms[model].chat([{"role": "user", "content": prompt}])
        except Exception:
            return fallback
        title = (out or "").strip().strip('"').splitlines()[0][:60] if out else ""
        return title or fallback

    def chat(self, chat_id: str, user_msg: str, mode: str = "fast",
             model: str | None = None) -> dict:
        """Обработать ход чата (RAG): поиск, ответ модели, запись истории."""
        h = self.c.history
        llms = self.c.llms
        model = model or (next(iter(llms)) if llms else None)
        history = self.get_messages(chat_id)      # через кэш состояния чата
        first_turn = not history
        standalone = self._condense(history, user_msg)
        chunks = self.c.retriever.search(standalone, k=5, mode=mode)
        if model:
            msgs = ([{"role": "system", "content": _SYS}]
                    + [{"role": m["role"], "content": m["content"]} for m in history[-6:]]
                    + [{"role": "user", "content": f"{user_msg}\n\nКонтекст:\n{_ctx(chunks)}"}])
            answer = self.c.llms[model].chat(msgs)
        else:
            answer = "(LLM не настроена - показаны только найденные фрагменты.)"
        h.append(chat_id, "user", user_msg)
        h.append(chat_id, "assistant", answer,
                 citations=_citations(chunks), model=model, mode=mode)
        self._invalidate_chat(chat_id)            # сообщения изменились - сбросить кэш
        if first_turn:                            # имя чата - по первому запросу выбранной моделью
            h.rename(chat_id, self._gen_title(user_msg, model))
        return {"answer": answer, "citations": chunks}

    def chat_stream(self, chat_id: str, user_msg: str, mode: str = "fast",
                    model: str | None = None) -> Iterator[str]:
        """Как chat(), но ответ по токенам.

        История/название пишутся после стрима (когда генератор исчерпан потребителем).
        """
        h = self.c.history
        llms = self.c.llms
        model = model or (next(iter(llms)) if llms else None)
        history = self.get_messages(chat_id)
        first_turn = not history
        standalone = self._condense(history, user_msg)
        chunks = self.c.retriever.search(standalone, k=5, mode=mode)
        parts: list[str] = []
        if model:
            msgs = ([{"role": "system", "content": _SYS}]
                    + [{"role": m["role"], "content": m["content"]} for m in history[-6:]]
                    + [{"role": "user", "content": f"{user_msg}\n\nКонтекст:\n{_ctx(chunks)}"}])
            for delta in self.c.llms[model].chat_stream(msgs):
                parts.append(delta)
                yield delta
        else:
            fallback = "(LLM не настроена - показаны только найденные фрагменты.)"
            parts.append(fallback)
            yield fallback
        answer = "".join(parts)
        h.append(chat_id, "user", user_msg)
        h.append(chat_id, "assistant", answer,
                 citations=_citations(chunks), model=model, mode=mode)
        self._invalidate_chat(chat_id)
        if first_turn:
            h.rename(chat_id, self._gen_title(user_msg, model))

    def list_chats(self, user_id: str) -> list[dict]:
        """Вернуть список чатов пользователя."""
        return self.c.history.list_chats(user_id)

    def create_chat(self, user_id: str, title: str) -> str:
        """Создать чат и вернуть его идентификатор."""
        return self.c.history.create_chat(user_id, title)

    def get_messages(self, chat_id: str) -> list[dict]:
        """Вернуть сообщения чата (через кэш состояния)."""
        return cache_get_or_set(self._cache(), self._chat_key(chat_id),
                                lambda: self.c.history.get_messages(chat_id), self._cache_ttl())

    def delete_chat(self, chat_id: str) -> dict:
        """Удалить чат и сбросить его кэш."""
        self.c.history.delete_chat(chat_id)
        self._invalidate_chat(chat_id)
        return {"ok": True}

    # --- админка ---
    def stats(self) -> dict:
        """Вернуть статистику индекса: число чанков, источники и языки (для UI-фильтров)."""
        from src.indexing.parsers.base import registered_langs
        langs = self.c.store.list_langs() or registered_langs()
        return {"chunks": self.c.store.count(), "sources": self.c.store.list_sources(),
                "langs": langs}

    def index(self, folder: str, source: str, incremental: bool = True) -> dict:
        """Проиндексировать папку и осиротить кэш поиска."""
        res = self.c.index_path(folder, source, self.c.store, self.c.embedder,
                                   self.c.registry, incremental)
        self._invalidate_search()
        return res

    def remove(self, source: str) -> dict:
        """Удалить источник из индекса и осиротить кэш поиска."""
        res = self.c.remove_source(source, self.c.store, self.c.registry)
        self._invalidate_search()
        return res

    # --- ingest из админки (фоном через JobQueue): ZIP / GitHub ---
    # submit получает сериализуемый task (RQ не умеет замыкания), тело - src.ingest.runner.run_ingest.
    def ingest_zip(self, data: bytes, source: str) -> dict:
        """Поставить ingest ZIP-архива в очередь, вернуть job_id."""
        return {"job_id": self.c.jobs.submit({"kind": "zip", "source": source, "data": data})}

    def ingest_github(self, url: str, ref: str | None, source: str) -> dict:
        """Поставить ingest GitHub-репозитория в очередь, вернуть job_id."""
        return {"job_id": self.c.jobs.submit(
            {"kind": "github", "source": source, "url": url, "ref": ref})}

    def ingest_jobs(self) -> list[dict]:
        """Вернуть список ingest-job."""
        return self.c.jobs.list()

    def ingest_job(self, job_id: str) -> dict | None:
        """Вернуть состояние ingest-job по идентификатору."""
        return self.c.jobs.get(job_id)

    def flag_policy(self) -> dict:
        """Вернуть текущую политику флагов ретривера."""
        p = self.c.flag_policy
        return p.to_dict() if p else {}


class HttpBackend(BackendClient):
    """Backend через HTTP (role=frontend): проксирует запросы к backend-сервису."""

    def __init__(self, url: str) -> None:
        """Запомнить базовый URL backend-сервиса."""
        self.url = url.rstrip("/")
        self.token = None          # access-токен (Bearer), выставляется после login

    def _headers(self) -> dict:
        """Вернуть заголовки с Bearer-токеном, если он выставлен."""
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _post(self, path: str, payload: dict) -> dict:
        """Выполнить POST к backend, вернуть JSON-ответ."""
        import requests
        return requests.post(f"{self.url}{path}", json=payload,
                             headers=self._headers(), timeout=180).json()

    def _get(self, path: str) -> dict:
        """Выполнить GET к backend, вернуть JSON-ответ."""
        import requests
        return requests.get(f"{self.url}{path}", headers=self._headers(), timeout=30).json()

    # --- авторизация (frontend -> backend по HTTP) ---
    def register(self, login: str, password: str) -> dict:
        """Зарегистрировать пользователя через backend."""
        return self._post("/auth/register", {"login": login, "password": password})

    def login(self, login: str, password: str) -> dict:
        """Войти и запомнить access-токен."""
        res = self._post("/auth/login", {"login": login, "password": password})
        if res.get("access_token"):
            self.token = res["access_token"]
        return res

    def refresh(self, refresh_token: str) -> dict:
        """Обновить access-токен по refresh-токену."""
        res = self._post("/auth/refresh", {"refresh_token": refresh_token})
        if res.get("access_token"):
            self.token = res["access_token"]
        return res

    def logout(self) -> dict:
        """Выйти и сбросить access-токен."""
        res = self._post("/auth/logout", {})
        self.token = None
        return res

    def search(self, query: str, k: int = 5, mode: str = "fast",
               flags: object | None = None, filters: dict | None = None) -> list[dict]:
        """Выполнить поиск через backend (опц. фильтр по lang/source)."""
        payload = {"query": query, "k": k, "mode": mode}
        if flags is not None:
            payload["flags"] = flags if isinstance(flags, dict) else flags.to_dict()
        if filters:
            payload["filters"] = filters
        return self._post("/search", payload)["results"]

    def chat(self, chat_id: str, user_msg: str, mode: str = "fast",
             model: str | None = None) -> dict:
        """Обработать ход чата через backend."""
        return self._post("/chat", {"chat_id": chat_id, "user_msg": user_msg,
                                     "mode": mode, "model": model})

    def chat_stream(self, chat_id: str, user_msg: str, mode: str = "fast",
                    model: str | None = None) -> Iterator[str]:
        """Стримить ход чата по токенам через backend (SSE)."""
        import requests

        from src.util.sse import parse_lines
        with requests.post(f"{self.url}/chat/stream",
                           json={"chat_id": chat_id, "user_msg": user_msg,
                                 "mode": mode, "model": model},
                           headers=self._headers(), stream=True, timeout=180) as r:
            r.raise_for_status()
            yield from parse_lines(r.iter_lines(decode_unicode=True))

    def list_chats(self, user_id: str) -> list[dict]:
        """Вернуть список чатов пользователя (берётся из access-токена)."""
        return self._get("/chats")["chats"]   # сервер берёт пользователя из access-токена

    def create_chat(self, user_id: str, title: str) -> str:
        """Создать чат через backend, вернуть его идентификатор."""
        return self._post("/chats", {"user_id": user_id, "title": title})["chat_id"]

    def get_messages(self, chat_id: str) -> list[dict]:
        """Вернуть сообщения чата через backend."""
        return self._get(f"/chats/{chat_id}/messages")["messages"]

    def delete_chat(self, chat_id: str) -> dict:
        """Удалить чат через backend."""
        import requests
        return requests.delete(f"{self.url}/chats/{chat_id}",
                               headers=self._headers(), timeout=30).json()

    def list_llms(self) -> list[str]:
        """Вернуть список имён LLM через backend."""
        return self._get("/llms")["models"]

    def answer(self, query: str, chunks: list[dict], model: str) -> str:
        """Получить ответ по фрагментам через backend."""
        return self._post("/answer", {"query": query, "chunks": chunks, "model": model})["answer"]

    def answer_stream(self, query: str, chunks: list[dict], model: str) -> Iterator[str]:
        """Стримить ответ по фрагментам через backend (SSE)."""
        import requests

        from src.util.sse import parse_lines
        with requests.post(f"{self.url}/answer/stream",
                           json={"query": query, "chunks": chunks, "model": model},
                           headers=self._headers(), stream=True, timeout=180) as r:
            r.raise_for_status()
            yield from parse_lines(r.iter_lines(decode_unicode=True))

    def stats(self) -> dict:
        """Вернуть статистику индекса через backend."""
        return self._get("/admin/stats")

    def index(self, folder: str, source: str, incremental: bool = True) -> dict:
        """Запустить индексацию папки через backend."""
        return self._post("/admin/index", {"folder": folder, "source": source,
                                            "incremental": incremental})

    def remove(self, source: str) -> dict:
        """Удалить источник из индекса через backend."""
        return self._post("/admin/remove", {"source": source})

    def ingest_zip(self, data: bytes, source: str) -> dict:
        """Отправить ZIP-архив на ingest через backend."""
        import requests
        r = requests.post(f"{self.url}/admin/ingest/zip",
                          data={"source": source},
                          files={"file": ("upload.zip", data, "application/zip")},
                          headers=self._headers(), timeout=300)
        return r.json()

    def ingest_github(self, url: str, ref: str | None, source: str) -> dict:
        """Запустить ingest GitHub-репозитория через backend."""
        return self._post("/admin/ingest/github", {"url": url, "ref": ref, "source": source})

    def ingest_jobs(self) -> list[dict]:
        """Вернуть список ingest-job через backend."""
        return self._get("/admin/ingest/jobs").get("jobs", [])

    def ingest_job(self, job_id: str) -> dict | None:
        """Вернуть состояние ingest-job через backend."""
        return self._get(f"/admin/ingest/jobs/{job_id}")

    def flag_policy(self) -> dict:
        """Вернуть политику флагов ретривера через backend."""
        return self._get("/flag-policy")
