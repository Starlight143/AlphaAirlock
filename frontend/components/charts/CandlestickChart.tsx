'use client';

/**
 * CandlestickChart — price chart for the Trading Terminal.
 *
 * The trading-terminal backend exposes a polling market-quote endpoint
 * (GET /api/trading-terminal/symbols/:symbol/market) that returns a single
 * TtMarketInfo snapshot: { last, bid, ask, ts, ... }. There is no OHLCV /
 * candle-history endpoint in this API surface.
 *
 * Strategy: the parent page polls that endpoint at 3 s cadence and passes
 * the snapshot stream down. This component accumulates snapshots into a
 * fixed-length rolling window and renders an area chart (last price over
 * time) using the existing Recharts 2.x library — the same library used
 * by every other chart in the project. No new dependencies are added.
 *
 * When a genuine OHLCV endpoint is added in the future, this component
 * also exports the OhlcvBar interface so callers can swap from the price-
 * series path to a candlestick path with a type-safe data shape already in
 * place.
 */

import { useMemo } from 'react';
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
} from 'recharts';

import {
  AXIS_FONT_SIZE,
  AXIS_STROKE,
  CHART_COLORS,
  GRID_STROKE,
  TOOLTIP_STYLE,
} from './_shared';

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/** Full OHLCV bar — reserved for future use when a candle endpoint exists. */
export interface OhlcvBar {
  /** Unix timestamp in milliseconds */
  t: number | string;
  o: number;
  h: number;
  l: number;
  c: number;
  volume?: number;
}

/** One price snapshot as returned by the TtMarketInfo polling endpoint. */
export interface PriceTick {
  /** ISO-8601 timestamp string or Unix-ms number */
  ts: string | number;
  last: number;
  bid?: number;
  ask?: number;
}

interface CandlestickChartProps {
  /**
   * Ordered array of price ticks (oldest → newest). Derived from the page's
   * accumulated TtMarketInfo poll results. May be empty while loading.
   */
  ticks: PriceTick[];
  /**
   * Current mark/last price used to draw a reference line.
   * If undefined the reference line is omitted.
   */
  currentPrice?: number;
  /** Height of the chart area in px. Defaults to 200. */
  height?: number;
}

// ---------------------------------------------------------------------------
// Internal derived row shape
// ---------------------------------------------------------------------------

interface PriceRow {
  /** Formatted time label for the X axis tick */
  label: string;
  /** Raw epoch ms for tooltip label */
  epochMs: number;
  last: number;
  bid: number | null;
  ask: number | null;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function toEpochMs(ts: string | number): number {
  if (typeof ts === 'number') return ts;
  const parsed = Date.parse(ts);
  return Number.isFinite(parsed) ? parsed : 0;
}

function formatTimeLabel(epochMs: number): string {
  if (epochMs === 0) return '';
  return new Date(epochMs).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function CandlestickChart({
  ticks,
  currentPrice,
  height = 200,
}: CandlestickChartProps) {
  const rows = useMemo<PriceRow[]>(() => {
    return ticks
      .filter((t) => Number.isFinite(t.last) && t.last > 0)
      .map((t) => {
        const epochMs = toEpochMs(t.ts);
        return {
          label: formatTimeLabel(epochMs),
          epochMs,
          last: t.last,
          bid: t.bid != null && Number.isFinite(t.bid) ? t.bid : null,
          ask: t.ask != null && Number.isFinite(t.ask) ? t.ask : null,
        };
      });
  }, [ticks]);

  // --- empty / loading state ---
  if (rows.length === 0) {
    return (
      <div
        className="flex items-center justify-center rounded-lg border border-slate-800 bg-slate-950/40 text-[11px] text-slate-600"
        style={{ height }}
      >
        Waiting for price data…
      </div>
    );
  }

  // --- Y-axis domain ---
  const prices = rows.flatMap((r) => {
    const pts: number[] = [r.last];
    if (r.bid != null) pts.push(r.bid);
    if (r.ask != null) pts.push(r.ask);
    return pts;
  });
  const rawMin = Math.min(...prices);
  const rawMax = Math.max(...prices);
  const span = rawMax - rawMin;
  const pad = span > 0 ? span * 0.1 : rawMin * 0.002;
  const yMin = rawMin - pad;
  const yMax = rawMax + pad;

  return (
    <ResponsiveContainer width="100%" height={height}>
      <AreaChart data={rows} margin={{ top: 6, right: 4, bottom: 0, left: 0 }}>
        <defs>
          <linearGradient id="ttPriceGradient" x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor={CHART_COLORS.equity} stopOpacity={0.18} />
            <stop offset="95%" stopColor={CHART_COLORS.equity} stopOpacity={0.02} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} />
        <XAxis
          dataKey="label"
          stroke={AXIS_STROKE}
          fontSize={AXIS_FONT_SIZE}
          tickLine={false}
          tick={{ fill: '#475569', fontSize: AXIS_FONT_SIZE }}
          minTickGap={48}
          interval="preserveStartEnd"
        />
        <YAxis
          domain={[yMin, yMax]}
          stroke={AXIS_STROKE}
          fontSize={AXIS_FONT_SIZE}
          tickLine={false}
          tick={{ fill: '#475569', fontSize: AXIS_FONT_SIZE }}
          width={58}
          tickFormatter={(v: number) =>
            v >= 10_000 ? v.toFixed(0) : v >= 100 ? v.toFixed(1) : v.toFixed(2)
          }
        />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          labelStyle={{ color: '#cbd5e1' }}
          formatter={(value: number, name: string) => {
            const label =
              name === 'last' ? 'Last' : name === 'bid' ? 'Bid' : 'Ask';
            return [Number.isFinite(value) ? value.toFixed(4) : '—', label];
          }}
        />
        {currentPrice != null && Number.isFinite(currentPrice) && (
          <ReferenceLine
            y={currentPrice}
            stroke="#06b6d4"
            strokeDasharray="4 3"
            strokeWidth={1}
          />
        )}
        <Area
          type="monotone"
          dataKey="last"
          stroke={CHART_COLORS.equity}
          strokeWidth={1.5}
          fill="url(#ttPriceGradient)"
          dot={false}
          isAnimationActive={false}
          connectNulls
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}
