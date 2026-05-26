import { lazy, Suspense } from "react";

const TextareaCodeEditor = lazy(() => import("@uiw/react-textarea-code-editor"));

interface LazyCodeEditorProps {
  value: string;
  language: string;
  onChange: (event: React.ChangeEvent<HTMLTextAreaElement>) => void;
  placeholder?: string;
  padding?: number;
  disabled?: boolean;
  className?: string;
  spellCheck?: boolean;
}

export default function LazyCodeEditor({
  value,
  language,
  onChange,
  placeholder,
  padding,
  disabled,
  className,
  spellCheck,
}: LazyCodeEditorProps) {
  const fallbackClassName = [
    "w-full rounded-lg bg-bg border border-border focus:border-primary focus:outline-none resize-y",
    className,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <Suspense
      fallback={
        <textarea
          value={value}
          onChange={onChange}
          placeholder={placeholder}
          disabled={disabled}
          className={fallbackClassName}
          spellCheck={spellCheck}
          style={padding !== undefined ? { padding } : undefined}
        />
      }
    >
      <TextareaCodeEditor
        value={value}
        language={language}
        onChange={onChange}
        placeholder={placeholder}
        padding={padding}
        disabled={disabled}
        className={className}
        spellCheck={spellCheck}
      />
    </Suspense>
  );
}