'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import clsx from 'clsx';
import { NAV, NAV_GROUPS, type NavItem } from './nav';

/**
 * Fixed left rail nav. 17 modules grouped into 4 categories. Uses a registered
 * route table (see ./nav.ts) so highlighting + ordering stay in lockstep with
 * any future page additions.
 */
export default function Sidebar() {
  const pathname = usePathname() || '';

  return (
    <aside className="flex h-full w-60 flex-col border-r border-slate-800 bg-slate-950/70">
      <Link
        href="/mission-control"
        className="flex h-16 items-center gap-2 border-b border-slate-800 px-4 hover:bg-slate-900/50"
      >
        <span className="inline-block h-3 w-3 rounded-full bg-cyan-400 shadow-[0_0_10px_#22D3EE]" />
        <div className="flex flex-col leading-tight">
          <span className="text-[11px] font-bold tracking-widest text-cyan-300">
            {(() => {
              // P16 A-L5 — `.toUpperCase()` is a no-op on CJK and
              // looks visually noisy on mixed scripts. Only force
              // uppercase when the brand string is pure ASCII so
              // non-Latin alphabets render at their native case.
              const brand = process.env.NEXT_PUBLIC_BRAND_NAME ?? 'Agentic Alpha';
              return /^[\x00-\x7F]+$/.test(brand) ? brand.toUpperCase() : brand;
            })()}
          </span>
          <span className="text-[9px] uppercase tracking-[0.2em] text-slate-500">
            research suite
          </span>
        </div>
      </Link>

      <nav className="flex-1 overflow-y-auto px-2 py-3 text-[11px]">
        {/* P12 A-M3 — Mission Control / Central is pinned above every group
            so the operator's first-look destination never moves when new
            pipeline pages ship. */}
        {(() => {
          const missionItem = NAV.find((i) => i.href === '/mission-control');
          if (!missionItem) return null;
          return (
            <div className="mb-4">
              <ul className="space-y-0.5">
                <SidebarLink
                  key={missionItem.href}
                  item={missionItem}
                  active={
                    pathname === missionItem.href ||
                    pathname.startsWith(missionItem.href + '/')
                  }
                />
              </ul>
            </div>
          );
        })()}
        {NAV_GROUPS.map((group) => {
          // Mission Control is rendered above — skip it here to avoid
          // double-rendering inside its own pipeline group.
          const items = NAV.filter(
            (i) => i.group === group.id && i.href !== '/mission-control',
          );
          if (items.length === 0) return null;
          return (
            <div key={group.id} className="mb-4">
              <div className="px-2 pb-1 text-[9px] font-bold uppercase tracking-[0.18em] text-slate-600">
                {group.label}
              </div>
              <ul className="space-y-0.5">
                {items.map((item) => (
                  <SidebarLink
                    key={item.href}
                    item={item}
                    active={
                      pathname === item.href ||
                      pathname.startsWith(item.href + '/')
                    }
                  />
                ))}
              </ul>
            </div>
          );
        })}
      </nav>

      {/* P11-F2-12 — version footer is hidden by default to keep the rail
          uncluttered; set NEXT_PUBLIC_SHOW_VERSION_FOOTER=1 to expose it for
          internal builds / on-call diagnostics. */}
      {process.env.NEXT_PUBLIC_SHOW_VERSION_FOOTER === '1' && (
        <div className="border-t border-slate-800 p-3 text-[10px] text-slate-500">
          <div className="flex items-center justify-between">
            <span>v{process.env.NEXT_PUBLIC_APP_VERSION ?? '1.0.0'}</span>
            <span className="rounded border border-slate-800 px-1.5 py-0.5 text-cyan-400">
              P0 shell
            </span>
          </div>
        </div>
      )}
    </aside>
  );
}

function SidebarLink({ item, active }: { item: NavItem; active: boolean }) {
  const Icon = item.icon;
  return (
    <li>
      <Link
        href={item.href}
        title={item.hint}
        className={clsx(
          'flex items-center gap-2 rounded-md px-2 py-1.5 transition-colors',
          active
            ? 'bg-cyan-500/10 text-cyan-200 ring-1 ring-inset ring-cyan-700/40'
            : 'text-slate-400 hover:bg-slate-900 hover:text-slate-200',
        )}
      >
        <Icon className={clsx('h-3.5 w-3.5', active ? 'text-cyan-300' : 'text-slate-500')} />
        <span className="flex-1 truncate">{item.label}</span>
        {/* P6-L01: phase pill removed — reference screenshots show a clean
            sidebar without phase badges. The phase metadata is still kept on
            ``nav.ts`` for tooltip and changelog use. */}
      </Link>
    </li>
  );
}
