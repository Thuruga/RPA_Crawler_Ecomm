import asyncio
from prisma import Prisma

# Instância global do Prisma Client para ser usada
# em toda a aplicação e workers background.
db = Prisma()
_attached_loop = None


async def ensure_db_connected() -> Prisma:
    """Garante que a conexão global com o banco está ativa no event loop atual."""
    global db, _attached_loop
    loop = asyncio.get_running_loop()
    if _attached_loop is not loop or not db.is_connected():
        if db.is_connected():
            engine = getattr(db, "_engine", None)
            if engine and getattr(engine, "process", None):
                try:
                    engine.process.kill()
                except Exception:
                    pass
            db._engine = None
        await db.connect()
        _attached_loop = loop
    return db
