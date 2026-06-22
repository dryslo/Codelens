# CodeLens - поиск по кодовой базе (RAG)

Состав команды: Светличный Сергей, Лубкин Егор, Щеголяев Григорий, Калачёв Александр

Семантический поиск по Python-коду: парсинг через `ast` → обогащение в NL-текст → эмбеддинги →
векторный поиск с опциональными каналами (BM25/RRF, HyDE, multi-query, MMR, cross-encoder).
Сверху - чат с history-aware RAG и админка кодовой базы.

Один код и одни образы на два профиля: `small` (одна нода, без k8s) и `large` (кластер на
k8s/Helm/Argo CD). Профили отличаются только реализациями за интерфейсами (Chroma↔Qdrant,
SQLite↔Postgres, in-process↔Redis) и масштабом, не логикой.

## Документация
- [`docs/`](docs/README.md) - архитектура, потоки данных, разбор по файлам.
- [`docs/architecture.md`](docs/architecture.md) - порты, профили, топология деплоя.
- [`deploy/gitops/README.md`](deploy/gitops/README.md) - деплой large через CI + Argo CD (GitOps).
- [`deploy/k3s-setup.md`](deploy/k3s-setup.md) - развёртывание k3s на VPS; [`deploy/minikube.md`](deploy/minikube.md) - локальная валидация чарта.

## Данные кейса (dataset_case3)
Корпус `gymhero` (`data/codebase/`, relpath вида `gymhero/...`) и эвал-файлы
(`data/eval_questions.json`, `data/sample_queries.txt`, `data/score.py` - официальный скорер)
лежат в репозитории: после clone готовы к `make index`/`make eval`, распаковывать ничего не нужно.

Пересобрать из исходного архива (если потребуется), корень репозитория - в `data/codebase`:
```bash
unzip dataset_case3_v1_0_fix.zip -d /tmp/ds
unzip /tmp/ds/codebase_python.zip -d /tmp/cb
mv /tmp/cb/gymhero data/codebase            # data/codebase/gymhero/security.py должен существовать
cp /tmp/ds/{eval_questions.json,sample_queries.txt,score.py} data/
```

## Быстрый старт (профиль small, без Docker)
```bash
# Debian/Ubuntu/WSL: если venv не создаётся - sudo apt install -y python3-venv python3-full
make install            # создаёт .venv и ставит зависимости
# (вручную: python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]")

make index              # индексация data/codebase
make run                # UI на http://localhost:8501
```
По умолчанию: Chroma (embedded, `.chroma/`) + SQLite (`codelens.db`), cross-encoder выключен.
LLM-провайдер настраивается в `llm.providers` (`config/config.yaml`); из коробки прописан Groq -
ключ берётся из `GROQ_API_KEY`. Без ключа поиск работает, LLM-функции (чат, HyDE, multi-query) - нет.

## Оценка качества
```bash
make eval                                   # Precision@5/Hit@5, пишет results.json
python evaluate.py --assert-min 0.60        # тот же прогон с CI-гейтом
python data/score.py --predictions results.json --questions data/eval_questions.json
```
Метрика считается по формуле официального скорера (`data/score.py`).

## Профиль large (Docker / k8s)
```bash
docker compose -f deploy/docker-compose.yml up --build     # все сервисы в одной сети
```
k8s: Helm-чарт `deploy/helm/codelens`, деплой через Argo CD - порядок в
[`deploy/gitops/README.md`](deploy/gitops/README.md); локальная проверка чарта - [`deploy/minikube.md`](deploy/minikube.md).

## Makefile
```bash
make install        # установка (dev)
make index          # индексация data/codebase
make run            # UI
make eval           # Precision@5/Hit@5
make test lint fmt typecheck
make migrate        # alembic upgrade head
make migration m="init"   # новая миграция
make up / make down       # docker-compose (профиль small)
make mk-up / make mk-validate   # minikube (профиль large)
```
Alembic уже настроен (`migrations/`, `alembic.ini`, `env.py` привязан к моделям и `DATABASE_DSN`).
`alembic init` не нужен - сразу `make migration m="init"` и `make migrate`.

## Состав
- Парсинг Python через `ast` за интерфейсом `Parser`; языки добавляются регистрацией парсера по расширению.
- Индексация в Chroma (small) / Qdrant (large) через единый `VectorStore`; инкрементально по хэшу файла.
- Эмбеддер `intfloat/multilingual-e5-large` (префиксы query/passage), кэш моделей в `cache/models/`.
- Каналы поиска: dense, BM25+RRF, HyDE, multi-query, MMR, cross-encoder `BAAI/bge-reranker-v2-m3` -
  каждый управляется флагом (`off`/`ui`/`fast`/`thinking`) в `config/config.yaml`.
- Чат (history-aware) на SQLAlchemy + репозитории; кэш поиска и ответов с epoch-инвалидацией.
- Streamlit: Поиск / Чат (всем); Метрики / Админка (только админам). Авторизация: JWT + refresh, argon2, OIDC-ready.
- FastAPI backend + inference-сервисы; `HttpBackend` для frontend.
