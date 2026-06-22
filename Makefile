# CodeLens - Makefile
# Интерпретатор определяется автоматически. Переопределение: make <цель> PY=/путь/к/python
# Инструменты вызываются через "$(PY) -m ...", поэтому раскладка venv (bin/ или Scripts/) не важна.
VENV ?= $(HOME)/.venvs/codelens

PY ?= $(shell \
	if [ -n "$$VIRTUAL_ENV" ] && [ -x "$$VIRTUAL_ENV/bin/python" ]; then echo "$$VIRTUAL_ENV/bin/python"; \
	elif [ -x "$(VENV)/bin/python" ]; then echo "$(VENV)/bin/python"; \
	elif [ -x .venv/bin/python ]; then echo .venv/bin/python; \
	elif [ -x venv/bin/python ]; then echo venv/bin/python; \
	elif command -v python3 >/dev/null 2>&1; then echo python3; \
	else echo python; fi)

DSN ?= sqlite:///codelens.db

.DEFAULT_GOAL := help
.PHONY: help venv install install-scale run index eval test lint fmt typecheck migrate migration up up-panels build down inference clean \
        mk-start mk-images mk-validate mk-up mk-infra mk-status mk-down

# --- Локальная валидация деплоя на minikube ---
CHART     ?= deploy/helm/codelens
MK_VALUES ?= $(CHART)/values-local.yaml
MK_SVCS   ?= frontend backend inference llm worker
# minikube с docker-драйвером берёт память НА КАЖДЫЙ узел: NODES*MEM не должно превышать ОЗУ.
# Дефолт 3*2048=6 ГБ - под слабую машину; переопределить: make mk-start MK_MEM=3072 MK_NODES=1
MK_NODES  ?= 3
MK_CPUS   ?= 2
MK_MEM    ?= 2048
# Доп. флаги helm для mk-up: на машинах с малой ОЗУ отключить тяжёлый embedder, для HA-теста CNPG -
# поднять реплики Postgres. Пример: make mk-up MK_SET="--set embedder.enabled=false"
MK_SET    ?=
# Манифест оператора CloudNativePG (нужен для postgres: kind Cluster). Версию проверить на
# github.com/cloudnative-pg/cloudnative-pg/releases и переопределить: make mk-start CNPG=...
CNPG ?= https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.24/releases/cnpg-1.24.1.yaml

help:  ## показать список команд
	@echo "CodeLens - команды. Интерпретатор: $(PY)"
	@echo "(переопределение: make <цель> PY=/путь/к/python)"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
	    awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

venv:  ## создать Linux-venv в $(VENV)
	@case "$(VENV)" in /mnt/*) echo "VENV=$(VENV) на /mnt/* (NTFS): в WSL медленно. Указать VENV=\$$HOME/.venvs/codelens.";; esac
	python3 -m venv "$(VENV)"
	"$(VENV)/bin/python" -m pip install -U pip
	@echo "Готово. PY=$(VENV)/bin/python используется автоматически"
	@echo "Далее: make install"

install:  ## установить всё для локальной разработки (run/index/eval/test) в $(VENV)
	$(PY) -m pip install -e ".[all,dev]"

install-scale:  ## алиас install (драйверы large уже входят в группу all)
	$(PY) -m pip install -e ".[all,dev]"

run:  ## запустить UI (Streamlit) на http://localhost:8501
	PYTHONUNBUFFERED=1 $(PY) -m streamlit run app.py --server.headless true

index:  ## проиндексировать data/codebase
	$(PY) index.py data/codebase

eval:  ## Precision@5 / Hit@5 + results.json
	$(PY) evaluate.py --assert-min 0.60

test:  ## прогнать тесты
	$(PY) -m pytest -q

lint:  ## линт (ruff)
	$(PY) -m ruff check .

fmt:  ## форматирование (ruff)
	$(PY) -m ruff format .

typecheck:  ## статическая проверка типов (mypy, advisory)
	$(PY) -m mypy src/

migrate:  ## применить миграции (DSN=...)
	DATABASE_DSN=$(DSN) $(PY) -m alembic upgrade head

migration:  ## новая миграция: make migration m="что изменил"
	DATABASE_DSN=$(DSN) $(PY) -m alembic revision --autogenerate -m "$(m)"

up:  ## docker-compose (профиль small); образы переиспользуются, отсутствующие соберутся
	docker compose -f deploy/docker-compose.yml up

up-panels:  ## то же + панели за forward-auth (nginx+grafana+prometheus) на http://localhost
	docker compose -f deploy/docker-compose.yml --profile panels up

build:  ## собрать образы: все или только нужные - make build S="backend frontend"
	docker compose -f deploy/docker-compose.yml build $(S)

down:  ## остановить compose
	docker compose -f deploy/docker-compose.yml down

inference:  ## локально поднять inference-сервис
	$(PY) -m uvicorn services.inference_app:app --reload --port 8000

clean:  ## очистить локальные артефакты
	rm -rf .chroma codelens.db .registry.db results.json .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

# --- minikube (runbook: deploy/minikube.md) ---
mk-start:  ## minikube: узлы + metrics-server + оператор CNPG (память НА узел: MK_NODES*MK_MEM <= ОЗУ)
	minikube start --nodes $(MK_NODES) --cpus $(MK_CPUS) --memory $(MK_MEM) --driver=docker
	minikube addons enable metrics-server
	kubectl apply --server-side -f $(CNPG)
	kubectl -n cnpg-system rollout status deploy/cnpg-controller-manager --timeout=180s

mk-images:  ## собрать образы и загрузить в minikube как codelens/<svc>:local
	@for s in $(MK_SVCS); do \
		echo "==> build codelens/$$s:local"; \
		docker build -f deploy/Dockerfile.$$s -t codelens/$$s:local . || exit 1; \
		minikube image load codelens/$$s:local || exit 1; \
	done

mk-validate:  ## статика чарта: helm lint + template + kubeconform (без кластера)
	helm lint $(CHART) -f $(MK_VALUES)
	helm template $(CHART) -f $(MK_VALUES) | kubeconform -strict -summary -ignore-missing-schemas \
		-schema-location default \
		-schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'

mk-up:  ## установить/обновить чарт (overlay values-local; доп. флаги через MK_SET)
	helm upgrade --install codelens $(CHART) -f $(MK_VALUES) $(MK_SET)

mk-infra:  ## только инфра: кластер Qdrant + CNPG + Redis, без сборки образов (для слабой ОЗУ)
	helm upgrade --install codelens $(CHART) -f $(MK_VALUES) --no-hooks \
		--set frontend.enabled=false --set backend.enabled=false --set worker.enabled=false \
		--set llm.enabled=false --set embedder.enabled=false $(MK_SET)

mk-status:  ## быстрые проверки: узлы, поды, кластер Qdrant, статус CNPG
	kubectl get nodes -o wide
	kubectl get pods -o wide
	@echo "--- Qdrant cluster (peers) ---"; \
	kubectl port-forward svc/codelens-qdrant 6333:6333 >/dev/null 2>&1 & PF=$$!; sleep 3; \
	curl -s http://localhost:6333/cluster || echo "(qdrant недоступен)"; \
	kill $$PF 2>/dev/null
	@echo; echo "--- CNPG ---"; kubectl get cluster.postgresql.cnpg.io -o wide 2>/dev/null || true

mk-down:  ## снести релиз (PVC qdrant/pg остаются - удалить вручную при необходимости)
	helm uninstall codelens || true