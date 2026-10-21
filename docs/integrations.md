# INTEGRATIONS

## Data from [2026-07-15]

This document catalogs all external integrations, APIs, and third-party services utilized by the Intelligence Scraper project.

### Marketplaces & E-commerce APIs
The core functionality relies on integrating with various e-commerce platforms to extract product data, categories, and prices.
- **VTEX API**: Deep integration for product catalog, pricing, and shipping calculation (e.g., `vtex_api_scraper.py`, `vtex_shipping.py`). This is the most complex integration due to varied endpoints.
- **Shopify API**: Integration for fetching products from Shopify-based stores (`shopify_api_client.py`). Generally more straightforward via `products.json`.
- **Mercado Livre**: Targeted scraping and search API integrations, handled via `curl_cffi` and Playwright fallbacks to bypass initial CAPTCHAs.
- **Netshoes**: Custom scraper endpoints with gender filtering and specific payload structures.
- **Amazon**: Specialized scraping with CAPTCHA-bypassing via Playwright and BrightData.

### Proxy & Anti-Bot Services
To ensure robust scraping capabilities, the system integrates with specialized proxy providers:
- **BrightData**: Configured via `BRIGHTDATA_PROXY_URL` for residential proxy routing to evade bot detection. Crucial for Amazon and ML.
- **ScraperAPI**: Integrated via `SCRAPERAPI_KEY` for CAPTCHA solving and IP rotation.

### Infrastructure & Caching
- **Redis**: Used as the primary caching layer (`REDIS_URL`). Specifically handles the Category Discovery Cache to prevent redundant live scraping across multiple instances.

### Local/Embedded Models
- **HuggingFace Transformers (CLIP)**: Used for visual similarity scoring (image-to-text / image-to-image relevance).
- **EasyOCR**: Used for text extraction from images locally, avoiding external API costs.
