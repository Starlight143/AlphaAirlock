// Pure-function client-side derivations. All charts in P1 read from the
// existing `equity_curve` (daily resampled by the backtester) and compute
// distributions / rolling stats locally. This avoids a second roundtrip and
// keeps the backend stable.
//
// All helpers tolerate empty input and return empty arrays — never throw.

import type { EquityPoint, TradeTapeRow } from './api';

export type Histogram = {
  bins: { x: number; count: number; label: string }[];
  totalCount: number;
};

/** Daily equity → daily simple return series (length N-1). */
export function dailyReturns(curve: EquityPoint[]): number[] {
  if (!curve || curve.length < 2) return [];
  // A non-finite or non-positive SEED equity makes the whole base-relative
  // return series untrustworthy (mirrors the recoveryTimes seed guard).
  // Return [] (the file's 'no data' contract) so charts show an explicit
  // empty state instead of an artificially calm all-zero distribution.
  const seed = Number(curve[0].equity);
  if (!(seed > 1e-14)) return [];
  const out: number[] = [];
  for (let i = 1; i < curve.length; i++) {
    const prev = curve[i - 1].equity;
    const cur = curve[i].equity;
    // Skip degenerate bars (data gap / non-finite / ~0 prior equity) instead
    // of booking a fabricated 0% day — a 0 is finite and slips past every
    // consumer's Number.isFinite filter, deflating volatility and biasing
    // Sharpe / skew / kurtosis / percentile estimates toward 0. Matches the
    // skip behaviour already used by monthlyReturns / yearlyReturns /
    // alignReturns in this module. Also fixes the per-1e-14 guard to the
    // project standard `!(prev > 1e-14)` (catches subnormals).
    if (!Number.isFinite(prev) || !Number.isFinite(cur) || !(prev > 1e-14)) {
      continue;
    }
    out.push(cur / prev - 1);
  }
  return out;
}

/**
 * Equally-spaced histogram bins. `binCount` defaults to 30.
 *
 * `integerLabels` (default false): when true, the X-axis bucket label is
 * rendered as a rounded whole number via Math.round(x). Use this for
 * integer-domain metrics (e.g. recovery-time in days) so the axis does not
 * show nonsensical fractional buckets like '2.50'. Binning math and the
 * numeric `x` field are unchanged — only the display label differs.
 */
export function histogram(
  values: number[],
  binCount = 30,
  integerLabels = false,
): Histogram {
  const finite = values.filter((v) => Number.isFinite(v));
  if (finite.length === 0) {
    return { bins: [], totalCount: 0 };
  }
  let lo = Math.min(...finite);
  let hi = Math.max(...finite);
  if (lo === hi) {
    // Single-value series — give it a tiny window so the chart isn't empty.
    lo -= 1e-9;
    hi += 1e-9;
  }
  const step = (hi - lo) / binCount;
  // Label precision must be adaptive to the bin STEP, not just |x|. For
  // tightly-clustered series (e.g. a low-vol daily-return / shallow-drawdown
  // distribution) the previous fixed toFixed(3)/toFixed(2) collapsed several
  // adjacent bin centers to the SAME string, so the XAxis showed repeated
  // ticks and the Recharts tooltip (which reads this same `label`) could not
  // distinguish which bin was hovered. We take the MAX of the original
  // precision and a step-derived precision (digits such that 10^-digits <=
  // step, plus one safety digit) so adjacent centers always differ. Using
  // max() keeps existing well-spaced charts byte-identical (additive change).
  const stepDigits =
    step > 0 ? Math.ceil(-Math.log10(step)) + 1 : 3;
  const bins = new Array(binCount).fill(0).map((_, i) => {
    const x = lo + step * (i + 0.5);
    const baseDigits = Math.abs(x) < 1 ? 3 : 2;
    const digits = Math.min(10, Math.max(baseDigits, stepDigits));
    return {
      x,
      count: 0,
      label: integerLabels
        ? String(Math.round(x))
        : x.toFixed(digits),
    };
  });
  for (const v of finite) {
    let idx = Math.floor((v - lo) / step);
    if (idx === binCount) idx = binCount - 1; // edge case: max value
    if (idx < 0 || idx >= binCount) continue;
    bins[idx].count++;
  }
  return { bins, totalCount: finite.length };
}

