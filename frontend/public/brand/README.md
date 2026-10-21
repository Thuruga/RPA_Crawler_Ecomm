# Assets oficiais Aramis

Fonte canônica: `C:/Users/arthur.correia/Downloads/OneDrive_2026-07-13.zip`, exclusivamente sob `Guia de Marca/Oficial`.

O responsável confirmou em 13/07/2026 que Druk Wide e Moderat Extended Regular 400 não existem no pacote fornecido. O frontend usa, portanto, somente as fontes oficiais disponíveis: Moderat Extended Bold como papel de título/UI 700 e Moderat Condensed Regular como papel de texto/UI 400. Nenhuma fonte foi convertida ou obtida externamente. O documento comercial de licença presente no pacote não foi copiado para o repositório.

## Arquivos copiados sem alteração

| Arquivo local | Origem no ZIP | Uso | Dimensões | SHA-256 |
|---|---|---|---|---|
| `aramis-logo-negative.svg` | `Guia de Marca/Oficial/Marca/Monocromática/RGB/SVG/AMIS.0001.Marca.Monocromatica.RGB-02.svg` | Marca sobre a sidebar preta | viewBox 811.81×249.14 | `2e5e775e8b496679ac526f5d7c42bf6612dd293500bf20a31c65509cf73c4d70` |
| `aramis-monogram-negative.svg` | `Guia de Marca/Oficial/Monograma/Monocromático/RGB/SVG/AMIS.0001.Monograma.Monocromatico.RGB-02.svg` | Rail e navegação compacta | viewBox 670×570 | `9e56391608f079de92780b0eaf07068facba75ea44ebe9493b38d2c3a50d5149` |
| `aramis-logo-positive.svg` | `Guia de Marca/Oficial/Marca/Preferencial/RGB/SVG/AMIS.0001.Marca.Preferencial.RGB.svg` | Marca eventual sobre branco | viewBox 811.81×249.14 | `a9913d16acd42807f35bda2a6a8b0a27cfd7433521f1746ddc13f52c126af48d` |
| `aramis-monogram-positive.svg` | `Guia de Marca/Oficial/Monograma/Preferencial/RGB/SVG/AMIS.0001.Monograma.Preferencial.RGB.svg` | Monograma eventual sobre branco | viewBox 670×570 | `cced4b8f5931c625d44f4958056976f7d1f7078b89087d4b3509716b295705f4` |

As fontes ficam em `src/assets/fonts/`:

| Arquivo local | Origem no ZIP | Papel | SHA-256 |
|---|---|---|---|
| `Moderat-Extended-Bold.woff2` | `Guia de Marca/Oficial/fontes/moderat/Moderat-Extended-Bold-Web/Moderat-Extended-Bold.woff2` | Títulos e UI 700 | `2e0443e0753e3b78a8032089f5695fb2fe7842fff238c87e562fe91251a0136b` |
| `Moderat-Condensed-Regular.ttf` | `Guia de Marca/Oficial/fontes/moderat/Condensed/Moderat-Condensed-Regular.ttf` | Texto e UI 400 | `484bef867bd3568e4f1411c00b2082de0a758f794d482ab56e483e6f23df3805` |

## Derivados técnicos

Os favicons foram redimensionados, sem redesenho, distorção ou recolorização, a partir de `Guia de Marca/Oficial/Monograma/Preferencial/RGB/PNG/AMIS.0001.Monograma.Preferencial.RGB.png` (1270×1080; SHA-256 `f154a99387d02eec7dac3e43a790c1c4e5d4db3fe572c174b8aaa03a35c84af4`). A proporção original foi preservada e centralizada em canvas transparente quadrado.

| Arquivo | Canvas | Marca renderizada | SHA-256 |
|---|---:|---:|---|
| `favicon-16.png` | 16×16 | 16×14 | `bf7f3a8ee18d96f13947f21ea270ae7b815bb8d40c8665de219e31a663e3e607` |
| `favicon-32.png` | 32×32 | 32×27 | `4f2ebdb3825610288fe197792a568511b58b9c7164138a61b39d92328c6cb61e` |
| `apple-touch-icon.png` | 180×180 | 180×153 | `60fce9df8a01c515481ad52eaeb9948e6eb6f4a37e38beba6eae1123f5121fcb` |

## Segurança e escopo

- Os quatro SVGs foram inspecionados contra `script`, handlers `on*=`, `javascript:`, URLs HTTP(S) em atributos ativos e `url()`; nenhuma ocorrência foi encontrada.
- Os SVGs devem ser servidos como arquivos estáticos por `<img>`, nunca inlineados com `dangerouslySetInnerHTML`.
- `Backup - Não usar`, `__MACOSX` e materiais comerciais não foram utilizados.
- Nenhum grafismo foi importado ou improvisado: os originais web oficiais não estão disponíveis no pacote.
