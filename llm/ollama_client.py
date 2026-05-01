"""Client LLM — migré de Ollama vers LocalAI (compatible OpenAI)."""
import requests

LOCALAI_BASE_URL = "http://localhost:8080"

def call_ollama(model: str, messages: list[dict], stream: bool = False) -> str:
    """Appelle LocalAI (API compatible OpenAI) et retourne le texte généré."""
    resp = requests.post(
        f"{LOCALAI_BASE_URL}/v1/chat/completions",
        json={"model": model, "messages": messages, "stream": stream, "max_tokens": 2000},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]

def ask_deepseek(prompt: str) -> str:
    """Generation de code via deepseek-coder sur LocalAI."""
    return call_ollama(
        "deepseek-coder-v2-lite-instruct",
        [{"role": "user", "content": prompt}],
    )
