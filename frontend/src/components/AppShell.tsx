/**
 * Shell around every page.
 * No opacity fade on mount/route change — that caused a blank flash on refresh.
 */
import { type ReactNode } from "react";

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="app-shell">
      <div className="ui-layer">{children}</div>
    </div>
  );
}
