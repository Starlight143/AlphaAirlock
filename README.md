# Agentic Alpha Research System

A multi-agent quant research pipeline that turns unstructured market commentary
into sandbox-tested, risk-critiqued alpha strategies. Built around five
specialized Claude agents (Intake → Researcher → Coder → Backtester → Critic)
and a Bloomberg-style split workspace UI. It backtests on a bundled synthetic
BTC dataset and can ingest real market data across a multi-asset universe
(crypto perps via Binance, US equities via Yahoo). Live trading is **disabled
by default** and sits behind explicit environment gates — out of the box the
system runs in backtest / paper mode only.

> ⚠️ **Disclaimer — research & educational use only.** This software is **not**
> financial, investment, or trading advice, and is provided **"as is" without
> warranty of any kind**. Automated trading — especially crypto and leveraged
> perpetuals — carries substantial risk, up to and including total loss of
> capital. You alone are responsible for any use; the authors accept no
> liability for any loss or damage. See
> [Disclaimer & risk notice](#disclaimer--risk-notice) below.

```
+--------------------+    +-------------+    +---------+    +-------------+
| Raw market text    | -> | Intake (LLM)| -> | KN node | -> | Researcher  |
+--------------------+    +-------------+    +---------+    +------+------+
                                                                    |
                              +---------+    +-------------+        v
                              | Critic  | <- | Backtester  | <-- Coder (LLM)
                              | (LLM)   |    | hourly + LH |
                              +----+----+    +-------------+
                                   |
                                   |  Go        No-Go (1 retry)
                                   v                |
                              +----+-----+          v
                              | APPROVED |    +----------+
                              +----------+    | REJECTED |
                                              +----------+
```

---

## What's in the box

| Layer        | Path                              | What it does                                                              |
| ------------ | --------------------------------- | ------------------------------------------------------------------------- |
| API          | `backend/app/main.py`             | FastAPI server (`:8000`) — intake / research / pipeline / portfolio / graph |
| Agents       | `backend/agents/`                 | Intake, Researcher, Coder, Critic — all Claude-backed                     |
| Engine       | `backend/core/engine.py`          | Look-ahead-bias-free hourly backtester (Sharpe scaled by √8760)           |
| Sandbox      | `backend/core/sandbox.py`         | AST-whitelisted exec for LLM-generated factor code                        |
| Orchestrator | `backend/core/orchestrator.py`    | State machine (INTAKE → … → APPROVED/REJECTED) with 1 retry loop          |
| Data         | `backend/core/data_gen.py` · `market_data.py` | Bundled 2 yr synthetic hourly BTC perp dataset (short-squeeze regimes) **plus** real multi-asset ingest — crypto perps + US equities — into `storage/prices/` |
| Portfolio    | `backend/core/portfolio.py`       | Inverse-volatility (risk parity) allocator with ε-floor                   |
| UI           | `frontend/` (Next.js App Router)  | Split workspace: knowledge graph + tabbed strategy detail                 |
| E2E test     | `backend/test_pipeline.py`        | One-shot integration test (requires `ANTHROPIC_API_KEY`)                  |
| Launchers    | `start.sh` (Linux/macOS/Git Bash), `start.ps1` (Windows) | Boot both services + trap clean shutdown               |

---

## Prerequisites

- **Python 3.11+** (3.12 recommended)
- **Node.js 20+** (for the Next.js frontend)
- **One LLM provider key** — one of:
  - an **Anthropic** API key, or
  - an **OpenRouter** API key (any OpenAI-compatible endpoint), or
  - a **MiniMax** API key (international `api.minimax.io`, token plan).

## LLM provider configuration

The agent layer speaks to whichever backend you set in `.env`:

| Env var                  | Anthropic native                       | OpenRouter (OpenAI-compatible)                | MiniMax (international)                          |
| ------------------------ | -------------------------------------- | --------------------------------------------- | ----------------------------------------------- |
| `LLM_PROVIDER`           | `anthropic`                            | `openrouter`                                  | `minimax`                                       |
| API key var              | `ANTHROPIC_API_KEY`                    | `OPENROUTER_API_KEY`                          | `MINIMAX_API_KEY`                               |
| Model var                | `ANTHROPIC_MODEL` (`claude-3-5-sonnet-latest` default) | `OPENROUTER_MODEL` (`anthropic/claude-sonnet-4.6` default) | `MINIMAX_MODEL` (`MiniMax-M3` default)          |
| Optional base URL        | n/a                                    | `OPENROUTER_BASE_URL` (any OpenAI-compatible endpoint) | `MINIMAX_BASE_URL` (`https://api.minimax.io/v1`) |
| Optional ranking headers | n/a                                    | `OPENROUTER_HTTP_REFERER`, `OPENROUTER_X_TITLE` | n/a                                             |

Pick whichever fits. Examples:

```bash
# A) Anthropic native
export LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-ant-...
export ANTHROPIC_MODEL=claude-3-5-sonnet-latest      # optional

# B) OpenRouter routing to any model
export LLM_PROVIDER=openrouter
export OPENROUTER_API_KEY=sk-or-...
export OPENROUTER_MODEL=anthropic/claude-sonnet-4.6   # or openai/gpt-4o, etc.

# C) MiniMax international (token plan, OpenAI-compatible)
export LLM_PROVIDER=minimax
export MINIMAX_API_KEY=...                            # your MiniMax key — never the OpenRouter one
export MINIMAX_MODEL=MiniMax-M3                        # optional
```

`LLM_PROVIDER=auto` (or unset) auto-picks Anthropic when its key is present,
otherwise OpenRouter. `minimax` must be selected explicitly (it is **not** part
of the `auto` fallback) and always uses its own `MINIMAX_API_KEY` — never your
OpenRouter key. The shipped `.env.example` sets `LLM_PROVIDER=minimax`; change it
to whichever provider you hold a key for. The agent prompts and
`response_format={"type":"json_object"}` contract behave identically across
providers - OpenRouter forwards the JSON mode to the underlying model; the
Anthropic provider translates it into the canonical assistant-prefill `{`
technique.

---

## Quick start

### 1. Clone & configure

```bash
git clone https://github.com/Starlight143/AlphaAirlock.git
cd AlphaAirlock
cp .env.example .env
# then edit .env and set the key for your chosen LLM_PROVIDER.
# The backend auto-loads .env at startup (via backend/_envloader.py),
# so you do NOT need to also export the vars in your shell.
# If you do export them, shell values win over .env (override=False).
```

Or, in your current shell:

```bash
# bash / zsh
export ANTHROPIC_API_KEY=sk-ant-...

# Windows PowerShell
$env:ANTHROPIC_API_KEY = 'sk-ant-...'
```

### 2. Install dependencies

```bash
# Python — from project root
python -m pip install -r requirements.txt

# Node — from frontend/
cd frontend && npm install && cd ..
```

### 3. Generate the synthetic dataset (one time)

```bash
python backend/core/data_gen.py
# -> writes backend/data/synthetic_btc.csv (17,520 hourly bars)
```

### 4. Boot both services

**One-command launcher (recommended):**

```bash
# Linux / macOS / Git Bash on Windows
chmod +x start.sh
./start.sh
```

```powershell
# Windows native PowerShell
./start.ps1
```

Both launchers:
- install Python deps if missing
- generate `synthetic_btc.csv` if missing
- start FastAPI on `http://127.0.0.1:8000`  (Swagger docs at `/docs`)
- start Next.js on `http://localhost:3000`
- install a Ctrl+C trap that kills both child processes — no orphaned
  `uvicorn` / `node` workers survive shutdown.

**Manual (two terminals):**

```bash
# Terminal 1 — backend
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000

# Terminal 2 — frontend
cd frontend && npm run dev
```

### 5. Use the UI

1. Open <http://localhost:3000>.
2. Click **+ INGEST RAW SOURCE** in the header.
3. Paste any market commentary (the dialog pre-fills a funding-rate example
   you can use immediately).
4. Watch the right-hand pipeline stepper transition through
   **Intake → Hypothesis → Coding → Backtesting → Critic Loop → Deployed**.
5. Click any node on the left-hand **Knowledge Anomaly Graph** to inspect its
   strategy. Colors map to lifecycle stage:
   - **Blue (`#3B82F6`)** — Stage 0/1 (intake / story)
   - **Purple (`#A855F7`)** — Stage 2/3 (coding / backtesting / critic)
   - **Amber (`#F59E0B`)** — Stage 4 approved
6. To subscribe to external feeds (RSS / Substack / Medium / Reddit / YouTube /
   Twitter / arXiv / Glassnode), open **`/sources`** and add a source. The exact
   **URL / Identifier** format for every source type — and which ones accept an
   author's home/profile URL vs. require a feed URL — is documented in
   **[docs/ingest-sources.md](docs/ingest-sources.md)**.

---

## End-to-end integration test

A single script verifies the full stack without touching the browser:

```bash
# requires ANTHROPIC_API_KEY in env
python backend/test_pipeline.py
```

What it checks:

1. **Pre-flight** — refuses to run with a missing or placeholder API key.
2. Triggers `WorkflowOrchestrator.run_full_pipeline` on a seed string.
3. Asserts a new `KnowledgeNode` row is written to SQLite.
4. Asserts the Coder agent's output executes inside the sandbox without
   tracebacks (i.e. `def compute_factor` survives validation + exec).
5. Asserts an equity-curve report lands in `storage/results/strategy_<id>.json`.
6. Prints a final ASCII status grid.

Exit code is 0 on success, 1 on any failure.

---

## REST endpoints (selected)

| Method | Path                                    | Purpose                                       |
| ------ | --------------------------------------- | --------------------------------------------- |
| GET    | `/api/health`                           | liveness + reports whether `ANTHROPIC_API_KEY` is set |
| POST   | `/api/intake`                           | `{raw_text}` → new `KnowledgeNode`            |
| POST   | `/api/research`                         | `{node_ids:[...]}` → unified Alpha Story      |
| POST   | `/api/pipeline/run`                     | Kicks off full pipeline in background         |
| GET    | `/api/pipeline/status/{strategy_id}`    | Live status + terminal log lines              |
| GET    | `/api/knowledge` / `/api/strategies`    | List views (consumed by graph + workspace)    |
| GET    | `/api/strategies/{id}`                  | Strategy detail incl. equity curve            |
| POST   | `/api/portfolio/allocate`               | `{strategy_ids:[...]}` → inverse-vol weights  |
| GET    | `/api/graph`                            | Nodes + edges payload for vis-network         |
| GET    | `/api/v2/pipeline/buckets`              | Stage 0-5 + Graveyard, merges SC + LIVE into Stage 5 with sub-pills |
| GET    | `/api/agent-dialogue?strategy_id=&limit=` | agent self-dialogue transcript (handoff / critique / approval / veto) |
| GET    | `/api/cointegration/pairs?lookback_days=&p_threshold=&refresh=` | Engle-Granger pair scan over a multi-asset synthetic universe |
| GET    | `/api/knowledge/{id}/postmortems`       | reverse-link from a KnowledgeNode to every postmortem that descends from it |
| GET    | `/api/alpha-lab/suggested-topics?limit=` | IC-ranked KB concepts + seed fallback for the Alpha Lab rail |
| GET    | `/api/telegram/recent-intakes?limit=`   | recent /intake submissions via Telegram |
| GET    | `/api/discord/status` + `/api/discord/recent-intakes` | Discord inbound bot snapshot (off unless `DISCORD_INBOUND_ENABLED=1`) |

OpenAPI playground: <http://127.0.0.1:8000/docs>

### Release notes

Per-milestone changes (including the P8 reference-alignment batch) are recorded
in [CHANGELOG.md](./CHANGELOG.md).

---

## Project layout

```
ai-alpha-system-true/
├── backend/
│   ├── app/main.py                 # FastAPI app + route registration
│   ├── agents/                     # intake / researcher / coder / critic
│   ├── core/
│   │   ├── database.py             # SQLAlchemy models + session helpers
│   │   ├── data_gen.py             # synthetic 2yr hourly BTC dataset
│   │   ├── engine.py               # AlphaBacktester (lookahead-safe)
│   │   ├── sandbox.py              # AST-whitelisted factor exec sandbox
│   │   ├── orchestrator.py         # state machine + retry loop
│   │   └── portfolio.py            # risk-parity allocator
│   ├── data/synthetic_btc.csv      # produced by data_gen.py
│   └── test_pipeline.py            # E2E integration test
├── frontend/
│   ├── app/                        # Next.js App Router pages
│   ├── components/                 # HeaderBar, KnowledgeGraph, Stepper, …
│   └── lib/api.ts                  # typed API client for the backend
├── storage/
│   ├── knowledge/                  # extracted markdown KnowledgeNodes
│   └── results/                    # strategy_<id>.json equity reports
├── requirements.txt
├── start.sh / start.ps1            # one-command launchers w/ cleanup trap
├── .env.example
└── README.md
```

---

## Design notes worth knowing

- **Look-ahead bias guard** — `engine.py` shifts the signal series forward by
  exactly one bar before multiplying with returns. Combined with the
  AST-whitelisted sandbox (forbids `.shift(-1)`, `iloc[i+1]`, future bars),
  this rules out cheating both at the factor level and the backtester level.
- **Cost model** — every position change pays `fee + slippage = 0.07%` per
  side, scaled by `|Δposition|`. A simple long↔flat round trip therefore
  pays 0.14% over the cycle, matching realistic crypto perp execution.
- **Sharpe annualization** — hourly variance × `√8760`; written explicitly
  in `engine.py` so quants can audit the constant.
- **Critic hard guardrails** — even if the LLM votes "Go", the Critic
  agent overrides with a hard No-Go when Sharpe < 0.5, MaxDD < −35%,
  trades < 20, or profit_factor < 1.05. The Critic and Coder then get
  exactly one retry round before the strategy is marked REJECTED.
- **Sandbox isolation** — generated code runs through a static AST whitelist
  (only `pandas` + `numpy` + a tiny set of safe builtins) **and** under an
  8-second cross-platform threaded watchdog. `os`, `sys`, `subprocess`,
  `open`, `__class__`, attribute-introspection, and any unlisted import are
  rejected before `exec()` ever sees the bytecode.
- **Portfolio allocator** — inverse-vol weights with an `ε=1e-6` floor.
  Strategies with fewer than 3 trades are filtered to 0%. Surviving weights
  re-normalize to exactly 1.0.

---

## Troubleshooting

| Symptom                                                             | Fix                                                                               |
| ------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| `ANTHROPIC_API_KEY is not set` from any agent                       | Export the key in your shell or put it in `.env`. If you'd rather use OpenRouter, set `LLM_PROVIDER=openrouter` + `OPENROUTER_API_KEY`. |
| UI shows yellow `LLM not configured` banner and `+ INGEST RAW SOURCE` is amber instead of green | The backend cannot see your key. Check `<http://127.0.0.1:8000/api/health>` to confirm `llm.configured`. Common causes: `.env` was edited after the backend started (just Ctrl+C the launcher and rerun), or the key var name in `.env` doesn't match `LLM_PROVIDER` (openrouter needs `OPENROUTER_API_KEY`, not `ANTHROPIC_API_KEY`). |
| `OPENROUTER HTTP 401 ...`                                           | The key is wrong, expired, or the chosen `OPENROUTER_MODEL` is gated. Double-check on <https://openrouter.ai/models>. |
| `Unknown LLM_PROVIDER=...`                                          | Must be one of `anthropic`, `openrouter`, or `auto` (case-insensitive).           |
| `synthetic_btc.csv` not found when running the orchestrator         | `python backend/core/data_gen.py`                                                 |
| `npm install` errors with `EPERM` on Windows                        | Close any IDE/explorer windows pinned to `frontend/node_modules`, then retry.     |
| Frontend `prose` classes not styled                                 | Confirm `@tailwindcss/typography` installed (it's already in `package.json`).     |
| `Sandbox execution exceeded …s wall clock`                          | The Coder produced an O(n²) factor. Re-run; the retry loop usually fixes it.      |

---

## Disclaimer & risk notice

This project is published for **research and educational purposes only**. It is
**not** financial, investment, trading, or legal advice, and nothing in it is a
recommendation to buy, sell, or hold any asset.

- **No warranty.** The software is provided "AS IS", without warranty of any
  kind, express or implied. See the [LICENSE](./LICENSE).
- **No liability.** In no event shall the authors or contributors be liable for
  any claim, loss, or damage — including, without limitation, financial loss
  arising from trading — connected with the software or its use.
- **Trading risk.** Automated trading, and crypto / leveraged perpetuals in
  particular, carries substantial risk up to and including **total loss of
  capital**. Backtested or simulated results do **not** guarantee future
  performance.
- **Live trading is off by default.** The system ships in backtest / paper
  mode; live order routing sits behind explicit environment gates
  (`LIVE_TRADE_ENABLED`, `LIVE_TRADE_PAGE_ENABLED`, both `0` by default).
  Enabling real-money trading, and every consequence of doing so, is **solely
  your responsibility**.
- **No affiliation.** This project is not affiliated with, endorsed by, or
  sponsored by any exchange, broker, data provider, or model vendor. You are
  responsible for complying with the terms of every third-party API you use and
  with all laws and regulations that apply to you.

## License

Released under the [MIT License](./LICENSE) — © 2026 Starlight143.
