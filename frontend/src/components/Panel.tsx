/** Content panel (no fade-from-invisible — that blanked the page on refresh). */
import type { ReactNode } from "react";

export function Panel({
  children,
  className = "",
  wide = false,
}: {
  children: ReactNode;
  className?: string;
  wide?: boolean;
}) {
  return (
    <div className={`glass-panel ${wide ? "glass-panel-wide" : ""} ${className}`}>
      {children}
    </div>
  );
}
