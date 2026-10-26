#!/usr/bin/env bash
# Installs the extra move models on a Debian-based machine (Raspberry Pi OS, Ubuntu):
# builds Leela Chess Zero (lc0) from source with the CPU (OpenBLAS) backend, and downloads
# the nine Maia nets (maia-1100 ... maia-1900) into models/maia.
#
#   sudo bash scripts/install_models.sh
#
# Building lc0 takes a while on a Pi (several minutes). Safe to re-run: steps already done
# are skipped. Maia: https://github.com/CSSLab/maia-chess  lc0: https://github.com/LeelaChessZero/lc0
set -euo pipefail

LC0_BRANCH="${LC0_BRANCH:-release/0.32}"
MAIA_URL="${MAIA_URL:-https://github.com/CSSLab/maia-chess/raw/master/maia_weights}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAIA_DIR="${MAIA_DIR:-$APP_DIR/models/maia}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this with sudo: sudo bash scripts/install_models.sh" >&2
  exit 1
fi

if ! command -v lc0 >/dev/null 2>&1; then
  echo "==> Installing build tools"
  apt-get update
  apt-get install -y --no-install-recommends git ca-certificates curl build-essential \
    meson ninja-build python3 libopenblas-dev zlib1g-dev
  echo "==> Building lc0 ($LC0_BRANCH) into /usr/local/bin"
  src="$(mktemp -d)"
  trap 'rm -rf "$src"' EXIT
  git clone --depth 1 -b "$LC0_BRANCH" https://github.com/LeelaChessZero/lc0.git "$src/lc0"
  (cd "$src/lc0" && INSTALL_PREFIX=/usr/local ./build.sh -Dgtest=false)
else
  echo "==> lc0 already installed: $(command -v lc0)"
fi

echo "==> Downloading Maia nets into $MAIA_DIR"
mkdir -p "$MAIA_DIR"
for elo in 1100 1200 1300 1400 1500 1600 1700 1800 1900; do
  file="$MAIA_DIR/maia-$elo.pb.gz"
  if gzip -t "$file" 2>/dev/null; then
    echo "    maia-$elo already there"
    continue
  fi
  curl -fL --retry 3 -o "$file" "$MAIA_URL/maia-$elo.pb.gz"
  gzip -t "$file" || { echo "maia-$elo.pb.gz didn't download correctly" >&2; rm -f "$file"; exit 1; }
  echo "    maia-$elo done"
done
chmod -R a+rX "$MAIA_DIR"

echo "==> Checking lc0 with Maia (one node)"
printf 'uci\nisready\nposition startpos\ngo nodes 1\n' | timeout 60 \
  lc0 --weights="$MAIA_DIR/maia-1500.pb.gz" 2>&1 | grep -E "^(id name|bestmove)" || {
  echo "lc0 did not answer; check the output above" >&2; exit 1; }

if systemctl list-unit-files bestmovechess.service >/dev/null 2>&1 && systemctl is-enabled bestmovechess >/dev/null 2>&1; then
  echo "==> Restarting bestmovechess"
  systemctl restart bestmovechess
fi
echo "Done. Check: curl http://127.0.0.1:8000/models"