/**
 * Rolling Sharpe over the daily-return series.
 * `window` defaults to 30 trading days; annualized by sqrt(252).
 *
 * NOTE: this is a daily approximation — the backtester reports
 * sqrt(8760) hourly Sharpe. The rolling chart on the strategy detail page
 * is a UX signal, not a contract metric.
 */
export function rollingSharpe(
  returns: number[],
  window = 30,
): { i: number; sharpe: number }[] {
  if (returns.length < window) return [];
  const out: { i: number; sharpe: number }[] = [];
  const sqrt252 = Math.sqrt(252);
  for (let i = window - 1; i < returns.length; i++) {
    const slice = returns.slice(i - window + 1, i + 1);
    const mean = slice.reduce((a, b) => a + b, 0) / window;
    const variance =
      slice.reduce((a, b) => a + (b - mean) * (b - mean), 0) / Math.max(1, window - 1);
    const std = Math.sqrt(variance);
    if (!(std > 1e-14)) {
      out.push({ i, sharpe: 0 });
      continue;
    }
    out.push({ i, sharpe: (mean / std) * sqrt252 });
  }
  return out;
}

/**
 * Histogram of recovery times — number of days from each drawdown trough
 * back to a new all-time-high equity. Uses the daily equity series so
 * `recovery_time` is expressed in days.
 *
 * A trough is the lowest equity point inside a contiguous drawdown segment
 * (i.e. between two consecutive new-high points). If the series ends in an
 * unrecovered drawdown, that segment is excluded from the histogram.
 */
export function recoveryTimes(curve: EquityPoint[]): number[] {
  if (!curve || curve.length < 2) return [];
  const out: number[] = [];
  // P34: bail if the seed equity is non-finite, zero, or subnormal — an NaN
  // runMax makes every ``eq < runMax`` comparison false and silently yields
  // zero recovery events; a zero/subnormal seed lets the very first real bar
  // pass the ``eq > runMax`` branch (updating runMax) or produces inconsistent
  // recovery arrays when dailyReturns() returns [] for the same curve (since
  // dailyReturns uses the project-standard !(seed > 1e-14) guard). Aligning
  // both functions' no-data contract prevents a contradictory UI state where
  // the recovery-time histogram is non-empty while the returns histogram is
  // empty for the same EquityPoint array.
  const firstEq = Number(curve[0].equity);
  if (!(firstEq > 1e-14)) return [];
  let runMax = firstEq;
  let troughEq = runMax;
  let troughIdx = 0;
  let inDrawdown = false;

  for (let i = 1; i < curve.length; i++) {
    const eq = curve[i].equity;
    if (!Number.isFinite(eq)) continue;
    if (!inDrawdown && eq < runMax) {
      inDrawdown = true;
      troughEq = eq;
      troughIdx = i;
    } else if (inDrawdown) {
      if (eq < troughEq) {
        troughEq = eq;
        troughIdx = i;
      }
      if (eq >= runMax - 1e-12) {
        // recovered
        out.push(i - troughIdx);
        inDrawdown = false;
        runMax = eq;
      }
    } else if (eq > runMax) {
      runMax = eq;
    }
  }
  return out;
}

/** Convenience: pull the drawdown column out as a plain array of negatives. */
export function drawdownSeries(curve: EquityPoint[]): number[] {
  return (curve ?? []).map((p) => Number(p.drawdown)).filter(Number.isFinite);
}

// ---------------------------------------------------------------------------
// P5-FE-11 — month / year aggregations + statistical moments
// ---------------------------------------------------------------------------

export type MonthlyReturn = {
  year: number;
  month: number; // 1..12
  ret: number;   // simple return for the month, as decimal
};

/**
 * Group the daily equity curve into per-month returns. Month return is
 * computed as `lastEquity / firstEquity - 1` over each calendar month.
 * Months with <2 datapoints are skipped.
 */
