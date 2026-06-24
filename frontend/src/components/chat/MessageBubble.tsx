interface Props {
  content: string
}

export default function MessageBubble({ content }: Props) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[70%] bg-orange-500 text-white rounded-2xl rounded-br-md px-4 py-2.5 shadow-sm">
        <p className="text-sm leading-relaxed whitespace-pre-wrap">{content}</p>
      </div>
    </div>
  )
}
