# Marketplace live audit

- Audit: `2026-08-24T163122+0000`
- Generated (UTC): `2026-08-24T16:31:22+00:00`
- Live: `true`
- External calls: `18`

## Marketplace Journeys

| Marketplace | Profile | Journey | Tier | Status | Calls | Duration (ms) |
|---|---|---|---|---|---:|---:|
| amazon | current | search | current | fresh_success | 1 | 2362 |
| amazon | current | pdp | current | fresh_success | 1 | 4664 |
| amazon | current | shipping | current | fresh_success | 1 | 9455 |
| amazon | coherent_http | search | coherent_http | fresh_success | 1 | 2274 |
| amazon | coherent_http | pdp | coherent_http | fresh_success | 1 | 5119 |
| amazon | coherent_http | shipping | coherent_http | blocked | 1 | 10731 |
| mercado_livre | current | search | current | fresh_success | 1 | 27887 |
| mercado_livre | current | pdp | current | parser_failure | 1 | 11452 |
| mercado_livre | current | shipping | current | parser_failure | 1 | 8334 |
| mercado_livre | coherent_http | search | coherent_http | fresh_success | 1 | 23751 |
| mercado_livre | coherent_http | pdp | coherent_http | parser_failure | 1 | 63122 |
| mercado_livre | coherent_http | shipping | coherent_http | parser_failure | 1 | 8485 |
| netshoes | current | search | current | blocked | 1 | 268 |
| netshoes | current | pdp | current | parser_failure | 1 | 10225 |
| netshoes | current | shipping | current | parser_failure | 1 | 4496 |
| netshoes | coherent_http | search | coherent_http | network_error | 1 | 491 |
| netshoes | coherent_http | pdp | coherent_http | parser_failure | 1 | 8352 |
| netshoes | coherent_http | shipping | coherent_http | parser_failure | 1 | 4598 |

## Search Gates

- Query: `camiseta masculina` (generic_camiseta_masculina) - **FAILED** (Accepted: 0)
- Query: `aramis camisa masculina` (business_aramis_camisa_masculina) - **FAILED** (Accepted: 0)

## Freight Gate

**FAILED**
- amazon ([REDACTED CEP]): unsupported - None
- amazon ([REDACTED CEP]): unsupported - None
- amazon ([REDACTED CEP]): unsupported - None
- amazon ([REDACTED CEP]): unsupported - None
- amazon ([REDACTED CEP]): unsupported - None
- mercado_livre ([REDACTED CEP]): unsupported - None
- mercado_livre ([REDACTED CEP]): unsupported - None
- mercado_livre ([REDACTED CEP]): unsupported - None
- mercado_livre ([REDACTED CEP]): unsupported - None
- mercado_livre ([REDACTED CEP]): unsupported - None
- netshoes ([REDACTED CEP]): unsupported - None
- netshoes ([REDACTED CEP]): unsupported - None
- netshoes ([REDACTED CEP]): unsupported - None
- netshoes ([REDACTED CEP]): unsupported - None
- netshoes ([REDACTED CEP]): unsupported - None
- shopify ([REDACTED CEP]): error - None
- shopify ([REDACTED CEP]): error - None
- shopify ([REDACTED CEP]): error - None
- shopify ([REDACTED CEP]): error - None
- shopify ([REDACTED CEP]): error - None
- vtex ([REDACTED CEP]): temporary_failure - None
- vtex ([REDACTED CEP]): temporary_failure - None
- vtex ([REDACTED CEP]): temporary_failure - None
- vtex ([REDACTED CEP]): temporary_failure - None
- vtex ([REDACTED CEP]): temporary_failure - None
- wake ([REDACTED CEP]): error - None
- wake ([REDACTED CEP]): error - None
- wake ([REDACTED CEP]): error - None
- wake ([REDACTED CEP]): error - None
- wake ([REDACTED CEP]): error - None

## Durable Job Evidence

- Durable job evidence not provided.

## Frontend Reconnect Evidence

- Frontend reconnect evidence not provided.

## Verdicts

- `amazon/coherent_http`: **not_circumvented**
- `amazon/current`: **circumvented_in_test_scope**
- `mercado_livre/coherent_http`: **not_circumvented**
- `mercado_livre/current`: **not_circumvented**
- `netshoes/coherent_http`: **not_circumvented**
- `netshoes/current`: **not_circumvented**
- `MKT-04`: **failed**
- `SEARCH-01`: **failed**
- `SHIP-01`: **failed**
- `VERIFY-01`: **failed**