export function monthlyReturns(curve: EquityPoint[]): MonthlyReturn[] {
  if (!curve || curve.length < 2) return [];
  // Bucket each calendar month's LAST equity (month-end close), in time order.
  // Mirrors backend factor_evaluator._monthly_returns_from_equity:
  //   monthly_eq = s.resample('1ME').last(); monthly_eq.pct_change().
  // Each month's return is monthEndClose / priorMonthEndClose - 1, so the cells
  // compound back to the full-period equity ratio. The very first month has no
  // prior close and is dropped (== pandas pct_change().dropna()).
  const buckets = new Map<string, { last: number; year: number; month: number }>();
  for (const p of curve) {
    if (!p?.timestamp || !Number.isFinite(p.equity)) continue;
    const date = new Date(p.timestamp);
    if (isNaN(date.getTime())) continue;
    const y = date.getUTCFullYear();
    const m = date.getUTCMonth() + 1;
    const key = `${y}-${m.toString().padStart(2, '0')}`;
    // curve is already time-ordered by the backtester; last write wins = close.
    buckets.set(key, { last: p.equity, year: y, month: m });
  }
  const ordered = Array.from(buckets.values()).sort(
    (a, b) => a.year - b.year || a.month - b.month,
  );
  const out: MonthlyReturn[] = [];
  for (let i = 1; i < ordered.length; i++) {
    const prevClose = ordered[i - 1].last;
    const cur = ordered[i];
    if (!(prevClose > 1e-14) || !Number.isFinite(cur.last)) continue;
    out.push({ year: cur.year, month: cur.month, ret: cur.last / prevClose - 1 });
  }
  return out;
}

export type YearlyReturn = { year: number; ret: number };

export function yearlyReturns(curve: EquityPoint[]): YearlyReturn[] {
  if (!curve || curve.length < 2) return [];
  // Bucket each year's LAST equity (year-end close), in time order; year return
  // is yearEndClose / priorYearEndClose - 1 so the cells compound to the
  // full-period equity ratio. First year has no prior close and is dropped.
  const buckets = new Map<number, number>(); // year -> last (close) equity
  for (const p of curve) {
    if (!p?.timestamp || !Number.isFinite(p.equity)) continue;
    const date = new Date(p.timestamp);
    if (isNaN(date.getTime())) continue;
    const y = date.getUTCFullYear();
    buckets.set(y, p.equity); // last write wins = year-end close
  }
  const ordered = Array.from(buckets.entries()).sort((a, b) => a[0] - b[0]);
  const out: YearlyReturn[] = [];
  for (let i = 1; i < ordered.length; i++) {
    const prevClose = ordered[i - 1][1];
    const [year, close] = ordered[i];
    if (!(prevClose > 1e-14) || !Number.isFinite(close)) continue;
    out.push({ year, ret: close / prevClose - 1 });
  }
  return out;
}

/** Fraction of months with positive return. */
export function monthlyWinRate(curve: EquityPoint[]): number | null {
  const months = monthlyReturns(curve);
  if (months.length === 0) return null;
  const wins = months.filter((m) => m.ret > 0).length;
  return wins / months.length;
}

/** Population skewness (Fisher-Pearson) over the supplied array. */
export function skewness(values: number[]): number | null {
  const xs = values.filter((v) => Number.isFinite(v));
  const n = xs.length;
  if (n < 3) return null;
  const mean = xs.reduce((a, b) => a + b, 0) / n;
  const m2 = xs.reduce((a, b) => a + (b - mean) * (b - mean), 0) / n;
  const m3 = xs.reduce((a, b) => a + (b - mean) ** 3, 0) / n;
  const std = Math.sqrt(m2);
  if (!(std > 1e-14)) return null;
  return m3 / (std * std * std);
}

/** Excess kurtosis (population — subtract 3 from raw kurtosis). */
export function kurtosis(values: number[]): number | null {
  const xs = values.filter((v) => Number.isFinite(v));
  const n = xs.length;
  if (n < 4) return null;
  const mean = xs.reduce((a, b) => a + b, 0) / n;
  const m2 = xs.reduce((a, b) => a + (b - mean) * (b - mean), 0) / n;
  const m4 = xs.reduce((a, b) => a + (b - mean) ** 4, 0) / n;
  if (!(m2 > 1e-14)) return null;
  return m4 / (m2 * m2) - 3;
}

/**
 * Lag-k autocorrelation. Returns the value at the requested lag (null when
 * the series is too short or the denominator collapses).
 */
