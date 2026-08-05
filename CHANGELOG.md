# Changelog

Notable changes to this project. Newest first.

## v1.0.2

### Fixed
- **Sim Account could not run equity strategies.** The LLM-code sandbox requires
  the full canonical column set, but the sim loaded equity CSVs as OHLCV-only, so
  pinning an equity (e.g. SPY) raised a `RuntimeError` ("missing required
  lowercase columns"). `_signal_array` now backfills the derivative columns
  (open_interest / funding_rate / liquidations) as 0.0 on a copy before the
  sandbox — matching the backtest path, without modelling phantom funding on
  equities.

## v1.0.1

### Fixed
- **Equity price-data resolution.** Sim accounts and the factor evaluator no
  longer assume the crypto `<SYM>-USDT.csv` filename for every instrument; they
  resolve through `universe.price_csv_path()`, so US equities (SPY, AAPL, …) load
  their real `<SYM>.csv` series. Fixes a "Market data missing for SPY" error in
  Sim Account and stops equity strategies from silently backtesting on the
  synthetic BTC series. BTC and crypto paths unchanged.

## v1.0.0 — first public release

Initial open-source release; feature history below.

## P8 — Reference-alignment overhaul

A large batch of UI + pipeline alignment work.

- **Stage re-numbering** — pipeline view uses a "Stage 0–5" framing (Stage 5 =
  Live Trade, with Small Cap + LIVE as sub-pills). The V1 endpoint
  `/api/pipeline/buckets` stays live for backward compatibility; the UI now
  consumes the V2 endpoint `/api/v2/pipeline/buckets`.
- **Soul Questions** — Critic emits a structured 6-key `soul_questions` dict
  (Q1 why works / Q2 what kills / Q3 counterparty / Q4 simple explanation /
  Q5 data availability / Q6 alpha decay). A regex fallback parses the H3
  sections if the LLM omits the JSON block.
- **Agent self-dialogue** — every status handoff / critic verdict is recorded
  into a ring buffer (`agent_dialogue.py`) and surfaced on Mission Control's
  "Show Dialogue" toggle.
- **Per-bar trade tape** — `_build_per_bar` emits `direction` / `mark_price` /
  `position_delta` per bar; the BACKTEST CSV tab renders coloured LONG / SHORT
  chips and paints the Position chart by direction.
- **Costs included** — strategy detail surfaces `fee_per_side` /
  `slippage_per_side` / `round_trip_cost` from the existing `raw_backtest`
  payload.
- **Structured Backtest Config** — `config.backtest_config` YAML is rendered as
  labelled cards (Data Sources / Direction / Entry & Exit / Model / Parameters).
- **VERDICT: PASS / FAIL** — verdict banner leads with a PASS / FAIL chip;
  the sub-label preserves the prior risk-sanctioned / promoted / failed text.
- **10 personas** — Team A Lead (Aaron), Team B Lead (Bella), and the
  Postmortem Writer (Mira) join the team; Mission Control's AgentTeam card
  groups by `persona.team` (A / B / ops).
- **Knowledge Graph mounted** — `/kb-explorer` exposes a Graph / Files toggle.
- **ArticlePreview** — Related Concepts chips + Derived Code link + Postmortems
  reverse-link rail for `past_alpha` kinds.
- **Cointegration** — `/cointegration` runs a real Engle-Granger scan over a
  30-asset synthetic universe (`generate_multiasset` in `data_gen.py`, cached
  on disk for 12h).
- **Live Trade deploy** — a Promotion Queue card on `/live-trade` lists every
  `SMALL_CAPITAL` strategy and exposes a DEPLOY → LIVE confirm modal (typed
  phrase + 5 s countdown) feeding `/api/strategies/{id}/promote`.
- **Discord /intake bot** — `discord.py` gateway client behind
  `DISCORD_INBOUND_ENABLED=1` + `DISCORD_BOT_TOKEN` +
  `DISCORD_ALLOWED_CHANNEL_IDS` (mirrors the Telegram inbound bot).
- **Backtest Lab tab refactor** — `?tab=history|sweep` URL state; History is
  the default tab (multi-strategy overlay + ALL COMBINED curve); Sweep retains
  the parameter grid runner.
- **Portfolio Optimizer** — EVENLY SPREAD / RISK PARITY toggle + an Efficient
  Frontier scatter panel.
- **Arena N-way** — up to 4 strategies overlaid with an NxN correlation heatmap.
- **Nav reorder** — Execution group moved above Knowledge; Alpha Lab promoted
  to the top of Knowledge.
