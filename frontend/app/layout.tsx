import './globals.css';
import 'katex/dist/katex.min.css'; // KaTeX math rendering (papers carry TeX); loaded once, globally
import type { Metadata } from 'next';
import type { ReactNode } from 'react';
import { Providers } from './providers';

export const metadata: Metadata = {
  title: 'Agentic Alpha Research System',
  description: 'Multi-agent quant research pipeline — Bloomberg-style workspace',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
