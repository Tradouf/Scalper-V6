#!/bin/bash
set -e

# Couleurs pour les logs
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}═══════════════════════════════════════${NC}"
echo -e "${GREEN}  SalleDesMarches V6 - Démarrage${NC}"
echo -e "${GREEN}═══════════════════════════════════════${NC}"

# ─── 1. Vérifier LocalAI ───────────────────────────────────────
echo -e "\n${YELLOW}[1/4]${NC} Vérification LocalAI..."

LOCALAI_URL="http://localhost:8080/v1/models"
LOCALAI_RUNNING=false

# Test si LocalAI répond
if curl -s --max-time 2 "$LOCALAI_URL" > /dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} LocalAI déjà en cours"
    LOCALAI_RUNNING=true
else
    echo -e "${YELLOW}⚠${NC} LocalAI non détecté"
fi

# ─── 2. Démarrer LocalAI si nécessaire ─────────────────────────
if [ "$LOCALAI_RUNNING" = false ]; then
    echo -e "\n${YELLOW}[2/4]${NC} Démarrage de LocalAI..."
    
    # Vérifier si Docker est disponible
    if command -v docker &> /dev/null; then
        # Chercher un container LocalAI existant
        CONTAINER_ID=$(docker ps -aq -f name=localai)
        
        if [ -n "$CONTAINER_ID" ]; then
            # Container existe, le démarrer
            echo "  → Redémarrage du container existant..."
            docker start localai
        else
            # Créer un nouveau container
            echo "  → Création d'un nouveau container LocalAI..."
            docker run -d \
                --name localai \
                -p 8080:8080 \
                -v "$HOME/localai-models:/models" \
                localai/localai:latest \
                --models-path /models
        fi
        
        echo -e "${GREEN}✓${NC} LocalAI lancé via Docker"
    else
        # Docker non disponible
        echo -e "${RED}✗${NC} Docker non trouvé. Essai de lancement natif..."
        
        if command -v localai &> /dev/null; then
            # LocalAI installé en natif
            nohup localai --models-path "$HOME/localai-models" > /tmp/localai.log 2>&1 &
            echo -e "${GREEN}✓${NC} LocalAI lancé en natif (PID: $!)"
        else
            echo -e "${RED}✗${NC} LocalAI non installé"
            echo -e "${YELLOW}Options:${NC}"
            echo "  1. Installer Docker: sudo apt install docker.io"
            echo "  2. Continuer sans LLM (mode technique pur)"
            read -p "Continuer sans LLM ? (y/N) " -n 1 -r
            echo
            if [[ ! $REPLY =~ ^[Yy]$ ]]; then
                exit 1
            fi
        fi
    fi
fi

# ─── 3. Attendre que LocalAI soit prêt ─────────────────────────
if [ "$LOCALAI_RUNNING" = false ]; then
    echo -e "\n${YELLOW}[3/4]${NC} Attente du chargement des modèles..."
    
    MAX_WAIT=120  # 2 minutes max
    ELAPSED=0
    
    while [ $ELAPSED -lt $MAX_WAIT ]; do
        if curl -s --max-time 2 "$LOCALAI_URL" > /dev/null 2>&1; then
            echo -e "${GREEN}✓${NC} LocalAI prêt après ${ELAPSED}s"
            break
        fi
        sleep 5
        ELAPSED=$((ELAPSED + 5))
        echo -n "."
    done
    
    if [ $ELAPSED -ge $MAX_WAIT ]; then
        echo -e "\n${RED}✗${NC} Timeout: LocalAI n'a pas répondu après ${MAX_WAIT}s"
        echo "  Consultez les logs: docker logs localai (ou /tmp/localai.log)"
        exit 1
    fi
else
    echo -e "\n${YELLOW}[3/4]${NC} LocalAI déjà prêt, skip"
fi

# ─── 4. Démarrer le bot SDM ────────────────────────────────────
echo -e "\n${YELLOW}[4/4]${NC} Démarrage du bot SDM V6..."

# Vérifier que les fichiers existent
if [ ! -f "main_v6.py" ]; then
    echo -e "${RED}✗${NC} main_v6.py introuvable"
    exit 1
fi

# Vérifier l'environnement Python
if [ -d ".venv" ]; then
    echo "  → Activation de l'environnement virtuel"
    source .venv/bin/activate
fi

# Vérifier les dépendances critiques
python3 -c "import hyperliquid" 2>/dev/null || {
    echo -e "${RED}✗${NC} Module hyperliquid manquant"
    echo "  Installation: pip install hyperliquid-python-sdk"
    exit 1
}

echo -e "${GREEN}✓${NC} Démarrage du bot..."
echo -e "${GREEN}═══════════════════════════════════════${NC}\n"

# Lancer le bot (avec output visible)
python3 main_v6.py

# Si on arrive ici, le bot s'est arrêté
echo -e "\n${YELLOW}Bot arrêté${NC}"
