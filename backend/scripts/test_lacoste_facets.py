import asyncio, aiohttp, json

async def run():
    url = 'https://cu0iyshi42-dsn.algolia.net/1/indexes/*/queries?x-algolia-application-id=CU0IYSHI42&x-algolia-api-key=7e20c005d05fe8f113d8b7e079bf34a1'
    
    test_cats = {
        'polos': ['categoryLabel:POLOS'],
        'camisetas': ['categoryLabel:TEE-SHIRTS & GOLAS ALTAS'],
        'camisas': ['categoryLabel:CAMISA'],
        'moletons_jaquetas': ['categoryLabel:SWEATS', 'categoryLabel:BLUSÃO', 'categoryLabel:PULLOVER'],
        'calcas': ['categoryLabel:CALCAS'],
        'bermudas': ['categoryLabel:BERMUDAS', 'categoryLabel:CALÇÕES'],
        'calcados': ['categoryLabel:COURT SNEAKERS', 'categoryLabel:ATHLEISURE SNEAKERS', 'categoryLabel:VULCANIZED SNEAKERS', 'categoryLabel:SLIDES & SANDALS'],
        'acessorios': ['categoryLabel:GORROS', 'categoryLabel:MEIAS', 'categoryLabel:ÓCULOS', 'categoryLabel:RELÓGIO', 'categoryLabel:LEATHER GOODS BELT'],
    }
    
    headers = {'Origin': 'https://www.lacoste.com', 'Referer': 'https://www.lacoste.com/'}
    from urllib.parse import urlencode
    async with aiohttp.ClientSession() as session:
        for cat_name, cat_facets in test_cats.items():
            facet_filters = [["genderLabel:Masculino"], cat_facets]
            params = {
                "query": "",
                "facetFilters": json.dumps(facet_filters),
                "hitsPerPage": 0
            }
            payload = {
                'requests': [{
                    'indexName': 'products_br_pt',
                    'params': urlencode(params)
                }]
            }
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json()
                if 'results' in data:
                    total = data['results'][0].get('nbHits', 0)
                    print(f'{cat_name:20s}: {total} produtos encontrados no Algolia')
                else:
                    print(f'{cat_name:20s}: ERRO -> {data}')

if __name__ == '__main__':
    asyncio.run(run())
