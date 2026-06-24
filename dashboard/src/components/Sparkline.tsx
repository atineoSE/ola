/*
 * Sparkline — a dependency-free inline-SVG trend line.
 *
 * No chart library: the dashboard deliberately ships none, and a sparkline is
 * a single normalized `<polyline>`. Values are mapped into a fixed viewBox so
 * the SVG scales with its container (CSS width/height), and the y-axis is
 * inverted (SVG's origin is top-left) so larger values draw higher.
 */

export interface SparklineProps {
  /** The series to plot, oldest → newest. */
  values: number[];
  /** ViewBox width in user units. */
  width?: number;
  /** ViewBox height in user units. */
  height?: number;
  className?: string;
  "data-testid"?: string;
}

export function Sparkline({
  values,
  width = 100,
  height = 24,
  className,
  "data-testid": testId,
}: SparklineProps) {
  // Nothing to draw without at least two points to connect.
  if (values.length < 2) return null;

  const min = Math.min(...values);
  const max = Math.max(...values);
  // A flat series has zero span; pin it to the vertical midline rather than
  // dividing by zero.
  const span = max - min || 1;
  const step = width / (values.length - 1);

  const points = values
    .map((v, i) => {
      const x = i * step;
      const y = height - ((v - min) / span) * height;
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");

  return (
    <svg
      data-testid={testId}
      className={className}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      role="img"
      aria-hidden="true"
    >
      <polyline
        data-testid="sparkline-polyline"
        points={points}
        fill="none"
        stroke="currentColor"
        strokeWidth={1.5}
        strokeLinejoin="round"
        strokeLinecap="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}
