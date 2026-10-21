"""
routes_auth.py — mantido por compatibilidade mas sem endpoints ativos.

A autenticação JWT foi substituída por API Key de ambiente (X-API-Key header).
Nenhum endpoint de login é necessário — o frontend acessa diretamente o dashboard.
"""

from fastapi import APIRouter

router = APIRouter(tags=["Auth"])

# Sem endpoints — autenticação via X-API-Key header gerenciada em api/auth.py
