import type { ReactNode } from "react";

type Tone = "neutral" | "accent" | "success" | "warning" | "danger" | "info";
type Variant = "soft" | "solid" | "outline";
type Size = "sm" | "md";

type Props = {
  children: ReactNode;
  tone?: Tone;
  variant?: Variant;
  size?: Size;
};

/** Compact status/tag pill. Tone carries meaning; keep usage sparse. */
export function Badge({ children, tone = "neutral", variant = "soft", size = "sm" }: Props) {
  return (
    <span className={`badge badge-${tone} badge-${variant} badge-${size}`}>{children}</span>
  );
}