export function autocorrelation(values: number[], lag = 1): number | null {
  const xs = values.filter((v) => Number.isFinite(v));
  const n = xs.length;
  if (n < lag + 2) return null;
  const mean = xs.reduce((a, b) => a + b, 0) / n;
  let num = 0;
  let den = 0;
  for (let i = 0; i < n; i++) {
    const dx = xs[i] - mean;
    den += dx * dx;
    if (i >= lag) num += dx * (xs[i - lag] - mean);
  }
  if (!(den > 1e-14)) return null;
  // Clamp per CLAUDE.md correctness rule for Pearson-derived statistics.
  return Math.max(-1, Math.min(1, num / den));
}

/** Highest absolute autocorrelation across lags 1..maxLag, signed. */
export function bestAutocorrelation(
  values: number[],
  maxLag = 10,
): { lag: number; value: number } | null {
  let best: { lag: number; value: number } | null = null;
  for (let lag = 1; lag <= maxLag; lag++) {
    const v = autocorrelation(values, lag);
    if (v == null) continue;
    if (!best || Math.abs(v) > Math.abs(best.value)) {
      best = { lag, value: v };
    }
  }
  return best;
}

// ---------------------------------------------------------------------------
// P5 — strategy quality label (matches reference's [PROVEN SR-x.x] tags)
// ---------------------------------------------------------------------------

export type QualityTier = 'PROVEN' | 'STRONG' | 'CANDIDATE' | 'EXPERIMENTAL' | 'UNKNOWN';

export type QualityLabel = {
  tier: QualityTier;
  text: string;        // e.g. "PROVEN SR-1.5"
  tone: 'emerald' | 'cyan' | 'amber' | 'slate' | 'muted';
};

/**
 * Classify a strategy by its annualized Sharpe — mirrors the reference UI's
 * [PROVEN SR-x.x] chips on the Alpha Ideas / Stage drill-down modal.
 * Thresholds align with `backend.core.thresholds.MIN_SHARPE` (>=0.5 → at
 * least CANDIDATE).
 */
export function qualityLabel(sharpe: number | null | undefined): QualityLabel {
  if (sharpe == null || !Number.isFinite(sharpe)) {
    return { tier: 'UNKNOWN', text: 'NO METRICS', tone: 'muted' };
  }
  const formatted = `SR-${sharpe.toFixed(1)}`;
  if (sharpe >= 1.5) return { tier: 'PROVEN', text: `PROVEN ${formatted}`, tone: 'emerald' };
  if (sharpe >= 1.0) return { tier: 'STRONG', text: `STRONG ${formatted}`, tone: 'cyan' };
  if (sharpe >= 0.5) return { tier: 'CANDIDATE', text: `CANDIDATE ${formatted}`, tone: 'amber' };
  return { tier: 'EXPERIMENTAL', text: `EXPERIMENTAL ${formatted}`, tone: 'slate' };
}

/** Tailwind class shorthand for the quality-label chip background/text. */
export function qualityLabelClasses(tone: QualityLabel['tone']): string {
  switch (tone) {
    case 'emerald':
      return 'border-emerald-700/60 bg-emerald-500/10 text-emerald-300';
    case 'cyan':
      return 'border-cyan-700/60 bg-cyan-500/10 text-cyan-300';
    case 'amber':
      return 'border-amber-700/60 bg-amber-500/10 text-amber-300';
    case 'slate':
      return 'border-slate-700 bg-slate-900 text-slate-400';
    case 'muted':
    default:
      return 'border-slate-800 bg-slate-950 text-slate-600';
  }
}


// ---------------------------------------------------------------------------
// P6-A7 — Trade-level helpers (DetailedMetricsTable)
// ---------------------------------------------------------------------------
//
// A "trade" is a contiguous run of same-sign signal bars (non-zero). Flat
// bars (signal == 0) separate trades. Each trade's PnL is the COMPOUNDED return
// over its lifespan — product(1 + bar pnl_pct) - 1 — matching the backend's
// equity = (1+net).cumprod(); per-bar pnl_pct already includes fees + slippage.

export type TradeStat = {
  signal: 1 | -1;
  bars: number;
  pnlPct: number;
  startTime: string;
  endTime: string;
};


/**
 * Walk the per-bar tape, emitting one TradeStat per contiguous same-sign run.
 * Returns [] for empty or all-zero tapes.
 */
