# Security Policy

## Supported versions

| Version | Supported |
| ------- | --------- |
| 1.0.x   | ✅        |

## Reporting a vulnerability

**Please do not open public issues for security problems.**

Report privately through GitHub: open the repository's **Security** tab →
**Report a vulnerability** (GitHub Private Vulnerability Reporting). That opens a
private thread with the maintainer. Include reproduction steps, affected files,
and the impact you observed. We aim to acknowledge reports within a few days.

## Scope & safety model

This is **research / educational** software that is capable of placing trades.
Some context before you assess risk:

- **Live trading is off by default.** Real order routing is gated behind
  `LIVE_TRADE_ENABLED` / `LIVE_TRADE_PAGE_ENABLED` (both `0`); out of the box the
  system runs in backtest / paper mode only.
- **LLM-generated code is sandboxed.** Factor code runs through an AST whitelist
  (pandas / numpy + a small set of safe builtins) under a wall-clock watchdog;
  `os`, `sys`, `subprocess`, `open`, imports, and attribute introspection are
  rejected before execution.
- **Secrets live only in `.env`** (git-ignored). Never commit real API keys —
  `.env.example` ships placeholders only.
- **Untrusted input.** External data (news / RSS / financials / API responses)
  is treated as untrusted and must not be acted on as instructions.

Findings that most interest us: sandbox escapes, provider-key leakage,
paper→live gate bypasses, and order-path idempotency / duplicate-submission
issues.
