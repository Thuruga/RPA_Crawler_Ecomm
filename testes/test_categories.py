import pytest
from unittest.mock import AsyncMock, patch
from backend.services.vtex_api_scraper import VtexApiClient

@pytest.mark.asyncio
async def test_validate_url_with_empty_category():
    # Test if an empty category (e.g. returning '0 produtos') correctly returns False
    with patch('backend.services.vtex_api_scraper.AsyncSession') as MockSession:
        mock_session_instance = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.text = '<html><body><span>0 produtos</span></body></html>'
        mock_session_instance.get.return_value = mock_response
        MockSession.return_value.__aenter__.return_value = mock_session_instance

        result = await VtexApiClient.validate_url("https://www.test.com/category")
        assert result is False, "A categoria vazia não foi identificada corretamente."

@pytest.mark.asyncio
async def test_validate_url_with_valid_category():
    # Test if a valid category returns True
    with patch('backend.services.vtex_api_scraper.AsyncSession') as MockSession:
        mock_session_instance = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.text = '<html><body><div class="product">Camisa Azul</div><div class="product">Camisa Vermelha</div>' * 100 # Make it long enough
        mock_session_instance.get.return_value = mock_response
        MockSession.return_value.__aenter__.return_value = mock_session_instance

        result = await VtexApiClient.validate_url("https://www.test.com/category")
        assert result is True, "A categoria válida não foi identificada corretamente."

from backend.services.category_filter import is_masculine_category

def test_category_is_masculine_or_boys():
    # Test valid ones
    assert is_masculine_category("Moda Masculina", "/roupas/masculino") is True
    assert is_masculine_category("Infantil Menino", "/infantil-menino") is True
    assert is_masculine_category("Homem", "/homem") is True

    # Test invalid ones
    assert is_masculine_category("Moda Feminina", "/roupas/feminino") is False
    assert is_masculine_category("Infantil Menina", "/infantil-menina") is False
    assert is_masculine_category("Beleza", "/beleza") is False

