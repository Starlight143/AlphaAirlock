/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // vis-network ships ESM that Next would otherwise try to transpile inside SSR.
  // Wrapped in dynamic({ ssr: false }) on the client, but we still
  // mark it transpiled for build-time safety.
  transpilePackages: ['vis-network', 'vis-data'],
  // P6 asset cache — ingested markdown references cached images as
  // same-origin `/api/assets/<sha256>` paths, but the FastAPI backend (which
  // actually serves them) lives on a different port. Proxy just that route so
  // the images render without widening the img-src CSP beyond 'self'.
  async rewrites() {
    const apiBase = process.env.NEXT_PUBLIC_API_BASE || 'http://127.0.0.1:8000';
    return [
      {
        source: '/api/assets/:hash',
        destination: `${apiBase}/api/assets/:hash`,
      },
    ];
  },
  // P29-F13: baseline security headers. CSP scoped to self + local FastAPI.
  async headers() {
    return [
      {
        source: '/:path*',
        headers: [
          {
            key: 'Content-Security-Policy',
            value: [
              "default-src 'self'",
              // P30-S6: 'unsafe-eval' is only required by Next.js dev-mode
              // HMR / React refresh. Production builds do not need it;
              // keeping it permanently enabled defeats most XSS protection.
              // 'unsafe-inline' is retained because Next ships __NEXT_DATA__
              // via an inline <script> tag and the app does not yet route
              // hydration through a nonce-middleware (separate refactor).
              process.env.NODE_ENV === 'production'
                ? "script-src 'self' 'unsafe-inline'"
                : "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
              "style-src 'self' 'unsafe-inline'",
              "img-src 'self' data: blob: https:",
              "font-src 'self' data:",
              "connect-src 'self' http://localhost:* http://127.0.0.1:* ws://localhost:* ws://127.0.0.1:*",
              "frame-ancestors 'none'",
              "base-uri 'self'",
              "form-action 'self'",
            ].join('; '),
          },
          { key: 'X-Frame-Options', value: 'DENY' },
          { key: 'X-Content-Type-Options', value: 'nosniff' },
          { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
          { key: 'Permissions-Policy', value: 'camera=(), microphone=(), geolocation=(), payment=()' },
        ],
      },
    ];
  },
};

export default nextConfig;
