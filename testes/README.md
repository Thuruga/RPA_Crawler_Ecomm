# Teste de extração de banners desktop

Este protótipo abre a home de cada marca ativa cadastrada em
`backend/data/brands.json`, simula uma tela desktop de **1366 × 768** e coleta todos
os slides de imagem do carrossel principal que estiverem disponíveis no DOM.

Além dos slides já carregados, o extrator tenta acionar o botão "próximo" do
carrossel para revelar imagens carregadas sob demanda.

Slides em vídeo são contabilizados no relatório, mas não são baixados: este teste
está deliberadamente focado nos banners de imagem desktop.

## Executar

Na raiz do projeto:

```powershell
python testes/extrair_banners.py
```

Para testar somente algumas marcas:

```powershell
python testes/extrair_banners.py --brands aramis ricardoalmeida
```

Para acompanhar o navegador durante o teste:

```powershell
python testes/extrair_banners.py --brands aramis --show-browser
```

## Resultado

Os arquivos são gerados em `testes/saida/`:

- `index.html`: galeria visual para conferir o que foi extraído;
- `resumo.csv`: uma linha por banner detectado;
- `resultado.json`: resultado completo da execução;
- `<marca>/viewport.png`: captura da primeira tela usada como contexto;
- `<marca>/banners/`: arquivos originais dos slides baixados.

O diretório `saida/` é ignorado pelo Git porque contém imagens e resultados
gerados. Ele pode ser apagado e recriado a qualquer momento.

## Regra atual de detecção

Uma imagem é considerada candidata quando ela, ou o contêiner de carrossel que a
contém:

- cruza a primeira tela;
- ocupa pelo menos 60% da largura da viewport;
- possui pelo menos 180 px de altura;
- vem de `img`, `picture`, atributos de lazy loading ou `background-image`.

Essa regra é propositalmente observável: falsos positivos e sites bloqueados ficam
visíveis na galeria junto com a captura da viewport.
