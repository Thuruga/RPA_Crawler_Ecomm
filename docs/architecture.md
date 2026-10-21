# ARCHITECTURE

## Data from [2026-07-15]

This document outlines the architectural patterns and data flow of the project.

### High-Level Architecture
The project follows a standard Client-Server decoupled architecture:
1. **Frontend**: A React SPA built with Vite that communicates via REST HTTP.
2. **Backend**: A FastAPI server that orchestrates data scraping, AI-based relevance scoring, and caching.

### Backend Architecture
The backend is structured into distinct layers to separate concerns:
- **API Layer (`backend/api/`)**: Defines FastAPI routers. Responsible for request validation (via Pydantic), authentication, and HTTP responses.
- **Service Layer (`backend/services/`)**: Contains the core business logic.
  - **Orchestration**: `orchestrator.py` manages cross-marketplace searches.
  - **Scraping Engines**: `engines/` and specific files like `vtex_api_scraper.py` implement platform-specific data extraction.
  - **AI/NLP Validation**: `nlp_service.py` and `image_ai_service.py` apply relevance gates to scraped results.
  - **Monitoring**: Scheduled jobs (`price_monitor_service.py`, `category_monitor_service.py`) run asynchronously.
- **Infrastructure Layer (`backend/infrastructure/`)**: Manages low-level HTTP clients (`aiohttp`, `curl_cffi`) and retry policies.
- **Core Configuration (`config.py`)**: Centralized settings via `pydantic-settings`. No magic numbers in services.

### Data Flow Example (Cross-Marketplace Search)
1. **Request**: Frontend sends a search query (`/api/search/cross`).
2. **Orchestration**: `orchestrator.py` receives the query and dispatches parallel tasks to multiple scraping engines.
3. **Scraping**: Engines hit VTEX, Shopify, ML, etc. If HTTP blocked, they fallback to Playwright.
4. **Scoring & Relevance (Gates)**:
   - Results are passed to `relevance_gates.py`.
   - NLP models (RapidFuzz) score title similarities.
   - If enabled, Vision models (CLIP via `image_ai_service`) resolve text ambiguities.
5. **Aggregation**: Results are merged, sorted by relevance score, and returned to the client.

### Cross-cutting Concerns
- **Caching**: Category discovery results are cached in Redis to prevent rate-limiting.
- **Concurrency**: `asyncio` is used heavily to allow parallel scraping of multiple platforms.
