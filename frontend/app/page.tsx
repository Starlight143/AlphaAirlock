import { redirect } from 'next/navigation';

/**
 * Root route — always redirects to the Mission Control homepage.
 * Keeping this thin (no UI) means /<empty> is never a broken landing page
 * during the multi-page refactor.
 */
export default function RootPage(): never {
  redirect('/mission-control');
}
