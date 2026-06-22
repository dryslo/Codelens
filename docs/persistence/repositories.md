# persistence - history_repo.py и registry_repo.py

Репозитории реализуют доменные порты (`History`, `IndexRegistry`) поверх SQLAlchemy. Верхний код зависит от интерфейса, не от ORM.

## history_repo.py - `SqlHistory`
```python
def __init__(self, session_factory):
    self.Session = session_factory
```
- Принимает фабрику сессий (из `make_session_factory`). На каждую операцию открывает свою короткую сессию.

```python
def create_chat(self, user_id, title):
    cid = str(uuid.uuid4())
    with self.Session() as s:
        s.add(Chat(id=cid, user_id=user_id, title=title))
        s.commit()
    return cid
```
- Генерация UUID для чата. `with self.Session() as s` - сессия закрывается автоматически (даже при исключении). `s.add(...)` ставит объект в очередь, `s.commit()` фиксирует. Возвращается id.

```python
def list_chats(self, user_id):
    with self.Session() as s:
        rows = (s.query(Chat).filter_by(user_id=user_id)
                .order_by(Chat.created_at.desc()).all())
        return [{"id": c.id, "title": c.title} for c in rows]
```
- Запрос чатов пользователя, новые сверху (`desc()`). Возвращаются простые dict, а не ORM-объекты - чтобы верхние слои не зависели от ORM и данные были живы вне сессии.

```python
def get_messages(self, chat_id):
    with self.Session() as s:
        rows = (s.query(Message).filter_by(chat_id=chat_id)
                .order_by(Message.created_at).all())
        return [{"role": m.role, "content": m.content,
                 "citations": json.loads(m.retrieved_ids or "[]")} for m in rows]
```
- Сообщения чата в хронологическом порядке. Цитаты хранятся строкой в колонке `retrieved_ids` → `json.loads` обратно в список; `or "[]"` - при `None` парсится пустой список.

```python
def append(self, chat_id, role, content, citations=None, model=None, mode=None):
    with self.Session() as s:
        s.add(Message(id=str(uuid.uuid4()), chat_id=chat_id, role=role, content=content,
                      retrieved_ids=json.dumps(citations or []), model=model, mode=mode))
        s.commit()
```
- Добавляет сообщение. `json.dumps(citations or [])` - список цитат → колонка `retrieved_ids`. `model`/`mode` пишутся для аналитики.

## registry_repo.py - `SqlRegistry`
Реализует `IndexRegistry` для инкрементальной индексации.
```python
def get_hash(self, source, file):
    with self.Session() as s:
        row = s.get(IndexedFile, (source, file))
        return row.hash if row else None
```
- `s.get(Model, pk)` - быстрый поиск по первичному ключу. Ключ составной → кортеж `(source, file)`. Нет записи → `None` (значит файл новый).

```python
def set_hash(self, source, file, h):
    with self.Session() as s:
        row = s.get(IndexedFile, (source, file))
        if row:
            row.hash = h
        else:
            s.add(IndexedFile(source=source, file=file, hash=h))
        s.commit()
```
- Upsert: если запись есть - обновляется хэш (ORM отследит изменение и обновит при commit), иначе вставляется новая.

```python
def files(self, source):
    with self.Session() as s:
        return [r.file for r in s.query(IndexedFile).filter_by(source=source).all()]
```
- Все файлы источника - пайплайн вычитает из них «текущие», чтобы найти удалённые.

```python
def remove(self, source, file=None):
    with self.Session() as s:
        q = s.query(IndexedFile).filter_by(source=source)
        if file:
            q = q.filter_by(file=file)
        q.delete()
        s.commit()
```
- Удаление записей реестра: всего источника (`file=None`) или одного файла. `q.delete()` - bulk-delete по фильтру (без загрузки объектов).

Почему репозитории отдают dict/строки, а не ORM-объекты: изоляция - `LocalBackend` и UI не знают про SQLAlchemy; хранилище можно заменить, не трогая верхние слои.
