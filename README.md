# OmniPulse RPA — E-Commerce Competitive Intelligence Platform

<p align="center">
  <img src="frontend/public/brand/omnipulse-logo.svg" alt="OmniPulse RPA Logo" width="320"/>
</p>

<p align="center">
  <strong>Plataforma distribuída de RPA, crawling inteligente e auditoria competitiva em tempo real para e-commerce.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-10B981?style=flat-square&logo=python&logoColor=white" alt="Python"/>
  <img src="https://img.shields.io/badge/FastAPI-2.0-059669?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI"/>
  <img src="https://img.shields.io/badge/React-19-10B981?style=flat-square&logo=react&logoColor=white" alt="React"/>
  <img src="https://img.shields.io/badge/TypeScript-Vite-34D399?style=flat-square&logo=typescript&logoColor=black" alt="TypeScript"/>
  <img src="https://img.shields.io/badge/PostgreSQL-Prisma-047857?style=flat-square&logo=postgresql&logoColor=white" alt="PostgreSQL"/>
  <img src="https://img.shields.io/badge/RabbitMQ-AMQP-FF6600?style=flat-square&logo=rabbitmq&logoColor=white" alt="RabbitMQ"/>
  <img src="https://img.shields.io/badge/Playwright-Stealth-10B981?style=flat-square&logo=playwright&logoColor=white" alt="Playwright"/>
</p>

---

## 🎯 Sobre o Projeto

O **OmniPulse RPA** é uma solução de engenharia voltada à **Inteligência de Mercado e Monitoramento de Concorrência**. O sistema extrai, normaliza e compara catálogos de múltiplos e-commerces em larga escala, analisando preços, buy-box, rupturas de estoque, profundidade de grade e variações promocionais.

Desenvolvido para operar com alta resiliência contra bloqueios anti-bot (Cloudflare, Akamai, Datadome), ele combina motores baseados em APIs diretas (VTEX Intelligent Search, Wake, Shopify, Salesforce B2C Commerce) e navegação headless assistida via **Playwright Stealth**.

---

## ⚡ Principais Capacidades

### 1. Motores Multi-Plataforma (Crawling Polimórfico)
- **VTEX Intelligent Search**: Extração estruturada de facets, ordenação e metadados de SKU.
- **Shopify & Wake APIs**: Conexão com endpoints de catálogo com parsing de variações de cor e tamanho.
- **Salesforce Commerce Cloud (SFCC) & Zara**: Motores adaptativos com sanitização de seletores dinâmicos.
- **Evasão Anti-Bot**: Rotação de User-Agents, controle de fingerprint TLS (`curl_cffi`) e delays adaptativos.

### 2. Monitoramento Contínuo & WebSockets
- **Agendamento em Segundo Plano**: APScheduler gerenciando varreduras periódicas com tolerância a drift e reconexão de jobs.
- **Mensageria com RabbitMQ**: Fila assíncrona (`category_scans`) com política de Dead-Letter Queue (DLQ) para auditoria e retentativas automáticas.
- **Live Stream via WebSockets**: Streaming em tempo real do progresso da extração direto para o painel.

### 3. Análise Cruzada de SKU & Buy-Box
- Localização automatizada de produtos similares em múltiplos marketplaces a partir de um SKU alvo ou termo base.
- Comparação de preços à vista, parcelados, variações promocionais e cálculo de frete por CEP.

### 4. Inteligência Visual & Rastreamento de Banners
- Coleta periódica de banners promocionais dos e-commerces monitorados.
- Extração de textos, links de destino e categorização de campanhas sazonais.

---

## 🛠️ Arquitetura do Sistema

```mermaid
graph TD
    Client[Frontend React 19 / Vite] <-->|HTTP / REST & WebSockets| Backend[FastAPI Core Server]
    
    subgraph Orquestração & Armazenamento
        Backend <-->|ORM Assíncrono| Postgres[(PostgreSQL Database)]
        Backend <-->|Fila / DLQ| RabbitMQ[RabbitMQ Broker]
        Worker[Category Scan Worker] <-->|Consome Mensagens| RabbitMQ
    end

    subgraph Motores de Extração (Scraping Engines)
        Backend & Worker --> EngineVTEX[VTEX Engine]
        Backend & Worker --> EngineShopify[Shopify Engine]
        Backend & Worker --> EngineWake[Wake Engine]
        Backend & Worker --> EngineHeadless[Playwright Stealth Engine]
    end

    subgraph E-Commerces & Marketplaces
        EngineVTEX --> Store1[VTEX Stores]
        EngineShopify --> Store2[Shopify Stores]
        EngineWake --> Store3[Wake Stores]
        EngineHeadless --> Store4[Lojas com Anti-Bot]
    end
```

---

## 🚀 Como Executar Localmente

### Opção 1: Usando Docker Compose (Recomendado)

Suba toda a infraestrutura (Banco, RabbitMQ, Backend e Frontend) com um único comando:

```bash
docker compose up -d --build
```

Acessos:
- **Painel Frontend**: http://localhost:5174
- **API Swagger/Docs**: http://localhost:8080/docs
- **RabbitMQ Management**: http://localhost:15672 *(user: guest, pass: guest)*

---

### Opção 2: Execução Manual

#### Pré-requisitos
- Python 3.11+
- Node.js 20+
- PostgreSQL rodando na porta 5433 (ou ajuste no `.env`)
- RabbitMQ rodando na porta 5672

#### 1. Backend

```powershell
# Criação do ambiente virtual
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Instalação das dependências
pip install -r backend/requirements.txt
playwright install chromium

# Configuração de variáveis
cp backend/.env.example backend/.env

# Inicialização do banco (Prisma)
cd backend
python -m prisma db push
python -m prisma generate

# Iniciar servidor
python app.py
```

#### 2. Frontend

```powershell
cd frontend
npm install
npm run dev
```

Acesse em `http://localhost:5173`.

---

## 🎨 Design System — Emerald / Fintech Dark

O frontend foi desenvolvido do zero com estética **Fintech / Cyber Dark**, priorizando densidade de informação para analistas de dados:
- **Paleta de Cores**: Fundo Obsidian (`#080C14`), cartões com efeito Glassmorphism (`#0F172A`), bordas sutis e acentos em **Verde Esmeralda Neon** (`#10B981` / `#34D399`).
- **Tipografia**: `Inter` e `JetBrains Mono` para precisão em valores monetários e tabelas numéricas.
- **Acessibilidade**: Indicadores de foco visíveis, modo alto contraste e respeito a `prefers-reduced-motion`.

---

## 👨‍💻 Autor

Desenvolvido por **Arthur Correia**  

---

## 📄 Licença

Projeto desenvolvido para fins de demonstração técnica e portfólio.
