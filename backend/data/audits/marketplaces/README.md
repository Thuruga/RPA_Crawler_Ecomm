# Auditoria controlada dos marketplaces

Este diretório contém o contrato público e os relatórios sanitizados da auditoria de Amazon, Mercado Livre e Netshoes. A auditoria mede o comportamento do código existente; ela não altera engines de produção e não autoriza contorno de CAPTCHA, rotação de IP, Tor ou proxy não credenciado.

## O que conta como sucesso

`fresh_success` exige uma chamada externa feita na execução atual, sem cache stale e sem challenge. A busca precisa entregar produto com nome, preço positivo e URL HTTPS do domínio esperado. A PDP precisa ter identidade e preço positivo. Frete precisa ser explicitamente grátis ou estar disponível com preço positivo; zero inferido nunca é aceite.

Os demais estados permanecem separados: `blocked`, `parser_failure`, `legitimate_empty`, `stale`, `timeout`, `network_error`, `unsupported`, `not_fresh` e `not_reached`. HTTP 403/429/503 ou sinais de CAPTCHA, Anubis, Access Denied e login wall são `blocked`, inclusive quando o servidor responde HTTP 200. HTTP 200 sem challenge e sem a estrutura esperada é `parser_failure`.

Um marketplace só pode receber o veredito `circumvented_in_test_scope` quando busca, PDP e frete são `fresh_success` no mesmo perfil. Qualquer gap resulta em `not_circumvented`.

## Teste hermético (sem rede)

No PowerShell, a partir da raiz do repositório:

```powershell
Set-Location backend
python -m pytest tests/test_marketplace_live_audit.py tests/test_marketplace_resilience.py tests/test_marketplace_status_propagation.py -q
python scripts/audit_marketplaces_live.py --manifest data/audits/marketplaces/acceptance_manifest.json --freight-matrix data/audits/marketplaces/freight_acceptance_matrix.json
```

O segundo comando é dry-run por padrão: valida manifesto e seleções, imprime a matriz, orçamento, concorrência 1 e `attempts_executed: 0`. Importar o módulo, coletar pytest ou executar o dry-run não cria sessão, browser ou socket.

## Ensaio live opt-in

Pré-requisitos: dependências do backend instaladas. Os engines atuais podem acionar seus fallbacks Playwright internos; nesse caso, Playwright e o Chromium do projeto precisam estar disponíveis. Execute somente no egress aprovado, respeitando os termos dos marketplaces e os cooldowns do manifesto.

```powershell
Set-Location backend
python scripts/audit_marketplaces_live.py --live `
  --profiles current,coherent_http `
  --marketplaces amazon,mercado_livre,netshoes `
  --journeys search,pdp,shipping `
  --manifest data/audits/marketplaces/acceptance_manifest.json `
  --freight-matrix data/audits/marketplaces/freight_acceptance_matrix.json `
  --max-external-calls 36 `
  --output data/audits/marketplaces
```

- `current` é o baseline dos engines existentes, sem usar a camada de cache resiliente como evidência.
- `coherent_http` usa o único fingerprint Chrome 120 compatível com o TLS impersonation atualmente fixado em Amazon e Mercado Livre; no Netshoes, também fixa as escolhas da `SessionFactory`. O perfil é restaurado após cada jornada e não altera produção.
- Os fallbacks Playwright continuam sendo responsabilidade dos engines atuais e podem ocorrer dentro de ambos os perfis. O runner não inventa uma linha `browser_fallback` separada porque os engines não expõem todos os tiers de browser como interfaces independentes comparáveis.

A PDP e o frete marketplace usam exclusivamente a URL obtida por uma busca fresca da rodada. Se a busca falhar, ambos ficam `not_reached`. O marketplace usa o CEP `01310100`. As integrações SHIP-01 (frete) usam matriz multirregional separada carregada por `--freight-matrix`. A execução é sequencial, com orçamento global moderado (36 chamadas externas) e por marketplace (12 chamadas), timeout (45s) e cooldown (3s a 6s) com jitter limitado. `external_calls` representa chamadas lógicas reservadas pelo runner; engines com fallback interno podem realizar mais de uma requisição física, portanto o campo é um limite inferior e não deve ser usado como telemetria de rede exata.

Para verificar o relatório mais recente sem acessar a rede:

```powershell
python scripts/audit_marketplaces_live.py --verify-latest --output data/audits/marketplaces
```

## Evidencia de jobs duraveis e reconexao frontend

O closeout da Phase 10 deve anexar evidencia estruturada para `search`, `cross`, `category_scan` e `price_check` usando `--job-evidence`. O restart real controlado usa por padrao um job `search`, porque esse tipo possui factory de retomada estavel e precisa chegar a `completed` apos a reconciliacao.

Runbook local padrao, a partir da raiz do repositorio:

```powershell
Set-Location backend
python -m uvicorn app:app --host 127.0.0.1 --port 8080
```

Em outra sessao, inicie o job de aceite, registre `job_id`, status e timestamp antes do restart, interrompa e suba novamente o mesmo processo Uvicorn com o comando acima, e registre o status apos a reconciliacao. O JSON passado para `--job-evidence` deve conter `restart_job_kind: search`, `backend_restart_command`, `restart_started_at`, `restart_completed_at`, snapshots `pre_restart`/`post_restart`, `duplicate_check` e os quatro tipos em `job_types`. `search` e `cross` devem terminar como `completed`; `category_scan` e `price_check` podem terminar como `completed`/`ok` ou falha terminal honesta com `failure_reason`, desde que nao desaparecam e nao dupliquem.

Se o alvo de producao atual for um servico gerenciado em vez do processo local, registre no mesmo campo `backend_restart_command` a acao exata usada no servico gerenciado, incluindo timestamp. Nao substitua esse passo por simulacao hermetica.

A reconexao frontend e evidencia interna. Use `--frontend-checklist` para anexar um checklist JSON ou Markdown com rota observada, reload/reconnect executado, job ainda visivel, ausencia de duplicacao e `screenshot_path` quando houver captura. Nao inclua cookies, tokens, headers, storage bruto, HTML bruto, credenciais de proxy ou screenshots com segredos.

Exemplo de closeout com evidencias anexadas:

```powershell
python scripts/audit_marketplaces_live.py --live `
  --manifest data/audits/marketplaces/acceptance_manifest.json `
  --freight-matrix data/audits/marketplaces/freight_acceptance_matrix.json `
  --job-evidence data/audits/marketplaces/job_evidence.json `
  --frontend-checklist data/audits/marketplaces/frontend_reconnect_checklist.md `
  --phase-report-output ..\.planning\phases\10-verifica-o-operacional-de-ponta-a-ponta `
  --output data/audits/marketplaces
```
## Interpretação e decisão

Compare por marketplace e perfil: número de jornadas `fresh_success`, bloqueios, latência, chamadas externas e fallback. Recomende uma alternativa gratuita apenas quando ela aumenta as jornadas frescas sem aumentar bloqueios e completa as três jornadas. Empates favorecem menos chamadas e menor complexidade.

Se nenhum perfil completar busca, PDP e frete, registre `não contornado` e considere API oficial/credenciada ou ajuste legítimo de egress. Não instale Camoufox, não use Tor e não faça rotação de IP como parte desta bateria.

Os artefatos não guardam cookies, headers de autenticação, credenciais de proxy, query strings nem HTML bruto. Faça duas rodadas em horários distintos somente após aprovação operacional e não repita marketplace bloqueado antes do `next_retry_at` informado pelo sistema.
