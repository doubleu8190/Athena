interface SuccessGaugeProps {
  percentage: number
  size?: number
}

export default function SuccessGauge({ percentage, size = 120 }: SuccessGaugeProps) {
  const radius = 45
  const circumference = 2 * Math.PI * radius
  const offset = circumference - (Math.min(percentage, 100) / 100) * circumference

  const color = percentage >= 80 ? '#3fb950' : percentage >= 50 ? '#d29922' : '#f85149'

  return (
    <div className="flex flex-col items-center gap-2">
      <svg width={size} height={size} viewBox="0 0 120 120">
        {/* Background circle */}
        <circle
          cx="60"
          cy="60"
          r={radius}
          fill="none"
          stroke="#21262d"
          strokeWidth="8"
        />
        {/* Progress circle */}
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
          style={{ transition: 'stroke-dashoffset 0.5s ease' }}
        />
        {/* Center text */}
        <text
          x="60"
          y="60"
          textAnchor="middle"
          dominantBaseline="central"
          fill="#e6edf3"
          fontSize="22"
          fontWeight="bold"
        >
          {percentage}%
        </text>
      </svg>
      <span className="text-xs text-text-muted">Success Rate (24h)</span>
    </div>
  )
}
