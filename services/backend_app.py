"""Backend-оркестратор (профиль large): REST поверх LocalBackend.

Группы роутеров:
  public    - /auth/*, /healthz, /flag-policy
  protected - search/chat/answer/llms (зависимость require_user)
  admin     - /admin/* (зависимость require_admin)
"""

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import StreamingResponse

from src.admin.router import router as admin_router
from src.auth.deps import require_user
from src.auth.router import router as auth_router
from src.factory import build
from src.persistence.schemas import AnswerReq, ChatReq, CreateChatReq, SearchReq
from src.util import metrics, sse

load_dotenv()  # локально читает .env; в compose/k8s файла нет, env уже в окружении
app = FastAPI(title="codelens-backend")
metrics.mount(app, "backend")        # /metrics + HTTP-латентность (no-op без prometheus)
COMP = build()
BACKEND = COMP.backend
app.state.auth = COMP.auth
app.state.backend = BACKEND
app.state.cfg = COMP.cfg


# --- защищённые роуты ---
protected = APIRouter(dependencies=[Depends(require_user)])


@protected.post("/search")
def search(r: SearchReq) -> dict:
    """Возвращает результаты retrieval по запросу с заданными флагами и фильтрами."""
    return {"results": BACKEND.search(r.query, r.k, r.mode, flags=r.flags, filters=r.filters)}


@protected.post("/chat")
def chat(r: ChatReq, user: dict = Depends(require_user)) -> dict:
    """Возвращает ответ на сообщение в чате."""
    return BACKEND.chat(r.chat_id, r.user_msg, r.mode, r.model)


@protected.post("/chat/stream")
def chat_stream(r: ChatReq, user: dict = Depends(require_user)) -> StreamingResponse:
    """Стримит ответ в чате дельтами в формате SSE."""
    src = BACKEND.chat_stream(r.chat_id, r.user_msg, r.mode, r.model)
    return StreamingResponse(sse.stream(src), media_type="text/event-stream")


@protected.get("/chats")
def list_chats(user: dict = Depends(require_user)) -> dict:
    """Возвращает список чатов пользователя."""
    return {"chats": BACKEND.list_chats(user["user_id"])}


@protected.post("/chats")
def create_chat(r: CreateChatReq, user: dict = Depends(require_user)) -> dict:
    """Создаёт чат и возвращает его идентификатор."""
    return {"chat_id": BACKEND.create_chat(user["user_id"], r.title)}


@protected.get("/chats/{chat_id}/messages")
def messages(chat_id: str) -> dict:
    """Возвращает сообщения чата."""
    return {"messages": BACKEND.get_messages(chat_id)}


@protected.delete("/chats/{chat_id}")
def delete_chat(chat_id: str) -> dict:
    """Удаляет чат."""
    return BACKEND.delete_chat(chat_id)


@protected.get("/llms")
def llms() -> dict:
    """Возвращает список доступных моделей."""
    return {"models": BACKEND.list_llms()}


@protected.post("/answer")
def answer(r: AnswerReq) -> dict:
    """Возвращает ответ LLM по запросу и переданным фрагментам."""
    return {"answer": BACKEND.answer(r.query, r.chunks, r.model)}


@protected.post("/answer/stream")
def answer_stream(r: AnswerReq) -> StreamingResponse:
    """Стримит ответ LLM дельтами в формате SSE."""
    src = BACKEND.answer_stream(r.query, r.chunks, r.model)
    return StreamingResponse(sse.stream(src), media_type="text/event-stream")


# --- публичные ---
@app.get("/flag-policy")
def flag_policy() -> dict:
    """Возвращает политику флагов retrieval."""
    return BACKEND.flag_policy()


@app.get("/healthz")
def healthz() -> dict:
    """Возвращает признак готовности сервиса."""
    return {"ok": True}


app.include_router(auth_router)
app.include_router(protected)
app.include_router(admin_router)
