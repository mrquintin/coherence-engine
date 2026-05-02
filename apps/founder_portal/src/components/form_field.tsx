"use client";

import { useId, type InputHTMLAttributes, type TextareaHTMLAttributes } from "react";

interface BaseProps {
  label: string;
  hint?: string;
  error?: string | null;
  required?: boolean;
}

type InputProps = BaseProps &
  Omit<InputHTMLAttributes<HTMLInputElement>, "id" | "aria-describedby">;

type TextareaProps = BaseProps &
  Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "id" | "aria-describedby">;

function describedBy(id: string, hint?: string, error?: string | null): string | undefined {
  const parts: string[] = [];
  if (hint) parts.push(`${id}-hint`);
  if (error) parts.push(`${id}-error`);
  return parts.length > 0 ? parts.join(" ") : undefined;
}

export function FormField({ label, hint, error, required, ...rest }: InputProps) {
  const id = useId();
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-sm font-medium text-slate-700">
        {label}
        {required ? (
          <span aria-hidden="true" className="ml-1 text-rose-600">
            *
          </span>
        ) : null}
      </label>
      <input
        id={id}
        required={required}
        aria-required={required ? "true" : undefined}
        aria-invalid={error ? "true" : undefined}
        aria-describedby={describedBy(id, hint, error)}
        className="block w-full rounded-md border border-slate-300 bg-white px-3 py-2 shadow-sm focus:border-slate-500 focus:outline-none focus:ring-2 focus:ring-slate-400"
        {...rest}
      />
      {hint ? (
        <p id={`${id}-hint`} className="text-xs text-slate-500">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={`${id}-error`} className="text-xs text-rose-700" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}

export function FormTextarea({ label, hint, error, required, ...rest }: TextareaProps) {
  const id = useId();
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-sm font-medium text-slate-700">
        {label}
        {required ? (
          <span aria-hidden="true" className="ml-1 text-rose-600">
            *
          </span>
        ) : null}
      </label>
      <textarea
        id={id}
        required={required}
        aria-required={required ? "true" : undefined}
        aria-invalid={error ? "true" : undefined}
        aria-describedby={describedBy(id, hint, error)}
        rows={4}
        className="block w-full rounded-md border border-slate-300 bg-white px-3 py-2 shadow-sm focus:border-slate-500 focus:outline-none focus:ring-2 focus:ring-slate-400"
        {...rest}
      />
      {hint ? (
        <p id={`${id}-hint`} className="text-xs text-slate-500">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={`${id}-error`} className="text-xs text-rose-700" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}