export function extractTrades(tape: TradeTapeRow[]): TradeStat[] {
  if (!tape || tape.length === 0) return [];
  const out: TradeStat[] = [];
  let active: TradeStat | null = null;
  // Running compounded growth factor for the active trade: product of (1+rᵢ)
  // over its bars. pnlPct is kept in sync (= growth - 1) on every bar so it is
  // correct at all three push sites. This is the true multi-bar return; a plain
  // sum of per-bar pnl_pct (simple returns) would diverge from the backend's
  // compounded equity = (1+net).cumprod() (engine.py:123).
  let growth = 1;
  for (const row of tape) {
    const sig = Math.sign(Number(row.signal) || 0);
    const pnl = Number(row.pnl_pct) || 0;
    const ts = row.start_time || '';
    if (sig === 0) {
      if (active) {
        out.push(active);
        active = null;
      }
      continue;
    }
    if (!active || active.signal !== sig) {
      if (active) out.push(active);
      growth = 1 + pnl;
      active = { signal: sig === 1 ? 1 : -1, bars: 1, pnlPct: growth - 1, startTime: ts, endTime: ts };
    } else {
      active.bars += 1;
      growth *= 1 + pnl;
      active.pnlPct = growth - 1;
      active.endTime = ts;
    }
  }
  if (active) out.push(active);
  return out;
}


export function avgWin(trades: TradeStat[]): number | null {
  const wins = trades.filter((t) => t.pnlPct > 0);
  if (wins.length === 0) return null;
  return wins.reduce((a, t) => a + t.pnlPct, 0) / wins.length;
}


export function avgLoss(trades: TradeStat[]): number | null {
  const losses = trades.filter((t) => t.pnlPct < 0);
  if (losses.length === 0) return null;
  return losses.reduce((a, t) => a + t.pnlPct, 0) / losses.length;
}


export function avgHoldingBars(trades: TradeStat[]): number | null {
  if (trades.length === 0) return null;
  const total = trades.reduce((a, t) => a + t.bars, 0);
  return total / trades.length;
}


export function maxHoldingBars(trades: TradeStat[]): number | null {
  if (trades.length === 0) return null;
  return trades.reduce((a, t) => Math.max(a, t.bars), 0);
}


export function longShortCounts(trades: TradeStat[]): { long: number; short: number } {
  let long = 0;
  let short = 0;
  for (const t of trades) {
    if (t.signal > 0) long++;
    else if (t.signal < 0) short++;
  }
  return { long, short };
}


export function longShortRatio(trades: TradeStat[]): number | null {
  const { long, short } = longShortCounts(trades);
  if (short === 0) return long > 0 ? Infinity : null;
  return long / short;
}


/**
 * Turnover proxy: sum of |delta-signal| over the tape, divided by the
 * number of bars (yields turnover per bar; multiply by bars/day for daily).
 * Returns null on empty or single-bar tapes.
 */
export function turnoverPerBar(tape: TradeTapeRow[]): number | null {
  if (!tape || tape.length < 2) return null;
  let total = 0;
  for (let i = 1; i < tape.length; i++) {
    total += Math.abs((Number(tape[i].signal) || 0) - (Number(tape[i - 1].signal) || 0));
  }
  return total / tape.length;
}


export function winningStreak(trades: TradeStat[]): number {
  let best = 0;
  let cur = 0;
  for (const t of trades) {
    if (t.pnlPct > 0) {
      cur++;
      if (cur > best) best = cur;
    } else {
      cur = 0;
    }
  }
  return best;
}


export function largestWin(trades: TradeStat[]): number | null {
  const wins = trades.filter((t) => t.pnlPct > 0);
  if (wins.length === 0) return null;
  return wins.reduce((a, t) => Math.max(a, t.pnlPct), 0);
}


export function largestLoss(trades: TradeStat[]): number | null {
  const losses = trades.filter((t) => t.pnlPct < 0);
  if (losses.length === 0) return null;
  return losses.reduce((a, t) => Math.min(a, t.pnlPct), 0);
}


export function winTradesCount(trades: TradeStat[]): number {
  return trades.filter((t) => t.pnlPct > 0).length;
}


