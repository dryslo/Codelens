#!/usr/bin/env bash
# Генератор конфигов AmneziaWG для подключения KZ-узла (node-eu) к кластеру через границу.
# Топология "звезда": node-eu пирится с каждым из 5 РФ-узлов точка-точка. РФ↔РФ идёт по приватной
# сети провайдера и AmneziaWG НЕ использует - awg нужен только на канале KZ↔РФ (там DPI).
#
# Запускается РАЗОВО на любой машине с amneziawg-tools (команда awg; ключи совместимы с wg).
# Кладёт по awg0.conf на каждый из 6 узлов в ./out/<node>/awg0.conf. Дальше каждый файл копируется
# на свой узел в /etc/amnezia/amneziawg/awg0.conf и поднимается `awg-quick up awg0`.
# Подробности и порядок - ./README.md.
#
# AmneziaWG = WireGuard с обфускацией против DPI. Параметры (Jc..H4) ОБЯЗАНЫ совпадать на всех пирах.
set -euo pipefail

# --- KZ-узел: имя | публичный IP | tunnel-IP (его же k3s --node-ip) -------------
EU_NAME="node-eu"
EU_PUBLIC="178.236.17.61"
EU_TUNNEL="10.10.0.6"

# --- РФ-узлы: имя | публичный IP | ПРИВАТНЫЙ IP (k3s --node-ip) | awg-адрес ------
#   ПРИВАТНЫЙ IP - адрес узла в приватной сети провайдера (подсеть 10.16.0.0/24, интерфейс ens9).
#   Это node-ip, к которому node-eu обращается через туннель: он попадает в AllowedIPs со стороны KZ.
RF_NODES=(
  "node-s1    159.194.229.34  10.16.0.2  10.10.0.1"
  "node-s2    159.194.235.78  10.16.0.3  10.10.0.2"
  "node-s3    31.207.76.197   10.16.0.4  10.10.0.3"
  "node-heavy 85.198.66.196   10.16.0.5  10.10.0.4"
  "node-dev   85.198.68.29    10.16.0.1  10.10.0.5"
)
PORT="${PORT:-51820}"
AWG="${AWG:-awg}"            # или wg - формат ключей одинаковый

# --- Параметры обфускации (одинаковы на ВСЕХ узлах) ----------------------------
read -r -d '' OBFS <<'EOF' || true
Jc = 7
Jmin = 50
Jmax = 1000
S1 = 86
S2 = 574
H1 = 1278431955
H2 = 1632190015
H3 = 1956219007
H4 = 2079117603
EOF

OUT="${OUT:-./out}"
rm -rf "$OUT"; mkdir -p "$OUT"

# ключи: node-eu + каждый РФ-узел
declare -A PRIV PUB
gen_keys() { local n="$1"; local p; p="$("$AWG" genkey)"; PRIV[$n]="$p"; PUB[$n]="$(printf '%s' "$p" | "$AWG" pubkey)"; }
gen_keys "$EU_NAME"
for entry in "${RF_NODES[@]}"; do read -r name _ _ _ <<<"$entry"; gen_keys "$name"; done

# --- конфиг node-eu: [Peer] на каждый РФ-узел (AllowedIPs = приватный node-ip РФ) ---
eudir="$OUT/$EU_NAME"; mkdir -p "$eudir"
{
  echo "[Interface]"
  echo "Address = $EU_TUNNEL/32"
  echo "ListenPort = $PORT"
  echo "PrivateKey = ${PRIV[$EU_NAME]}"
  echo "$OBFS"
  echo
  for entry in "${RF_NODES[@]}"; do
    read -r name pubip privip _awg <<<"$entry"
    echo "[Peer]  # $name"
    echo "PublicKey = ${PUB[$name]}"
    echo "Endpoint = $pubip:$PORT"
    echo "AllowedIPs = $privip/32"        # node-eu маршрутизирует приватный node-ip РФ через туннель
    echo "PersistentKeepalive = 25"
    echo
  done
} > "$eudir/awg0.conf"
echo "  $EU_NAME ($EU_TUNNEL) -> $eudir/awg0.conf"

# --- конфиг каждого РФ-узла: единственный [Peer] - node-eu (AllowedIPs = tunnel-IP KZ) ---
for entry in "${RF_NODES[@]}"; do
  read -r name _pubip _privip awgaddr <<<"$entry"
  dir="$OUT/$name"; mkdir -p "$dir"
  {
    echo "[Interface]"
    echo "Address = $awgaddr/32"
    echo "ListenPort = $PORT"
    echo "PrivateKey = ${PRIV[$name]}"
    echo "$OBFS"
    echo
    echo "[Peer]  # $EU_NAME"
    echo "PublicKey = ${PUB[$EU_NAME]}"
    echo "Endpoint = $EU_PUBLIC:$PORT"
    echo "AllowedIPs = $EU_TUNNEL/32"      # РФ-узел маршрутизирует tunnel-IP KZ через туннель
    echo "PersistentKeepalive = 25"
    echo
  } > "$dir/awg0.conf"
  echo "  $name -> $dir/awg0.conf"
done

echo
echo "Готово. На каждом узле:"
echo "  scp out/<node>/awg0.conf <node>:/etc/amnezia/amneziawg/awg0.conf"
echo "  ssh <node> 'awg-quick up awg0 && systemctl enable awg-quick@awg0'"
echo "Проверка с node-eu: ping <приватный IP любого РФ-узла> должен идти через awg0."
