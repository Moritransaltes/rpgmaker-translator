"""Backward-compat shim — re-exports everything from ai_client.py."""
# All code moved to ai_client.py.  This file re-exports so old imports
# like ``from .ollama_client import OllamaClient`` still work.
from .ai_client import *          # noqa: F401,F403
from .ai_client import AIClient as OllamaClient  # noqa: F401  — legacy alias
