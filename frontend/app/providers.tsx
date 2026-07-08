'use client';

import { QueryClientProvider } from '@tanstack/react-query';
import { useState, type ReactNode } from 'react';
import { createQueryClient } from '@/lib/query';

/**
 * Root client-provider wrapper. Hosts the TanStack QueryClient + any future
 * cross-app contexts (theme, feature flags). The QueryClient is created lazily
 * via useState so React's Strict-Mode double-render in dev doesn't blow away
 * the cache.
 */
export function Providers({ children }: { children: ReactNode }) {
  const [client] = useState(() => createQueryClient());
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
