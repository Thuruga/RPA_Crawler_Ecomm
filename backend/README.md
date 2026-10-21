# OmniPulse RPA — Backend API

Backend em **FastAPI (Python 3.11+)** com arquitetura assíncrona, ORM Prisma (PostgreSQL), RabbitMQ e motores de raspagem polimórficos.

## Estrutura

- `api/`: Rotas REST, endpoints de download, auditorias e WebSockets de stream em tempo real.
- `core/`: Orquestração de jobs, conexão ao banco (Prisma), modelos de domínio e telemetria.
- `services/`: Motores de crawling (VTEX, Wake, Shopify, SFCC, Zara, Linx), worker de categorias e cálculo de frete.
- `data/brands.json`: Catálogo de marcas e e-commerces monitorados.

## Setup Local

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium

cp .env.example .env
python -m prisma db push
python -m prisma generate

python app.py
```
