'use client';

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { useRouter } from 'next/navigation';
import IngestDialog from '@/components/IngestDialog';

type Ctx = {
  open: () => void;
  close: () => void;
  isOpen: boolean;
};

const IngestDialogCtx = createContext<Ctx | null>(null);

/**
 * Wraps the app shell so any descendant (sidebar, header, mission-control,
 * strategy page) can pop the global Ingest dialog without each route mounting
 * its own copy. The dialog itself lives at this level (rendered once).
 */
export function IngestDialogProvider({ children }: { children: ReactNode }) {
  const [isOpen, setOpen] = useState(false);
  const router = useRouter();

  const open = useCallback(() => setOpen(true), []);
  const close = useCallback(() => setOpen(false), []);

  const value = useMemo<Ctx>(
    () => ({ open, close, isOpen }),
    [open, close, isOpen],
  );

  return (
    <IngestDialogCtx.Provider value={value}>
      {children}
      <IngestDialog
        open={isOpen}
        onClose={close}
        onLaunched={(strategyId) => {
          close();
          router.push(`/strategies/${strategyId}`);
        }}
      />
    </IngestDialogCtx.Provider>
  );
}

export function useIngestDialog(): Ctx {
  const ctx = useContext(IngestDialogCtx);
  if (!ctx) {
    throw new Error(
      'useIngestDialog must be used inside an <IngestDialogProvider>.',
    );
  }
  return ctx;
}
