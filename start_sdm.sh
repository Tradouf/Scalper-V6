#!/bin/bash

# ── 1. Démarrer LocalAI si pas déjà en cours ──
if ! curl -s http://localhost:8080/v1/models > /dev/null 2>&1; then
    echo "Démarrage LocalAI..."
    docker start local-ai
    
    # Attendre que LocalAI réponde vraiment
    echo -n "Chargement des modèles"
    READY=false
    for i in {1..60}; do  # 2 minutes max
        sleep 2
        
        # Tester si l'API répond ET retourne des modèles
        if curl -s http://localhost:8080/v1/models 2>/dev/null | grep -q "qwen"; then
            echo " ✓ Prêt après $((i*2))s"
            READY=true
            break
        fi
        echo -n "."
    done
    
    if [ "$READY" = false ]; then
        echo ""
        echo "⚠ LocalAI n'a pas répondu après 2 minutes"
        echo "Vérifiez: docker logs local-ai"
        exit 1
    fi
else
    echo "✓ LocalAI déjà en cours"
fi

# ── 2. Vérification finale ──
echo "Vérification des modèles disponibles..."
curl -s http://localhost:8080/v1/models | grep -o '"id":"[^"]*"' | head -5

# ── 3. Lancer le bot ──
echo "Démarrage du bot..."
cd ~/SalleDesMarches_fixed

if [ -d ".venv" ]; then
    source .venv/bin/activate
fi

if [ -f ".env" ]; then
    set -a
    source .env
    set +a
fi

python3 main_v6.py
