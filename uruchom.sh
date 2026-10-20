#!/usr/bin/env bash
# ==============================================================================
# Facebook OSINT & Scraper — Auto-Launcher & Installer dla Linux / WSL
# Podwójny klik lub uruchomienie w terminalu: sam instaluje i odpala program!
# ==============================================================================

set -e
cd "$(dirname "$0")"

echo "===================================================================="
echo "          Facebook OSINT Scraper — Automatyczny Starter             "
echo "===================================================================="

# 1. Wybór interpretera Pythona (preferowany python3.13 z uwagi na mini-racer)
PYTHON_CMD=""
for cmd in python3.13 python3 python; do
    if command -v $cmd &>/dev/null; then
        PYTHON_CMD=$cmd
        break
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    echo "❌ Błąd: Nie znaleziono zainstalowanego Pythona w systemie!"
    echo "Zainstaluj Pythona (np. sudo apt install python3 python3-venv)."
    exit 1
fi

# 2. Sprawdzenie środowiska wirtualnego
if [ ! -d ".venv" ]; then
    echo "⚙️ Tworzenie środowiska wirtualnego (.venv)..."
    $PYTHON_CMD -m venv .venv
    echo "📦 Instalowanie wymaganych bibliotek (to potrwa chwilę tylko za pierwszym razem)..."
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -e .
    .venv/bin/pip install -r requirements.txt
    echo "✅ Instalacja zakończona sukcesem!"
fi

# 3. Uruchomienie aplikacji
echo "🚀 Uruchamianie aplikacji..."
.venv/bin/python scrape_all.py "$@"
