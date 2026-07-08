'use client';

/**
 * Shared ReactMarkdown component overrides (P8-FIX/M-2, extended in P19 F5).
 *
 * Centralises the safe image / external-link / code-block transformers so every
 * place that renders ingested markdown content (Sources file drawer, KB
 * Explorer article preview, Alpha Lab session view, strategy detail tabs, etc.)
 * gets the same handling:
 *
 * - ``<img>`` is restricted to ``http(s)://`` sources; ``data:`` /
 *   ``javascript:`` URLs are rendered as a visible block instead of being
 *   silently dropped (so the operator notices a stripped payload).
 * - ``<a>`` always opens in a new tab with ``noreferrer noopener`` so
 *   ingested third-party links can't manipulate window.opener.
 * - ``<code>`` distinguishes inline vs block: inline gets a thin emerald
 *   chip; fenced code blocks fall through to the ``<pre>`` wrapper that
 *   paints a slate terminal panel and pins a language badge on the right.
 * - ``<pre>`` wraps fenced code blocks in a bordered panel so multi-line
 *   YAML / Python / SQL examples stop blending into the prose background.
 *
 * No external syntax-highlight library is installed deliberately — adding
 * ``react-syntax-highlighter`` would balloon the bundle by ~200 KB and the
 * project has stayed dependency-lean for 18 patch cycles. The Tailwind-only
 * styling here matches the rest of the Bloomberg-mono aesthetic.
 *
 * Usage: ``<ReactMarkdown components={markdownComponents}>{src}</ReactMarkdown>``
 */

import { isValidElement, type ReactNode } from 'react';
import type { Components } from 'react-markdown';
import type { PluggableList } from 'unified';
import type { Node, Parent } from 'unist';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import { visit } from 'unist-util-visit';

/**
 * Math rendering for ingested prose (papers carry TeX). ``remark-math`` turns
 * ``$…$`` into math, but its single-dollar rule also captures prose money like
 * ``$50 or above $50,000`` — rendering dollar amounts as garbled formulas.
 * ``remarkGuardDollarMath`` runs immediately AFTER remark-math and reverts any
 * inline-math node that is not real TeX back to its literal ``$…$`` text, so
 * amounts (and any other ``$``-delimited prose) survive intact while genuine
 * formulas (``$k$``, ``$\mathcal{O}(s^8)$``, ``$x_i^2$``) still render via KaTeX.
 * Block ``$$…$$`` math is always kept — it does not collide with prose.
 */
interface InlineMathNode extends Node {
  type: 'inlineMath';
  value: string;
}

function isRealInlineMath(value: string): boolean {
  // Real TeX iff: it contains a control char (\ ^ _ { }), OR it is a single
  // whitespace-free token that is not a comma-grouped number ("50,000"). Prose
  // money ("50 or above 50,000") has spaces/grouping and is therefore reverted.
  if (/[\\^_{}]/.test(value)) return true;
  return !/\s/.test(value) && !/\d,\d{3}/.test(value);
}

function remarkGuardDollarMath() {
  return (tree: Node): void => {
    visit(
      tree,
      (node: Node): boolean => node.type === 'inlineMath',
      (node, index, parent) => {
        if (typeof index !== 'number' || !parent) return;
        const value = (node as InlineMathNode).value;
        if (isRealInlineMath(value)) return;
        (parent as Parent).children[index] = { type: 'text', value: `$${value}$` } as Node;
      },
    );
  };
}

/**
 * Shared remark/rehype plugin lists for math-aware markdown. Pass BOTH to every
 * ``<ReactMarkdown>`` that renders ingested prose so papers' formulas render
 * (KaTeX) without mangling dollar amounts. ``throwOnError: false`` keeps a
 * malformed formula visible (in red) rather than crashing the whole render.
 * Requires ``katex/dist/katex.min.css`` to be loaded once (see app/layout.tsx).
 */
export const remarkMathPlugins: PluggableList = [remarkMath, remarkGuardDollarMath];
export const rehypeMathPlugins: PluggableList = [
  [rehypeKatex, { throwOnError: false, errorColor: '#f87171' }],
];

function extractLanguage(children: ReactNode): string | undefined {
  if (!isValidElement(children)) return undefined;
  const props = children.props as { className?: unknown } | undefined;
  const cls = typeof props?.className === 'string' ? props.className : '';
  const m = /language-([\w-]+)/.exec(cls);
  return m?.[1];
}

