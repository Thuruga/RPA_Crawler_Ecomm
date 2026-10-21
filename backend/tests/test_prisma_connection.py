import pytest
import pytest_asyncio
from prisma import Prisma

@pytest_asyncio.fixture
async def db():
    client = Prisma()
    await client.connect()
    yield client
    await client.disconnect()

@pytest.mark.asyncio
async def test_prisma_connection_healthy(db: Prisma):
    # Execute a simple raw query
    result = await db.query_raw("SELECT 1 as is_alive")
    assert result[0]['is_alive'] == 1

@pytest.mark.asyncio
async def test_prisma_models_exist(db: Prisma):
    # Verify that we can query the 'brands' model (even if empty, it shouldn't crash)
    brands = await db.brands.find_many(take=1)
    assert isinstance(brands, list)
