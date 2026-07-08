// Registered navigation table — single source of truth for the 17-module
// sidebar. Imported by both the Sidebar client component and any breadcrumb /
// page-title generators.
//
// Each entry's `status`:
//   - 'live'         fully functional page (rendered as-is in P0)
//   - 'placeholder'  routed but renders an inline placeholder card
//
// P16 A-M3 — every entry below is `status: 'live'`; the EmptyShell
// placeholder component was removed because no entry referenced it.
// As features ship in P1-P4, flip the corresponding entry's status to 'live'.
//
// P8-FIX/NAV-1: order rearranged to match the reference video — Alpha Lab is
// promoted to the top of the Knowledge group, and the Execution group sits
// before Knowledge in the sidebar ordering (so trade-flow items render in the
// vertical centre, with Knowledge / lab tooling at the bottom).

import type { LucideIcon } from 'lucide-react';
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Brain,
  ClipboardCheck,
  Compass,
  Database,
  FlaskConical,
  GitBranch,
  GitMerge,
  Joystick,
  LayoutDashboard,
  LineChart,
  ListChecks,
  MessageSquare,
  Network,
  Radio,
  Scale,
  Search,
  Settings,
  Sigma,
  Sparkles,
  Target,
  Telescope,
  Wallet,
  Workflow,
} from 'lucide-react';

export type NavGroup = 'pipeline' | 'analytics' | 'execution' | 'knowledge';

export type NavItem = {
  href: string;
  label: string;
  icon: LucideIcon;
  group: NavGroup;
  status: 'live' | 'placeholder';
  phase?: 'P0' | 'P1' | 'P2' | 'P3' | 'P4' | 'P5' | 'P6' | 'P7' | 'P8';
  hint?: string;
};

export const NAV: NavItem[] = [
  // --- Pipeline ---
  { href: '/mission-control',     label: 'Mission Central',     icon: Compass,        group: 'pipeline',  status: 'live', phase: 'P0' },
  { href: '/pipeline-analytics',  label: 'Pipeline Analytics',  icon: Workflow,       group: 'pipeline',  status: 'live', phase: 'P7' },
  { href: '/gate-review',         label: 'Gate Review',         icon: ClipboardCheck, group: 'pipeline',  status: 'live', phase: 'P5' },
  { href: '/mission-panel',       label: 'Mission Panel',       icon: ListChecks,     group: 'pipeline',  status: 'live', phase: 'P7' },

  // --- Analytics ---
  { href: '/backtest-lab',        label: 'Backtest Lab',        icon: FlaskConical,   group: 'analytics', status: 'live', phase: 'P8' },
  { href: '/arena',               label: 'Arena',               icon: Sparkles,       group: 'analytics', status: 'live', phase: 'P7' },
  { href: '/backtest-panel',      label: 'Backtest Panel',      icon: BarChart3,      group: 'analytics', status: 'live', phase: 'P2' },
  { href: '/alpha-flow',          label: 'Alpha Flow',          icon: GitBranch,      group: 'analytics', status: 'live', phase: 'P7' },
  { href: '/ir-explorer',         label: 'IR Explorer',         icon: LineChart,      group: 'analytics', status: 'live', phase: 'P7' },

  // --- Execution (above Knowledge per P8-FIX/NAV-1) ---
  { href: '/paper-trade',         label: 'Paper Trade',         icon: Activity,       group: 'execution', status: 'live', phase: 'P4' },
  { href: '/sim-account',         label: 'Sim Account',         icon: Wallet,         group: 'execution', status: 'live', phase: 'P8' },
  { href: '/live-trade',          label: 'Live Trade',          icon: Target,         group: 'execution', status: 'live', phase: 'P8' },
  { href: '/trading-terminal',    label: 'Trading Terminal',    icon: Joystick,       group: 'execution', status: 'live', phase: 'P7' },
  { href: '/portfolio-optimizer', label: 'Portfolio Optimizer', icon: Scale,          group: 'execution', status: 'live', phase: 'P7' },

  // --- Execution: strategy detail anchor (no index page; href kept in execution
  //     group so /strategies/:id highlights the correct group and the header
  //     breadcrumb shows the canonical label instead of the deriveFallbackLabel
  //     fallback). The href points to /arena which is the nearest listing view.
  { href: '/strategies',          label: 'Strategies',          icon: GitBranch,      group: 'execution', status: 'live', phase: 'P0' },

  // --- Knowledge (Alpha Lab is the entrypoint — promoted to top) ---
  { href: '/alpha-lab',           label: 'Alpha Lab',           icon: MessageSquare,  group: 'knowledge', status: 'live', phase: 'P4' },
  { href: '/kb-explorer',         label: 'KB Explorer',         icon: Database,       group: 'knowledge', status: 'live', phase: 'P3' },
  { href: '/factor-network',      label: 'Factor Network',      icon: Network,        group: 'knowledge', status: 'live', phase: 'P3' },
  { href: '/factor-explorer',     label: 'Factor Explorer',     icon: Telescope,      group: 'knowledge', status: 'live', phase: 'P5' },
  { href: '/factor-studio',       label: 'Factor Studio',       icon: Sigma,          group: 'knowledge', status: 'live', phase: 'P7' },
  { href: '/alpha-genealogy',     label: 'Alpha Genealogy',     icon: Brain,          group: 'knowledge', status: 'live', phase: 'P7' },
  { href: '/cointegration',       label: 'Cointegration',       icon: GitMerge,       group: 'knowledge', status: 'live', phase: 'P8' },
  { href: '/sources',             label: 'Sources',             icon: Radio,          group: 'knowledge', status: 'live', phase: 'P3' },
  // P11-F2-11 — Alpha Dashboard moved to Knowledge tail
  { href: '/alpha-dashboard',     label: 'Alpha Health',        icon: LayoutDashboard, group: 'knowledge', status: 'live', phase: 'P5' },
];

export const NAV_GROUPS: { id: NavGroup; label: string }[] = [
  { id: 'pipeline',  label: 'Pipeline' },
  { id: 'analytics', label: 'Analytics' },
  { id: 'execution', label: 'Execution' },
  { id: 'knowledge', label: 'Knowledge' },
];

export function navItemFor(pathname: string): NavItem | undefined {
  // Prefer the longest match so /strategies/47 resolves to /strategies when
  // present (currently it falls through to undefined; sidebar treats this as
  // "no active item" rather than throwing).
  let best: NavItem | undefined;
  for (const item of NAV) {
    if (pathname === item.href || pathname.startsWith(item.href + '/')) {
      if (!best || item.href.length > best.href.length) best = item;
    }
  }
  return best;
}

// Quick-search icon used by the header search field; kept here so the icon
// import stays in lockstep with the nav table.
export const HEADER_SEARCH_ICON = Search;
export const HEADER_SETTINGS_ICON = Settings;
export const HEADER_ALERT_ICON = AlertTriangle;
