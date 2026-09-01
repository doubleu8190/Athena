interface FilterRowProps {
  label: string
  checked: boolean
  onChange: (checked: boolean) => void
}

function FilterRow({ label, checked, onChange }: FilterRowProps) {
  return (
    <label className="flex items-center gap-2 text-sm text-athena-text cursor-pointer select-none">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="w-3.5 h-3.5 accent-athena-accent rounded"
      />
      {label}
    </label>
  )
}

export default FilterRow
