import React, { useState } from "react";

interface ConfigSchemaFormProps {
  schema: Record<string, unknown>;
  initialConfig?: Record<string, unknown>;
  onChange: (config: Record<string, unknown>) => void;
  disabledProperties?: string[];
  disabled?: boolean;
}

export function ConfigSchemaForm({
  schema,
  initialConfig = {},
  onChange,
  disabledProperties = [],
  disabled = false,
}: ConfigSchemaFormProps) {
  const [useRawJson, setUseRawJson] = useState(false);
  const [config, setConfig] = useState<Record<string, unknown>>(initialConfig);
  const [rawJsonText, setRawJsonText] = useState(() => JSON.stringify(initialConfig, null, 2));
  const [jsonError, setJsonError] = useState<string | null>(null);

  const properties = (schema.properties as Record<string, Record<string, unknown>> | undefined) || {};
  const hasProperties = Object.keys(properties).length > 0;

  function handleFieldChange(key: string, value: unknown) {
    const updated = { ...config, [key]: value };
    setConfig(updated);
    setRawJsonText(JSON.stringify(updated, null, 2));
    onChange(updated);
  }

  function handleJsonTextChange(event: React.ChangeEvent<HTMLTextAreaElement>) {
    const text = event.target.value;
    setRawJsonText(text);
    try {
      const parsed = JSON.parse(text);
      if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
        setConfig(parsed as Record<string, unknown>);
        setJsonError(null);
        onChange(parsed as Record<string, unknown>);
      } else {
        setJsonError("Configuration must be a JSON object.");
      }
    } catch (e: unknown) {
      setJsonError((e as Error).message);
    }
  }

  if (!hasProperties) {
    return (
      <div className="space-y-2">
        <label className="block text-sm font-medium text-gray-700 dark:text-gray-300">
          Package Configuration (JSON)
        </label>
        <textarea
          value={rawJsonText}
          onChange={handleJsonTextChange}
          disabled={disabled}
          rows={4}
          className="w-full font-mono text-sm p-2 border rounded dark:bg-gray-800 dark:border-gray-700"
          placeholder="{}"
        />
        {jsonError && <p className="text-xs text-red-500">{jsonError}</p>}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex justify-between items-center">
        <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
          Configuration Settings
        </span>
        <button
          type="button"
          onClick={() => setUseRawJson(!useRawJson)}
          className="text-xs text-blue-600 dark:text-blue-400 hover:underline"
        >
          {useRawJson ? "Switch to Form Mode" : "Switch to Raw JSON"}
        </button>
      </div>

      {useRawJson ? (
        <div className="space-y-2">
          <textarea
            value={rawJsonText}
            onChange={handleJsonTextChange}
            disabled={disabled}
            rows={6}
            className="w-full font-mono text-sm p-2 border rounded dark:bg-gray-800 dark:border-gray-700"
          />
          {jsonError && <p className="text-xs text-red-500">{jsonError}</p>}
        </div>
      ) : (
        <div className="space-y-3">
          {Object.entries(properties).map(([key, propSchema]) => {
            const isImmutable = propSchema["x-nervos-immutable"] === true;
            const isFieldDisabled = disabled || disabledProperties.includes(key);
            const propType = propSchema.type as string;
            const description = propSchema.description as string | undefined;
            const title = (propSchema.title as string) || key;
            const currentValue = config[key] !== undefined ? config[key] : propSchema.default;

            if (propType === "boolean") {
              return (
                <div key={key} className="flex items-start space-x-2">
                  <input
                    type="checkbox"
                    id={`prop-${key}`}
                    checked={Boolean(currentValue)}
                    onChange={(e) => handleFieldChange(key, e.target.checked)}
                    disabled={isFieldDisabled}
                    className="mt-1"
                  />
                  <div>
                    <label htmlFor={`prop-${key}`} className="text-sm font-medium text-gray-900 dark:text-gray-100">
                      {title}
                      {isImmutable && <span className="ml-1 text-xs text-amber-500 font-normal">(Immutable)</span>}
                    </label>
                    {description && <p className="text-xs text-gray-500 dark:text-gray-400">{description}</p>}
                  </div>
                </div>
              );
            }

            if (propType === "integer" || propType === "number") {
              return (
                <div key={key} className="space-y-1">
                  <label htmlFor={`prop-${key}`} className="block text-sm font-medium text-gray-900 dark:text-gray-100">
                    {title}
                    {isImmutable && <span className="ml-1 text-xs text-amber-500 font-normal">(Immutable)</span>}
                  </label>
                  <input
                    type="number"
                    id={`prop-${key}`}
                    value={currentValue !== undefined ? Number(currentValue) : ""}
                    onChange={(e) => handleFieldChange(key, e.target.value === "" ? undefined : Number(e.target.value))}
                    disabled={isFieldDisabled}
                    className="w-full p-2 border rounded text-sm dark:bg-gray-800 dark:border-gray-700"
                  />
                  {description && <p className="text-xs text-gray-500 dark:text-gray-400">{description}</p>}
                </div>
              );
            }

            return (
              <div key={key} className="space-y-1">
                <label htmlFor={`prop-${key}`} className="block text-sm font-medium text-gray-900 dark:text-gray-100">
                  {title}
                  {isImmutable && <span className="ml-1 text-xs text-amber-500 font-normal">(Immutable)</span>}
                </label>
                <input
                  type="text"
                  id={`prop-${key}`}
                  value={currentValue !== undefined ? String(currentValue) : ""}
                  onChange={(e) => handleFieldChange(key, e.target.value)}
                  disabled={isFieldDisabled}
                  className="w-full p-2 border rounded text-sm dark:bg-gray-800 dark:border-gray-700"
                />
                {description && <p className="text-xs text-gray-500 dark:text-gray-400">{description}</p>}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
