interface Props {
  label: string
  value: number | string
  trend?: string
  color: 'green' | 'blue' | 'yellow' | 'red'
  icon: React.ReactNode
}

const colorClasses = {
  green: 'text-green-600 dark:text-green-400',
  blue: 'text-blue-600 dark:text-blue-400',
  yellow: 'text-amber-600 dark:text-amber-400',
  red: 'text-red-600 dark:text-red-400',
}

const bgClasses = {
  green: 'bg-green-100 dark:bg-green-950',
  blue: 'bg-blue-100 dark:bg-blue-950',
  yellow: 'bg-amber-100 dark:bg-amber-950',
  red: 'bg-red-100 dark:bg-red-950',
}

export default function MetricCard({ label, value, trend, color, icon }: Props) {
  return (
    <div className="bg-white dark:bg-gray-900 rounded-xl border border-gray-200 dark:border-gray-800 p-5">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-[10px] font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wide">
            {label}
          </div>
          <div className={`text-2xl font-bold mt-1 ${colorClasses[color]}`}>{value}</div>
        </div>
        <div className={`w-9 h-9 rounded-full ${bgClasses[color]} flex items-center justify-center`}>
          {icon}
        </div>
      </div>
      {trend && <div className="text-[10px] text-gray-400 mt-3">{trend}</div>}
    </div>
  )
}
