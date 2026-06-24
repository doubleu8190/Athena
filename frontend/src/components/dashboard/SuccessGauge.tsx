interface SuccessGaugeProps {
  percentage: number
  size?: number
}

export default function SuccessGauge({ percentage, size = 120 }: SuccessGaugeProps) {
  const radius = 45
  const circumference = 2 * Math.PI * radius
  const offset = circumference - (Math.min(percentage, 100) / 100) * circumference

  const successColor = 'var(--color-success)'
  const warningColor = 'var(--color-warning)'
  const errorColor = 'var(--color-error)'
  const color = percentage >= 80 ? successColor : percentage >= 50 ? warningColor : errorColor

  return (
    <div className="flex flex-col items-center gap-2">
      <svg width={size} height={size} viewBox="0 0 120 120">
        <defs>
          <filter id="glow" x="-20%" y="-20%" width="140%" height="140%">
            <feGaussianBlur stdDeviation="2" result="blur" />
            <feComposite in="SourceGraphic" in2="blur" operator="over" />
          </filter>
        </defs>
        {/* Background circle */}
        <circle
          cx="60"
          cy="60"
          r={radius}
          fill="none"
          stroke="var(--color-bg-elevated)"
          strokeWidth="8"
        />
        {/* Progress circle with glow */}
        <circle
          cx="60"
          cy="60"
          r={radius}
          fill="none"
          stroke={color}
          strokeWidth="8"
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          transform="rotate(-90 60 60)"
          filter="url(#glow)"
          style={{ transition: 'stroke-dashoffset 1s ease-out' }}
        />
        {/* Center text */}
        <text
          x="60"
          y="55"
          textAnchor="middle"
          dominantBaseline="central"
          fill="var(--color-text-primary)"
          fontSize="22"
          fontWeight="bold"
        >
          {percentage}%
        </text>
        <text
          x="60"
          y="72"
          textAnchor="middle"
          dominantBaseline="central"
          fill="var(--color-text-secondary)"
          fontSize="10"
        >
          Success Rate
        </text>
      </svg>
      <span className="text-xs text-text-muted">Past 24 hours</span>
    </div>
  )
}
