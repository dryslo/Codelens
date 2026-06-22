#!/usr/bin/env bash
# ============================================================================
#  Генерирует зашифрованный SealedSecret для CodeLens и кладёт его в git.
#  Зашифрованный файл коммитить безопасно: расшифровать может только контроллер
#  sealed-secrets в кластере (приватный ключ там, не в git).
#
#  Требуется:
#    - kubectl с доступом в кластер
#    - kubeseal (CLI sealed-secrets)
#    - установленный в кластере контроллер sealed-secrets (см. README.md рядом)
#
#  Реальные значения берутся из окружения. Их можно держать в gitignored-файле
#  deploy/gitops/sealed/secrets.<env>.env (он подхватывается автоматически):
#      JWT_SECRET=...
#      GROQ_API_KEY=...
#      ADMIN_PASSWORD=...
#      DATABASE_DSN=postgresql+psycopg://codelens:PASS@codelens-pg-rw:5432/codelens
#      GEMINI_API_KEY=...   HF_TOKEN=...   ADMIN_LOGIN=admin
#
#  Использование:
#      deploy/gitops/sealed/seal.sh staging
#      deploy/gitops/sealed/seal.sh prod
# ============================================================================
set -euo pipefail

ENV="${1:?usage: seal.sh <staging|prod>}"
case "$ENV" in staging|prod) ;; *) echo "env должен быть staging|prod" >&2; exit 1 ;; esac

NS="codelens-$ENV"
SECRET_NAME="codelens-secrets"
DIR="$(cd "$(dirname "$0")" && pwd)"
OUT="$DIR/$ENV/$SECRET_NAME.yaml"      # эту папку синкает отдельный Argo Application
mkdir -p "$DIR/$ENV"

# Контроллер: по умолчанию ищется как service sealed-secrets-controller в kube-system.
# Переопределяется при иной установке: CONTROLLER_NS=... CONTROLLER_NAME=...
CONTROLLER_NS="${CONTROLLER_NS:-kube-system}"
CONTROLLER_NAME="${CONTROLLER_NAME:-sealed-secrets-controller}"

# Подхватить значения из gitignored-файла, если есть.
ENV_FILE="$DIR/secrets.$ENV.env"
if [ -f "$ENV_FILE" ]; then
  echo "→ значения из $ENV_FILE"
  set -a; . "$ENV_FILE"; set +a
fi

: "${JWT_SECRET:?нужен JWT_SECRET (>=32 байт; openssl rand -base64 32)}"
: "${ADMIN_PASSWORD:?нужен ADMIN_PASSWORD}"

# DATABASE_DSN: если не задан, собирается на сервис -pg-rw (как делал чарт при secrets.create=true).
: "${PG_USER:=codelens}"; : "${PG_DB:=codelens}"
if [ -z "${DATABASE_DSN:-}" ]; then
  : "${PG_PASSWORD:?нужен DATABASE_DSN или PG_PASSWORD для сборки DSN}"
  DATABASE_DSN="postgresql+psycopg://${PG_USER}:${PG_PASSWORD}@codelens-pg-rw:5432/${PG_DB}"
fi

# 1) Собирается обычный Secret локально (--dry-run: в кластер ничего не уходит, только YAML в stdout).
#    Имена ключей обязаны совпадать с ${VAR} в config.yaml (см. templates/secret.yaml).
# 2) kubeseal шифрует его публичным ключом контроллера -> SealedSecret. Скоуп strict:
#    расшифруется только в namespace "$NS" под именем "$SECRET_NAME".
kubectl create secret generic "$SECRET_NAME" -n "$NS" \
  --from-literal=DATABASE_DSN="$DATABASE_DSN" \
  --from-literal=JWT_SECRET="$JWT_SECRET" \
  --from-literal=GROQ_API_KEY="${GROQ_API_KEY:-}" \
  --from-literal=GEMINI_API_KEY="${GEMINI_API_KEY:-}" \
  --from-literal=ADMIN_LOGIN="${ADMIN_LOGIN:-admin}" \
  --from-literal=ADMIN_PASSWORD="$ADMIN_PASSWORD" \
  --from-literal=HF_TOKEN="${HF_TOKEN:-}" \
  --dry-run=client -o yaml \
  | kubeseal --format yaml \
      --controller-namespace "$CONTROLLER_NS" \
      --controller-name "$CONTROLLER_NAME" \
  > "$OUT"

echo "→ записан зашифрованный $OUT"
echo "  git add '$OUT' && git commit && git push  →  Argo CD применит, контроллер расшифрует в Secret/$SECRET_NAME"
