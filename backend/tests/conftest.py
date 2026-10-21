"""Conftest global do pytest para testes herméticos locais."""
import os

os.environ["OBS_ENABLED"] = "false"
os.environ["AUTH_ENABLED"] = "false"
