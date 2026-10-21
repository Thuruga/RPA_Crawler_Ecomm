"""Cadastro e gerenciamento de marcas persistido exclusivamente no banco PostgreSQL via Prisma."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import RootModel, ValidationError

from core.models import CategoryMapping, DynamicBrand, DynamicBrandCreate
from services.wake_category_paths import canonicalize_wake_category_source

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DB_SEED_FILE = DATA_DIR / "brands.json"

logger = logging.getLogger("BrandService")


class BrandDatabase(RootModel):
    root: Dict[str, DynamicBrand]


class BrandManagerService:
    def __init__(self):
        self.brands: Dict[str, DynamicBrand] = {}
        self.is_loaded: bool = False
        self.updated_event = asyncio.Event()
        self._ensure_offline_seed()

    def _ensure_offline_seed(self) -> None:
        """Carrega seed em memória apenas para testes unitários herméticos sem DB conectado."""
        if not self.brands and DB_SEED_FILE.exists():
            try:
                raw_data = json.loads(DB_SEED_FILE.read_text(encoding="utf-8"))
                validated_db = BrandDatabase.model_validate(raw_data)
                self.brands = validated_db.root
            except Exception:
                pass

    async def load_from_db(self) -> None:
        """Carrega todas as marcas e seus mapeamentos exclusivamente do banco de dados PostgreSQL."""
        from core.db import db
        try:
            if not db.is_connected():
                await db.connect()

            rows = await db.brands.find_many(
                include={"brand_category_mappings": True}
            )

            # Se o banco de dados estiver completamente vazio (primeiro boot), semeia uma única vez
            if not rows and DB_SEED_FILE.exists():
                logger.info("Banco PostgreSQL vazio. Inicializando marcas a partir do seed inicial...")
                try:
                    raw_data = json.loads(DB_SEED_FILE.read_text(encoding="utf-8"))
                    validated_db = BrandDatabase.model_validate(raw_data)
                    for _, brand in validated_db.root.items():
                        await self._sync_brand_to_db(brand)
                    rows = await db.brands.find_many(
                        include={"brand_category_mappings": True}
                    )
                except Exception as seed_err:
                    logger.error("Erro durante seed inicial no PostgreSQL: %s", seed_err)

            loaded: Dict[str, DynamicBrand] = {}
            for row in rows:
                config = {}
                if isinstance(row.configuration, str):
                    try:
                        config = json.loads(row.configuration)
                    except Exception:
                        config = {}
                elif isinstance(row.configuration, dict):
                    config = dict(row.configuration)

                config["brand_key"] = row.brand_key
                config["brand_name"] = row.brand_name
                config["domain"] = row.domain
                # A coluna engine do PostgreSQL é a fonte canônica
                resolved_engine = row.engine or config.get("engine") or "vtex"
                config["engine"] = resolved_engine
                config["is_active"] = row.is_active
                config["configuration"] = dict(config)

                if row.brand_category_mappings:
                    config["mappings"] = [
                        {
                            "canonical_slug": m.canonical_slug,
                            "vtex_fq_path": canonicalize_wake_category_source(
                                row.brand_key, m.source_path or ""
                            ),
                            "label": m.label or "",
                        }
                        for m in row.brand_category_mappings
                    ]
                elif "mappings" not in config:
                    config["mappings"] = []

                try:
                    brand_obj = DynamicBrand.model_validate(config)
                    loaded[row.brand_key.lower()] = brand_obj
                except ValidationError as ve:
                    logger.warning("Erro ao validar marca %s do banco: %s", row.brand_key, ve)

            if loaded:
                self.brands = loaded
                self.is_loaded = True
                self.updated_event.set()
                self.updated_event.clear()
                logger.info("%d marcas carregadas do banco de dados PostgreSQL.", len(self.brands))
        except Exception as exc:
            logger.error("Nao foi possivel carregar marcas do banco de dados: %s", exc)

    async def _sync_brand_to_db(self, brand: DynamicBrand) -> None:
        """Persiste ou atualiza uma marca e seus mappings no PostgreSQL."""
        from core.db import db
        try:
            if not db.is_connected():
                await db.connect()
            brand_dump = brand.model_dump()
            db_brand = await db.brands.upsert(
                where={"brand_key": brand.brand_key},
                data={
                    "create": {
                        "brand_key": brand.brand_key,
                        "brand_name": brand.brand_name,
                        "domain": brand.domain,
                        "engine": brand.engine,
                        "is_active": brand.is_active,
                        "configuration": json.dumps(brand_dump),
                    },
                    "update": {
                        "brand_name": brand.brand_name,
                        "domain": brand.domain,
                        "engine": brand.engine,
                        "is_active": brand.is_active,
                        "configuration": json.dumps(brand_dump),
                    },
                },
            )
            # Sincroniza mappings
            if db_brand and brand.mappings is not None:
                new_slugs = {m.canonical_slug for m in brand.mappings}
                existing_mappings = await db.brand_category_mappings.find_many(
                    where={"brand_id": db_brand.id}
                )
                for em in existing_mappings:
                    if em.canonical_slug not in new_slugs:
                        await db.brand_category_mappings.delete(where={"id": em.id})

                for m in brand.mappings:
                    await db.brand_category_mappings.upsert(
                        where={
                            "brand_id_canonical_slug": {
                                "brand_id": db_brand.id,
                                "canonical_slug": m.canonical_slug,
                            }
                        },
                        data={
                            "create": {
                                "brand_id": db_brand.id,
                                "canonical_slug": m.canonical_slug,
                                "source_path": m.vtex_fq_path,
                                "label": m.label,
                            },
                            "update": {
                                "source_path": m.vtex_fq_path,
                                "label": m.label,
                            },
                        },
                    )
        except Exception as exc:
            logger.warning("Erro ao sincronizar marca %s no banco: %s", brand.brand_key, exc)

    async def _delete_brand_from_db(self, brand_key: str) -> bool:
        """Exclui uma marca e seus mappings do PostgreSQL."""
        from core.db import db
        try:
            if not db.is_connected():
                await db.connect()
            db_brand = await db.brands.find_first(where={"brand_key": brand_key.lower().strip()})
            if db_brand:
                await db.brand_category_mappings.delete_many(where={"brand_id": db_brand.id})
                await db.brands.delete(where={"id": db_brand.id})
                logger.info("Marca '%s' excluida do PostgreSQL com sucesso.", brand_key)
                return True
        except Exception as exc:
            logger.warning("Erro ao excluir marca %s do PostgreSQL: %s", brand_key, exc)
        return False

    def _schedule_db_sync(self, brand: Optional[DynamicBrand] = None, deleted_key: Optional[str] = None) -> None:
        """Dispara sincronizacao em background caso chamado em contexto sincrono com loop ativo."""
        self.updated_event.set()
        self.updated_event.clear()
        try:
            loop = asyncio.get_running_loop()
            if brand is not None:
                loop.create_task(self._sync_brand_to_db(brand))
            elif deleted_key is not None:
                loop.create_task(self._delete_brand_from_db(deleted_key))
        except RuntimeError:
            pass

    def list_brands(self, active_only: bool = False) -> List[DynamicBrand]:
        """Retorna a lista de marcas carregadas do PostgreSQL."""
        brands = list(self.brands.values())
        return [brand for brand in brands if brand.is_active] if active_only else brands

    def get_brand(self, brand_key: str) -> Optional[DynamicBrand]:
        """Retorna uma marca pelo brand_key a partir dos dados do PostgreSQL."""
        return self.brands.get(brand_key.lower())

    def add_brand(self, data: DynamicBrandCreate) -> DynamicBrand:
        key = data.brand_key.lower().strip()
        if key in self.brands:
            self.brands[key].domain = data.domain
            self.brands[key].brand_name = data.brand_name
            if data.engine:
                self.brands[key].engine = data.engine
        else:
            self.brands[key] = DynamicBrand(**data.model_dump())

        brand = self.brands[key]
        self._schedule_db_sync(brand=brand)
        return brand

    async def add_brand_async(self, data: DynamicBrandCreate) -> DynamicBrand:
        brand = self.add_brand(data)
        await self._sync_brand_to_db(brand)
        return brand

    def save_brand(self, brand_data: dict) -> None:
        key = brand_data.get("brand_key", "").lower().strip()
        if not key:
            return
        self.brands[key] = DynamicBrand.model_validate(brand_data)
        self._schedule_db_sync(brand=self.brands[key])

    def set_active(self, brand_key: str, is_active: bool) -> Optional[DynamicBrand]:
        key = brand_key.lower()
        if key not in self.brands:
            return None
        self.brands[key].is_active = is_active
        self._schedule_db_sync(brand=self.brands[key])
        return self.brands[key]

    async def set_active_async(self, brand_key: str, is_active: bool) -> Optional[DynamicBrand]:
        brand = self.set_active(brand_key, is_active)
        if brand:
            await self._sync_brand_to_db(brand)
        return brand

    def update_mappings(
        self, brand_key: str, mappings: List[CategoryMapping]
    ) -> DynamicBrand:
        key = brand_key.lower()
        if key not in self.brands:
            raise KeyError(f"Marca {key} nao encontrada.")
        self.brands[key].mappings = mappings
        self._schedule_db_sync(brand=self.brands[key])
        return self.brands[key]

    async def update_mappings_async(
        self, brand_key: str, mappings: List[CategoryMapping]
    ) -> DynamicBrand:
        brand = self.update_mappings(brand_key, mappings)
        await self._sync_brand_to_db(brand)
        from services.category_discovery_cache import invalidate_cached_outcome

        await invalidate_cached_outcome(brand_key)
        return brand

    def delete_brand(self, brand_key: str) -> bool:
        key = brand_key.lower().strip()
        if key not in self.brands:
            return False
        del self.brands[key]
        self._schedule_db_sync(deleted_key=key)
        return True

    async def delete_brand_async(self, brand_key: str) -> bool:
        key = brand_key.lower().strip()
        if key not in self.brands:
            return False
        del self.brands[key]
        return await self._delete_brand_from_db(key)


brand_service = BrandManagerService()