export const markdownComponents: Components = {
  img: ({ src, alt }) => {
    // http(s) plus the local asset cache's same-origin `/api/assets/<sha256>`
    // refs (proxied to the backend via the next.config.mjs rewrite). The hash
    // is pinned to exactly 64 hex chars so no other relative path slips in.
    const safe =
      typeof src === 'string' &&
      (/^https?:\/\//i.test(src) || /^\/api\/assets\/[0-9a-f]{64}$/i.test(src))
        ? src
        : '';
    if (!safe) {
      return (
        <span className="my-1 inline-block rounded border border-rose-700/40 bg-rose-500/10 px-2 py-1 text-[10px] font-mono text-rose-300">
          [blocked image{alt ? `: ${alt}` : ''}]
        </span>
      );
    }
    return (
      <img
        src={safe}
        alt={alt ?? ''}
        loading="lazy"
        referrerPolicy="no-referrer"
        className="my-2 max-w-full rounded border border-slate-800"
      />
    );
  },
  a: ({ href, children, ...rest }) => {
    // P-WIKILINK — Obsidian-style [[concept]] tokens are pre-rewritten to
    // `[concept](#wiki:concept)` before markdown parsing (see Alpha Lab's
    // preprocessWikilinks). Render those as a concept chip instead of a normal
    // anchor. Scoped to #wiki: hrefs, so every other markdown surface (Sources,
    // KB Explorer, strategy detail…) is completely unaffected.
    if (typeof href === 'string' && href.startsWith('#wiki:')) {
      return (
        <span
          className="rounded border border-cyan-800/50 bg-cyan-500/10 px-1 font-mono text-[11px] text-cyan-300"
          title="concept"
        >
          {children}
        </span>
      );
    }
    // P29-F10: sanitize href against javascript:/data:/vbscript: schemes.
    const safe = (() => {
      if (typeof href !== 'string') return undefined;
      const trimmed = href.trim();
      const lower = trimmed.toLowerCase();
      if (!lower) return undefined;
      // P34: positive allowlist (replaces the leaky javascript:/data:/vbscript:
      // denylist). Permit http(s)/mailto + same-origin relative/anchor links;
      // drop protocol-relative "//host" and any other scheme (file:/blob:/…).
      if (
        lower.startsWith('http://') ||
        lower.startsWith('https://') ||
        lower.startsWith('mailto:') ||
        ((trimmed.startsWith('/') || trimmed.startsWith('#')) && !trimmed.startsWith('//'))
      ) {
        return href;
      }
      return undefined;
    })();
    if (!safe) {
      return (
        <span className="rounded border border-rose-700/40 bg-rose-500/10 px-1 text-[10px] font-mono text-rose-300">
          [blocked link] {children}
        </span>
      );
    }
    const isExternal =
      safe.startsWith('http://') ||
      safe.startsWith('https://') ||
      safe.startsWith('mailto:');
    return (
      <a
        href={safe}
        {...(isExternal ? { target: '_blank', rel: 'noreferrer noopener' } : {})}
        {...rest}
      >
        {children}
      </a>
    );
  },
  // P19 F5 — inline vs fenced code distinction.
  // ReactMarkdown 9 emits ``<code className="language-xxx">…</code>`` for
  // fenced blocks (wrapped in <pre>) and plain ``<code>…</code>`` (no
  // language class) for inline backticks. We use the language-class as the
  // discriminator. Block <code> stays minimal — the parent <pre> renderer
  // owns the visual chrome. Inline <code> gets a chip with a thin emerald
  // background so it visually separates from prose.
  code: ({ className, children, ...rest }) => {
    const cls = typeof className === 'string' ? className : '';
    const isBlock = /(^|\s)language-/.test(cls);
    if (isBlock) {
      const passClass = `${cls} font-mono text-[11px] leading-relaxed text-emerald-300`.trim();
      return (
        <code className={passClass} {...rest}>
          {children}
        </code>
      );
    }
    return (
      <code
        className="rounded border border-emerald-800/40 bg-emerald-500/10 px-1 py-0.5 font-mono text-[11px] text-emerald-300"
        {...rest}
      >
        {children}
      </code>
    );
  },
  pre: ({ children, ...rest }) => {
    const lang = extractLanguage(children);
    return (
      <div className="relative my-2 overflow-hidden rounded border border-slate-800 bg-slate-950">
        {lang && (
          <span
            className="absolute right-2 top-1 select-none rounded bg-slate-800/70 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-slate-400"
            aria-hidden="true"
          >
            {lang}
          </span>
        )}
        <pre
          className="overflow-x-auto p-3 font-mono text-[11px] leading-relaxed text-emerald-300"
          {...rest}
        >
          {children}
        </pre>
      </div>
    );
  },
};
