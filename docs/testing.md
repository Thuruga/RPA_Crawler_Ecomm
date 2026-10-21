# TESTING

## Data from [2026-07-15]

This document outlines the testing strategy, frameworks, and conventions for the project.

### Backend Testing (Python)
- **Framework**: `pytest` is used for all backend testing.
- **Location**: All backend tests are located in `backend/tests/`.
- **Configuration**: Managed via `pytest.ini`.
- **Mocking**: Use `unittest.mock` or `pytest-asyncio` for mocking external API calls (e.g., VTEX, ML, Shopify). We strictly avoid hitting real endpoints in unit tests to prevent rate limits and ensure CI speed.
- **Coverage Strategy**: Aim for high coverage on core business logic (e.g., `relevance_gates.py`, `nlp_service.py`), as these algorithms dictate the quality of the results. Testing parsers is also critical.

### Frontend Testing (React)
- **Framework**: `vitest` (configured in `package.json`) combined with React Testing Library.
- **Location**: Test files should be co-located with their respective components or in a dedicated `__tests__` folder.
- **Strategy**: Focus on testing complex Zustand store logic and critical UI components. Simple presentational components can have lower priority.

### E2E Testing & Playwright
Currently, the heavy reliance on anti-bot systems (like Playwright and ScraperAPI) makes end-to-end testing fragile. Focus on unit and integration tests with mocked responses. Playwright is primarily used for scraping, not for testing our own frontend UI.
