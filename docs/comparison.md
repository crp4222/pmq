# Comparison with the alternatives (2026-07-03)

Written by pmq's maintainer: an interested party. Every row is checkable
against the linked projects, and corrections are welcome as issues; the
table is dated so staleness is visible instead of silent.

| | pmq | py-clob-client-v2 (official) | pmxt | NautilusTrader | caiovicentino MCP |
|---|---|---|---|---|---|
| CLOB V2 signing | yes, local | yes, local | writes via its hosted backend | yes, local | V1 only (rejected in prod since 2026-04-28) |
| Confirmed-fill contract | yes (core design) | no (raw responses) | n/a | engine-level | no |
| Deposit wallet / POLY_1271 | yes, production-proven | open issues (#70 and others) | n/a | untested claim | no |
| Fee math | official per-category formula | fee at match, no helper | via backend | fee model | fee-blind |
| Reconciliation helper | yes | no | n/a | engine-level | no |
| Footprint | one small lib | one small lib | multi-venue platform | full trading framework | MCP server |

The honest routing advice: NautilusTrader is excellent if you want a full
backtesting and trading framework; pmxt is convenient if you accept
routing writes through their backend; raw py-clob-client-v2 is right if
you want no opinion layered on the official client. pmq is the small
self-custody library you embed in your own bot or agent.
