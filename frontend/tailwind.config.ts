import type { Config } from 'tailwindcss';

const config: Config = {
  content: [
    './app/**/*.{ts,tsx}',
    './components/**/*.{ts,tsx}',
    './pages/**/*.{ts,tsx}',
    './lib/**/*.{ts,tsx}',
  ],
  theme: {
    extend: {
      fontFamily: {
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
      },
      colors: {
        glow: {
          green: '#10b981',
          emerald: '#34d399',
          amber: '#f59e0b',
          rose: '#f43f5e',
          purple: '#a855f7',
          blue: '#3b82f6',
        },
      },
      animation: {
        'pulse-slow': 'pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'glow-pulse': 'glowPulse 1.8s ease-in-out infinite',
      },
      keyframes: {
        glowPulse: {
          '0%, 100%': { boxShadow: '0 0 4px rgba(16,185,129,0.6)' },
          '50%': { boxShadow: '0 0 14px rgba(16,185,129,1.0)' },
        },
      },
    },
  },
  plugins: [require('@tailwindcss/typography')],
};

export default config;