export function loseTradesCount(trades: TradeStat[]): number {
  return trades.filter((t) => t.pnlPct < 0).length;
}


// ---------------------------------------------------------------------------
// P7-04 — /arena head-to-head helpers
// ---------------------------------------------------------------------------

export type AlignedReturns = {
  timestamps: string[];
  retA: number[];
  retB: number[];
};


/** Inner-join two equity curves on the ISO date prefix (YYYY-MM-DD), then derive returns. */
export function alignReturns(a: EquityPoint[], b: EquityPoint[]): AlignedReturns {
  if (!a?.length || !b?.length) return { timestamps: [], retA: [], retB: [] };
  const dateOf = (ts: string) => ts.slice(0, 10);
  const mapA = new Map<string, number>();
  for (const p of a) mapA.set(dateOf(p.timestamp), p.equity);
  const overlap: { ts: string; ea: number; eb: number }[] = [];
  for (const p of b) {
    const d = dateOf(p.timestamp);
    const ea = mapA.get(d);
    if (ea !== undefined) overlap.push({ ts: d, ea, eb: p.equity });
  }
  overlap.sort((x, y) => x.ts.localeCompare(y.ts));
  const timestamps: string[] = [];
  const retA: number[] = [];
  const retB: number[] = [];
  for (let i = 1; i < overlap.length; i++) {
    const prev = overlap[i - 1];
    const cur = overlap[i];
    if (prev.ea > 1e-14 && prev.eb > 1e-14) {
      timestamps.push(cur.ts);
      retA.push(cur.ea / prev.ea - 1);
      retB.push(cur.eb / prev.eb - 1);
    }
  }
  return { timestamps, retA, retB };
}


export function pearsonCorrelation(x: number[], y: number[]): number | null {
  const n = Math.min(x.length, y.length);
  if (n < 2) return null;
  let sx = 0, sy = 0, sxy = 0, sxx = 0, syy = 0;
  for (let i = 0; i < n; i++) {
    sx += x[i]; sy += y[i];
    sxy += x[i] * y[i];
    sxx += x[i] * x[i]; syy += y[i] * y[i];
  }
  const denom = Math.sqrt((n * sxx - sx * sx) * (n * syy - sy * sy));
  if (!(denom > 1e-14)) return null;
  const r = (n * sxy - sx * sy) / denom;
  return Math.max(-1, Math.min(1, r));
}


export function rollingCorrelation(
  x: number[],
  y: number[],
  window = 30,
): { i: number; r: number | null }[] {
  const out: { i: number; r: number | null }[] = [];
  for (let i = 0; i + window <= x.length; i++) {
    out.push({ i: i + window - 1, r: pearsonCorrelation(x.slice(i, i + window), y.slice(i, i + window)) });
  }
  return out;
}


function _std(arr: number[]): number {
  if (arr.length < 2) return 0;
  const m = arr.reduce((a, b) => a + b, 0) / arr.length;
  const v = arr.reduce((a, b) => a + (b - m) * (b - m), 0) / (arr.length - 1);
  return Math.sqrt(v);
}


/** Equal-weight combined Sharpe given two return series + correlation (more
 * general than the equal-vol shortcut: derives from the actual cov matrix). */
export function combinedSharpe(retA: number[], retB: number[]): number | null {
  const n = Math.min(retA.length, retB.length);
  if (n < 2) return null;
  const wA = 0.5, wB = 0.5;
  const meanA = retA.reduce((a, b) => a + b, 0) / n;
  const meanB = retB.reduce((a, b) => a + b, 0) / n;
  const sA = _std(retA), sB = _std(retB);
  const corr = pearsonCorrelation(retA, retB);
  if (corr == null) return null;
  const portMean = wA * meanA + wB * meanB;
  const portVar = wA * wA * sA * sA + wB * wB * sB * sB + 2 * wA * wB * sA * sB * corr;
  if (!(portVar > 1e-14)) return null;
  return Math.max(-25, Math.min(25, (portMean / Math.sqrt(portVar)) * Math.sqrt(252)));
}


export function trackingError(retA: number[], retB: number[]): number | null {
  const n = Math.min(retA.length, retB.length);
  if (n < 2) return null;
  const diff = Array.from({ length: n }, (_, i) => retA[i] - retB[i]);
  const sd = _std(diff);
  if (!(sd > 1e-14)) return null;
  return sd * Math.sqrt(252);
}


