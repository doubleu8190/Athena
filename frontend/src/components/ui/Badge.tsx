type BadgeVariant = 'green' | 'red' | 'yellow' | 'blue' | 'gray' | 'purple' | 'orange'
const variantClasses: Record<BadgeVariant, string> = {
  green: 'bg-green-100 dark:bg-green-950 text-green-700 dark:text-green-400',
  red: 'bg-red-100 dark:bg-red-950 text-red-700 dark:text-red-400',
  yellow: 'bg-yellow-100 dark:bg-yellow-950 text-yellow-700 dark:text-yellow-400',
  blue: 'bg-blue-100 dark:bg-blue-950 text-blue-700 dark:text-blue-400',
  gray: 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400',
  purple: 'bg-purple-100 dark:bg-purple-950 text-purple-700 dark:text-purple-400',
  orange: 'bg-orange-100 dark:bg-orange-950 text-orange-700 dark:text-orange-400',
}

interface BadgeProps {
  children: React.ReactNode
  variant?: BadgeVariant
  dot?: boolean
  className?: string
}

export default function Badge({ children, variant = 'gray', dot = false, className = '' }: BadgeProps) {
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${variantClasses[variant]} ${className}`}
    >
      {dot && <span className={`w-1.5 h-1.5 rounded-full bg-current opacity-60`} />}
      {children}
    </span>
  )
}

/** Priority badge — P1 red, P2 orange, P3 blue */
export function PriorityBadge({ priority }: { priority: number }) {
  const variant: BadgeVariant =
    priority <= 1 ? 'red' : priority <= 2 ? 'orange' : 'blue'
  return (
    <Badge variant={variant} className="font-bold text-[10px] px-1.5">
      P{priority}
    </Badge>
  )
}
