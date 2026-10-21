# STACK

## Data from [2026-07-15]

This document maps the technology stack used across the project, including both backend and frontend environments, as well as the runtime and tooling.

### Backend Stack
- **Language**: Python 3.x
- **Framework**: FastAPI (high performance, async-first web framework)
- **ASGI Server**: Uvicorn (`uvicorn[standard]>=0.29.0`)
- **Configuration & Validation**: Pydantic (`>=2.0`) and Pydantic Settings
- **Task Scheduling**: APScheduler (`>=3.10.0`) for cron/background jobs
- **Caching**: Redis (`>=5.0.0`) for discovery cache

### Data Processing & AI
- **Data Manipulation**: Pandas (`>=2.0`), Openpyxl (`>=3.1.0`)
- **NLP & Vision Models**: Transformers (`>=4.40.0`), Rapidfuzz (`>=3.0.0`) for string matching
- **OCR**: EasyOCR (`>=1.7.1`), OpenCV-python-headless (`>=4.9.0`)

### Scraping & Networking
- **HTTP Clients**: `aiohttp` (async requests), `curl_cffi` (impersonation)
- **Browser Automation**: Playwright (`>=1.40.0`), Playwright-stealth
- **HTML Parsing**: BeautifulSoup4 (`>=4.12.0`)

### Frontend Stack
- **Library**: React 19 (`^19.2.5`) with TypeScript
- **Build Tool**: Vite (`^8.0.16`)
- **State Management**: Zustand (`^5.0.14`)
- **UI Components & Icons**: Lucide-react (`^1.14.0`), Framer-motion (`^12.38.0`), Sonner
- **Charts**: Recharts (`^3.8.1`)

### Tooling & CI/CD
- **Linting/Formatting**: ESLint (`^10.2.1`)
- **Testing**: Vitest (`^4.1.10`) for frontend, Pytest for backend
- **Environment Management**: `.env` files via `python-dotenv`
