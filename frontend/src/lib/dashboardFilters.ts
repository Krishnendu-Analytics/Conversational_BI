/**
 * Cross-dashboard filtering context and utilities.
 */

export type DashboardFilterState = {
  [key: string]: string | number | null;
};

export type FilterableColumn = {
  column: string;
  value: string | number;
};

export function applyFiltersToRows(
  rows: Record<string, unknown>[],
  filters: DashboardFilterState,
): Record<string, unknown>[] {
  if (Object.keys(filters).length === 0) return rows;

  return rows.filter((row) => {
    return Object.entries(filters).every(([column, value]) => {
      if (value === null) return true;
      const cellValue = String(row[column] ?? "").toLowerCase();
      const filterValue = String(value).toLowerCase();
      return cellValue === filterValue || cellValue.includes(filterValue);
    });
  });
}

export function getUniqueValues(
  rows: Record<string, unknown>[],
  column: string,
): (string | number)[] {
  const values = new Set<string | number>();
  rows.forEach((row) => {
    const val = row[column];
    if (val !== null && val !== undefined && val !== "") {
      values.add(String(val));
    }
  });
  return Array.from(values).sort();
}

export function debounce<T extends (...args: any[]) => any>(
  func: T,
  wait: number,
): (...args: Parameters<T>) => void {
  let timeout: ReturnType<typeof setTimeout> | null = null;
  return (...args: Parameters<T>) => {
    if (timeout) clearTimeout(timeout);
    timeout = setTimeout(() => func(...args), wait);
  };
}
