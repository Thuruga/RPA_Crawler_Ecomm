# STRUCTURE

## Data from [2026-07-15]

This document describes the directory layout and file organization of the repository.

### Root Directory
- `backend/`: Contains the FastAPI server, Python scripts, and AI models.
- `frontend/`: Contains the React SPA application.
- `.planning/`: GSD workflow state and codebase mapping documents.
- `docs/`: Project documentation.
- `testes/`: Standalone test scripts or data.

### Backend Structure (`backend/`)
- `api/`: API routers organized by domain (e.g., `routes_search.py`, `routes_brands.py`).
- `services/`: Business logic and orchestrators.
  - `engines/`: Specific marketplace scraping logic.
  - `promotions/`, `shipping/`: Sub-domains for promo extraction and freight calculation.
- `infrastructure/`: Networking and HTTP clients (e.g., `http/` folder for base clients).
- `core/`: Core utilities and generic helper functions.
- `data/`: Local storage for JSON or Excel datasets (e.g., category mapping files).
- `tests/`: Pytest test suite for the backend.
- `scripts/`: Utility scripts for maintenance.
- `config.py`: The central configuration hub (Pydantic Settings).
- `app.py`: FastAPI entry point.

### Frontend Structure (`frontend/`)
- `src/`: Source code for the React app.
  - `components/`: Reusable UI components.
  - `stores/`: Zustand state management files.
  - `api/`: Axios or Fetch wrappers to communicate with the backend.
  - `assets/`: Static assets (images, icons).
  - `styles/`: Global CSS or modular stylesheets.
  - `App.tsx`: Main application component and routing.
  - `main.tsx`: React DOM entry point.
- `public/`: Public static files served by Vite.
- `package.json`: NPM dependencies and scripts.
- `vite.config.ts`: Vite bundler configuration.

### Naming Conventions
- **Backend Files**: `snake_case.py`
- **Frontend Components**: `PascalCase.tsx`
- **Frontend Utilities**: `camelCase.ts`