export function diversificationBenefit(retA: number[], retB: number[]): number | null {
  const sA = _std(retA), sB = _std(retB);
  if (!(sA > 1e-14) || !(sB > 1e-14)) return null;
  const corr = pearsonCorrelation(retA, retB);
  if (corr == null) return null;
  const wA = 0.5, wB = 0.5;
  const portVar = wA * wA * sA * sA + wB * wB * sB * sB + 2 * wA * wB * sA * sB * corr;
  if (!(portVar > 1e-14)) return null;
  const weighted = wA * sA + wB * sB;
  if (!(weighted > 1e-14)) return null;
  return 1 - Math.sqrt(portVar) / weighted;
}


export function normalizeToBase100(curve: EquityPoint[]): { ts: string; v: number }[] {
  if (!curve?.length) return [];
  const base = curve[0].equity;
  // A non-finite or non-positive base cannot be normalised. Return [] (the
  // file's 'no data' contract) so the series drops out / renders an explicit
  // empty state, rather than masquerading as a flat break-even (v=100) line.
  if (!(base > 1e-14)) return [];
  return curve.map((p) => ({ ts: p.timestamp, v: (p.equity / base) * 100 }));
}


// ---------------------------------------------------------------------------
// C-M8 — centralised trades-count lookup.
//
// Three sites (PerformanceGrid, StrategyTable, KpiStrip) previously fell
// back between `raw_backtest.trades` and `config.trades` in inconsistent
// order. Canonical order: raw_backtest first (engine-truth), then config
// (operator/manual override), then null when both are missing or non-finite.
// ---------------------------------------------------------------------------

import type { AlphaStrategy } from './api';

export function getTradesCount(s: AlphaStrategy | null | undefined): number | null {
  if (!s) return null;
  const fromRaw = Number((s.raw_backtest as { trades?: unknown } | undefined)?.trades);
  if (Number.isFinite(fromRaw)) return fromRaw;
  const fromConfig = Number((s.config as { trades?: unknown } | undefined)?.trades);
  if (Number.isFinite(fromConfig)) return fromConfig;
  return null;
}

// ---------------------------------------------------------------------------
// C-H8 — centralised PASS/FAIL/PENDING classifier for the critic verdict
// chips. VerdictBanner (CriticVerdict.tsx) and VerdictBadge
// (StrategyDetail.tsx) previously kept their own copies of the bucket
// membership; both now consume this single source of truth.
// ---------------------------------------------------------------------------

export type VerdictKind = 'PASS' | 'FAIL' | 'PENDING';

const VERDICT_PASS_STATUSES = new Set([
  'APPROVED',
  'LIVE',
  'PAPER_TRADE',
  'SMALL_CAPITAL',
]);
const VERDICT_FAIL_STATUSES = new Set(['REJECTED', 'GRAVEYARD', 'PAUSED']);

export function classifyVerdict(status: string | null | undefined): VerdictKind {
  const upper = (status || '').toUpperCase();
  if (VERDICT_PASS_STATUSES.has(upper)) return 'PASS';
  if (VERDICT_FAIL_STATUSES.has(upper)) return 'FAIL';
  return 'PENDING';
}

/**
 * C-M10 — typed reader for the merged-pipeline flag on AlphaStrategy.config.
 * Centralises the `(config as any)?.is_merged` cast so the three callsites
 * in backtest-panel/page.tsx share one shape.
 */
export function getIsMerged(s: AlphaStrategy): boolean {
  return (s.config as { is_merged?: unknown } | undefined)?.is_merged === true;
}

/**
 * C-L3 — drawdown tone classifier shared by StrategyTable / KpiStrip /
 * PerformanceGrid so the threshold ladder (-0.15 emerald, -0.30 amber)
 * lives in exactly one place.
 */
export type MetricTone = 'emerald' | 'amber' | 'rose' | 'slate';
export function ddTone(v: number | null | undefined): MetricTone {
  if (v == null || !Number.isFinite(v)) return 'slate';
  if (v >= -0.15) return 'emerald';
  if (v >= -0.30) return 'amber';
  return 'rose';
}

