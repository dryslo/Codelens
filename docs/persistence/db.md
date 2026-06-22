# persistence/db.py - движок и сессии

```python
def make_session_factory(dsn: str):
    engine = create_engine(dsn, future=True)
    return sessionmaker(engine, expire_on_commit=False)
```
- `create_engine(dsn)` - точка подключения к БД. `dsn` задаёт бэкенд: `sqlite:///codelens.db` (dev) или `postgresql+psycopg://...` (large). Один код - разные БД. `future=True` - режим API 2.0.
- `sessionmaker(engine)` - фабрика сессий (вызов даёт новую сессию). Возвращается фабрика, а не сессия, чтобы репозитории открывали короткоживущую сессию на каждую операцию (`with self.Session() as s:`), не держа одну на всё приложение.
- `expire_on_commit=False` - после `commit` объекты не «протухают»: можно читать их поля вне сессии (иначе SQLAlchemy при доступе к атрибуту полез бы в закрытую сессию). Это важно, т.к. репозитории возвращают данные после выхода из `with`.

```python
def init_db(dsn: str):
    engine = create_engine(dsn, future=True)
    Base.metadata.create_all(engine)
```
- Создаёт все таблицы по моделям, если их нет. Только dev-удобство: чтобы `make index`/`make run` работали без ручного `alembic upgrade`. В проде (large) источник правды - миграции Alembic (а не `create_all`, который не умеет изменять существующие таблицы). `factory.build()` зовёт `init_db` для small; для large корректнее прогнать миграции и не полагаться на это.

Почему движок создаётся дважды (в обеих функциях): это разные точки входа; в небольшом проекте накладные расходы незаметны. При необходимости можно вынести один общий engine.
