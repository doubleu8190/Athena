interface Props {
  percentage: number
}

export default function SuccessGauge({ percentage }: Props) {
  const radius = 42
  const circumference = 2 * Math.PI * radius
  const offset = circumference - (percentage / 100) * circumference

  // Color based on percentage
  const color =
    percentage >= 80
      ? 'text-green-500'
      : percentage >= 50
        ? 'text-amber-500'
        : 'text-red-500'

  return (
    <div className="bg-white dark:bg-gray-900 rounded-xl border border-gray-200 dark:border-gray-800 p-5 flex flex-col items-center">
      <div className="text-[10px] font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-3">
        Success Rate (24h)
      </div>
      <div className="relative w-32 h-32">
        <svg viewBox="0 0 100 100" className="w-full h-full -rotate-90">
          <circle
            cx="50"
            cy="50"
            r={radius}
            fill="none"
            stroke="currentColor"
            className="text-gray-100 dark:text-gray-800"
            strokeWidth="8"
          />
          <circle
            cx="50"
            cy="50"
            r={radius}
            fill="none"
            stroke="currentColor"
            className={color}
            strokeWidth="8"
            strokeDasharray={circumference}
            strokeDashoffset={offset}
            strokeLinecap="round"
          />
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <span className={`text-xl font-bold ${color}`}>{percentage.toFixed(1)}%</span>
          <span className="text-[10px] text-gray-400 mt-0.5">success</span>
        </div>
      </div>
    </div>
  )
}
