import type { ReactNode } from "react";

type Props = {
  title: string;
  description: string;
  action?: ReactNode;
};

/** Generic empty state for lists, threads, and setup flows. */
export function EmptyState({ title, description, action }: Props) {
  return (
    <div className="empty-state">
      <strong>{title}</strong>
      <p className="form-hint">{description}</p>
      {action}
    </div>
  );
}
