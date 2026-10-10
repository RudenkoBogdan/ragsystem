/**
 * Thin progress bar. `value` in [0, 1] for known progress; omit it for an
 * indeterminate bar (work is happening but its size is unknown).
 */
export default function ProgressBar({
  value,
  label,
  className = "",
}: {
  value?: number | null;
  label?: string;
  className?: string;
}) {
  const known = typeof value === "number";
  const pct = known ? Math.round(Math.max(0, Math.min(1, value)) * 100) : null;

  return (
    <div className={className}>
      {label && (
        <div className="mb-1 flex items-center justify-between gap-2 text-[11px] text-text-muted">
          <span className="truncate">{label}</span>
          {pct !== null && <span className="tabular-nums">{pct}%</span>}
        </div>
      )}
      <div
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct ?? undefined}
        className="h-1 w-full overflow-hidden rounded-full bg-bg-secondary"
      >
        {known ? (
          <div
            className="h-full rounded-full bg-accent-blue transition-[width] duration-500 ease-out"
            style={{ width: `${pct}%` }}
          />
        ) : (
          <div className="progress-indeterminate h-full w-2/5 rounded-full bg-accent-blue" />
        )}
      </div>
    </div>
  );
}
