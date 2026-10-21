# CONVENTIONS

## Data from [2026-07-15]

This document catalogs the coding conventions, style guides, and design patterns used across the project.

### Backend Conventions (Python)
- **Typing**: Strict type hinting is enforced throughout the backend. Use `typing` module extensively.
- **Async First**: All I/O bound operations (scraping, API calls) must use `async`/`await`. Avoid blocking the event loop.
- **Configuration**: No magic numbers or hardcoded URLs in services. All configurations must reside in `config.py` using `pydantic-settings`.
- **Error Handling**: Use `try/except` with specific exceptions. Fallbacks (e.g., HTTP -> Playwright) should be gracefully handled and logged.
- **Logging**: Use the standard `logging` module. Print statements are discouraged in production code.

### Frontend Conventions (React/TS)
- **Typing**: TypeScript strict mode is enabled. Define interfaces for all API responses and component props.
- **Components**: Use Functional Components and React Hooks exclusively.
- **State Management**: Local state should use `useState`. Global state should use `Zustand`. Avoid prop drilling.
- **Styling**: Styles should be self-contained or use scoped CSS/Tailwind (if configured). Avoid polluting global CSS beyond `index.css`.

### General Patterns
- **API Responses**: Standardize JSON responses. Handlers should return Pydantic models directly (FastAPI handles serialization).
- **Naming**: 
  - Python: `snake_case` for variables, functions, and files. `PascalCase` for classes.
  - TS/JS: `camelCase` for variables and functions. `PascalCase` for components.
