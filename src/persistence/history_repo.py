"""SQL-реализация `History`: чаты и сообщения."""
import json
import uuid
from typing import Any

from src.domain.interfaces import History
from src.persistence.orm import Chat, Message


class SqlHistory(History):
    """История чатов поверх таблиц `chats` и `messages`."""

    def __init__(self, session_factory: Any) -> None:
        self.Session = session_factory

    def create_chat(self, user_id: str, title: str) -> str:
        """Создать чат и вернуть его идентификатор."""
        cid = str(uuid.uuid4())
        with self.Session() as s:
            s.add(Chat(id=cid, user_id=user_id, title=title))
            s.commit()
        return cid

    def list_chats(self, user_id: str) -> list[dict]:
        """Вернуть чаты пользователя в порядке убывания даты."""
        with self.Session() as s:
            rows = (s.query(Chat).filter_by(user_id=user_id)
                    .order_by(Chat.created_at.desc()).all())
            return [{"id": c.id, "title": c.title} for c in rows]

    def get_messages(self, chat_id: str) -> list[dict]:
        """Вернуть сообщения чата по возрастанию даты."""
        with self.Session() as s:
            rows = (s.query(Message).filter_by(chat_id=chat_id)
                    .order_by(Message.created_at).all())
            return [{"role": m.role, "content": m.content,
                     "citations": json.loads(m.retrieved_ids or "[]")} for m in rows]

    def append(
        self,
        chat_id: str,
        role: str,
        content: str,
        citations: list[dict] | None = None,
        model: str | None = None,
        mode: str | None = None,
    ) -> None:
        """Добавить сообщение в чат. citations ([{chunk_id, score}]) лежат в колонке retrieved_ids."""
        with self.Session() as s:
            s.add(Message(id=str(uuid.uuid4()), chat_id=chat_id, role=role, content=content,
                          retrieved_ids=json.dumps(citations or []), model=model, mode=mode))
            s.commit()

    def rename(self, chat_id: str, title: str) -> None:
        """Переименовать чат."""
        with self.Session() as s:
            c = s.get(Chat, chat_id)
            if c:
                c.title = title
                s.commit()

    def delete_chat(self, chat_id: str) -> None:
        """Удалить чат и его сообщения."""
        with self.Session() as s:
            s.query(Message).filter_by(chat_id=chat_id).delete()
            c = s.get(Chat, chat_id)
            if c:
                s.delete(c)
            s.commit()
