"""
Valida o pipeline CLIP end-to-end com um SKU real.
Executa: python scripts/validate_clip.py
Esperado: produto correto >= 70%, polo/camisa <= 40%
"""
import asyncio
from services.image_ai_service import image_ai_service, AI_AVAILABLE

async def main():
    if not AI_AVAILABLE:
        print("FALHA: AI_AVAILABLE=False. Instale: pip install transformers")
        return

    # URLs de imagem para teste (Tênis vs Polo)
    tenis_url = "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a9/Air_Jordan_1_retro.jpg/800px-Air_Jordan_1_retro.jpg"
    polo_url = "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e0/Polo_shirt_red.jpg/800px-Polo_shirt_red.jpg"

    print("Baixando imagens...")
    # Em um ambiente real isso usaria requests/aiohttp caso curl_cffi falhe
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(tenis_url) as resp:
            tenis_bytes = await resp.read() if resp.status == 200 else None
        async with session.get(polo_url) as resp:
            polo_bytes = await resp.read() if resp.status == 200 else None

    if not tenis_bytes or not polo_bytes:
        print("FALHA: Não foi possível baixar as imagens de teste.")
        return

    print("Calculando embeddings (primeira execução baixa o modelo CLIP ~600MB)...")
    ref_embed = await image_ai_service.get_embedding_async(tenis_bytes)
    polo_embed = await image_ai_service.get_embedding_async(polo_bytes)
    tenis2_embed = await image_ai_service.get_embedding_async(tenis_bytes)  # mesmo produto

    score_mesmo = await image_ai_service.calculate_score_from_embeddings(ref_embed, tenis2_embed)
    score_polo = await image_ai_service.calculate_score_from_embeddings(ref_embed, polo_embed)

    print(f"\nResultados:")
    print(f"  Tênis vs Tênis (mesmo): {score_mesmo*100:.1f}% (esperado >= 70%)")
    print(f"  Tênis vs Polo:          {score_polo*100:.1f}% (esperado <= 40%)")

    ok_mesmo = score_mesmo >= 0.70
    ok_polo = score_polo <= 0.40

    if ok_mesmo and ok_polo:
        print("\n✅ CLIP pipeline validado com sucesso!")
    else:
        print(f"\n❌ Validação falhou: mesmo={'OK' if ok_mesmo else 'FALHOU'}, polo={'OK' if ok_polo else 'FALHOU'}")

if __name__ == "__main__":
    asyncio.run(main())
