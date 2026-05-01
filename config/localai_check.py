"""
Vérifie que LocalAI tourne et liste les modèles disponibles.
Lance avec : python3 config/localai_check.py
"""
import requests, sys

try:
    r = requests.get("http://localhost:8080/v1/models", timeout=5)
    models = r.json().get("data", [])
    print(f"✅ LocalAI OK — {len(models)} modèle(s):")
    for m in models:
        print(f"   - {m['id']}")
except Exception as e:
    print(f"❌ LocalAI non disponible: {e}")
    print("   Lance: cd ~/localai && docker-compose up -d")
    sys.exit(1)
