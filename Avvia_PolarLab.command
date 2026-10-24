#!/bin/bash

# Directory del progetto
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "==========================================================="
echo "❤️  Avvio di PolarLab — ECG in tempo reale dalla Polar H10..."
echo "==========================================================="

# venv.nosync: escluso da iCloud e non nascosto (vedi README)
if [ ! -d "venv.nosync" ]; then
    echo "⚠️ Ambiente virtuale non trovato! Creazione in corso..."
    python3.13 -m venv venv.nosync || python3 -m venv venv.nosync
    source venv.nosync/bin/activate
    pip install -r requirements.txt
else
    source venv.nosync/bin/activate
fi

# Argomenti opzionali: ./Avvia_PolarLab.command --sim
python -m polarlab "$@"
