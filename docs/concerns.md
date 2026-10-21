# CONCERNS

## Data from [2026-07-15]

This document lists known technical debt, architectural concerns, and fragile areas of the codebase.

### Technical Debt
- **Frontend Monolith**: The main `App.tsx` file is disproportionately large (nearly 200 KB) and likely contains too much business logic and UI markup. It urgently needs refactoring into smaller, modular components.
- **Scraping Engine Complexity**: The `vtex_api_scraper.py` is quite large (72 KB). While VTEX is complex, this file might be doing too much and could benefit from abstraction (e.g., separating parsing from fetching).

### Fragile Areas
- **Anti-Bot Defenses**: Integrations with Mercado Livre, Amazon, and Netshoes are inherently fragile due to evolving anti-bot measures (CAPTCHAs, rate limits). The dual strategy (`curl_cffi` -> Playwright fallback) mitigates this but requires constant monitoring.
- **Playwright Dependencies**: Running Playwright instances locally and in production is resource-intensive and prone to zombie processes if not managed perfectly via timeouts and context managers.

### Performance Bottlenecks
- **AI Models**: Running NLP models (Transformers) and OpenCV locally can spike CPU usage. If concurrent scraping requests hit the `image_ai_service.py`, it could block the event loop if not properly offloaded to process pools.
- **Memory Leaks**: Browser contexts in Playwright must be meticulously closed to prevent memory leaks during long-running tasks.
