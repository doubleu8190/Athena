interface BrandProps {
  compact?: boolean;
  light?: boolean;
  markOnly?: boolean;
}

export function Brand({
  compact = false,
  light = false,
  markOnly = false,
}: BrandProps) {
  return (
    <span
      className={[
        "brand",
        light ? "brand--light" : "",
        markOnly ? "brand--mark-only" : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <span className="brand-mark" aria-hidden="true">
        <span />
      </span>
      {!markOnly && (
        <span className="brand-copy">
          <span className="brand-name">athena</span>
          {!compact && (
            <span className="brand-subtitle">Intelligence Workspace</span>
          )}
        </span>
      )}
    </span>
  );
}
