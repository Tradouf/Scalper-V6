"""Agent codeur : délègue le code à DeepSeek via Ollama."""

from llm.ollama_client import ask_deepseek

def generate_python_function(spec: str) -> str:
    """Demande à DeepSeek d'écrire une fonction Python selon une spec texte."""
    prompt = f"""Tu es un expert Python.

Écris uniquement du code Python valide, sans explications autour.

Spécification : {spec}
"""
    return ask_deepseek(prompt)

