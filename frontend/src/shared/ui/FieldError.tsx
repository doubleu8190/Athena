interface FieldErrorProps {
  error?: string;
  id: string;
}

// 统一表单字段级校验错误展示，保证无障碍 alert 语义与样式一致。
export function FieldError({ error, id }: FieldErrorProps) {
  return error ? (
    <p className="field-error" id={id} role="alert">
      {error}
    </p>
  ) : null;
}
