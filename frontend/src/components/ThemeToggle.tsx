import { useTheme } from "@/lib/theme";

const ICON_PROPS = {
  width: 18,
  height: 18,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.75,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
};

function SunIcon() {
  return (
    <svg {...ICON_PROPS} aria-hidden>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg {...ICON_PROPS} aria-hidden>
      <path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8Z" />
    </svg>
  );
}

function SystemIcon() {
  return (
    <svg {...ICON_PROPS} aria-hidden>
      <rect x="3" y="4" width="18" height="12" rx="2" />
      <path d="M8 20h8M12 16v4" />
    </svg>
  );
}

const NEXT_LABEL: Record<string, string> = {
  light: "dark",
  dark: "system",
  system: "light",
};

/** Cycles light -> dark -> system. Shows the icon for the *current* choice. */
export function ThemeToggle({ compact = false }: { compact?: boolean }) {
  const { theme, toggle } = useTheme();
  const icon = theme === "dark" ? <MoonIcon /> : theme === "system" ? <SystemIcon /> : <SunIcon />;

  return (
    <button
      type="button"
      className={compact ? "theme-toggle theme-toggle-compact" : "theme-toggle"}
      onClick={toggle}
      aria-label={`Theme: ${theme}. Switch to ${NEXT_LABEL[theme]}.`}
      title={`Theme: ${theme} (click for ${NEXT_LABEL[theme]})`}
    >
      {icon}
      {compact ? null : <span className="theme-toggle-label">{theme}</span>}
    </button>
  );
}