// ---------------------------------------------------------------------------
// C-H2 / C-H3 / C-H7 / C-M7 — KPI tile tone classifier.
//
// The previous inline pattern `Number(metric) >= good ? 'good' : ... : 'bad'`
// silently classifies NaN / undefined as 'bad' (rose) because the comparison
// returns false in every branch. KPI tiles then read em-dash for value but
// render against a red background — directly contradicting the screenshot
// reference of cyan/neutral for missing metrics.
//
// `metricTone(v, good, neutral)` returns 'muted' for non-finite values, then
// the standard good/neutral/bad ladder. `ddToneKpi`, `cumRetTone` give the
// fixed thresholds those two tiles need so callers don't repeat magic numbers.
// ---------------------------------------------------------------------------

export type KpiTone = 'good' | 'neutral' | 'bad' | 'muted';

export function metricTone(
  v: number | null | undefined,
  good: number,
  neutral: number,
): KpiTone {
  if (v == null || !Number.isFinite(v)) return 'muted';
  if (v >= good) return 'good';
  if (v >= neutral) return 'neutral';
  return 'bad';
}

/** KPI-tone (good/neutral/bad/muted) version of ddTone for tile usage. */
export function ddToneKpi(v: number | null | undefined): KpiTone {
  if (v == null || !Number.isFinite(v)) return 'muted';
  if (v >= -0.15) return 'good';
  if (v >= -0.30) return 'neutral';
  return 'bad';
}

/** Cumulative-return tile tone: positive cumulative return is good, negative bad. */
export function cumRetTone(v: number | null | undefined): KpiTone {
  if (v == null || !Number.isFinite(v)) return 'muted';
  return v > 0 ? 'good' : 'bad';
}

// ---------------------------------------------------------------------------
// C-M14 — explicit pending-reason text for the CriticVerdict PENDING branch.
//
// Previously every non-terminal status rendered "awaiting critic — {STATUS}"
// which is wrong for BACKTESTING / CODE_GEN / INTAKE / STORY_GEN — the critic
// hasn't even been invoked yet at those stages. Map each pipeline phase to a
// faithful human label so the operator understands which agent is in flight.
// ---------------------------------------------------------------------------

export function pendingReason(status: string | null | undefined): string {
  const upper = (status || '').toUpperCase();
  switch (upper) {
    case 'INTAKE':
      return 'awaiting intake';
    case 'STORY_GEN':
      return 'awaiting story generation';
    case 'CODE_GEN':
      return 'awaiting code generation';
    case 'BACKTESTING':
      return 'awaiting backtest';
    case 'CRITIC_LOOP':
      return 'awaiting critic';
    case 'UNKNOWN':
    case '':
      return 'awaiting pipeline';
    default:
      return `awaiting pipeline — ${upper.toLowerCase()}`;
  }
}

// ---------------------------------------------------------------------------
// C-L5 — hoisted TERMINAL_STATES set shared by StrategyDetail / TabsPane.
//
// Both files previously kept their own copy of the same string set; consumers
// now import this single source of truth so the canonical terminal-state
// list cannot drift between locations.
// ---------------------------------------------------------------------------

export const TERMINAL_STATES: ReadonlySet<string> = new Set([
  'APPROVED',
  'REJECTED',
  'GRAVEYARD',
  'LIVE',
  'SMALL_CAPITAL',
  'PAPER_TRADE',
  'PAUSED',
]);

// ---------------------------------------------------------------------------
// P17/H3 — quantile helper. Returns null when there is no finite input.
// Linear interpolation between sorted samples (NIST type-7, matches numpy
// default). Callers responsible for converting to display units.
// ---------------------------------------------------------------------------
export function percentile(values: number[], q: number): number | null {
  const xs = values.filter((v) => Number.isFinite(v)).slice().sort((a, b) => a - b);
  if (xs.length === 0) return null;
  if (xs.length === 1) return xs[0];
  const qC = Math.max(0, Math.min(1, q));
  const idx = qC * (xs.length - 1);
  const lo = Math.floor(idx);
  const hi = Math.ceil(idx);
  if (lo === hi) return xs[lo];
  const frac = idx - lo;
  return xs[lo] * (1 - frac) + xs[hi] * frac;
}
