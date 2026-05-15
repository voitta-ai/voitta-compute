// Schema-driven settings panel for plugins.
//
// A plugin declares ``settings_schema`` in its manifest.json:
//
//   {
//     "title": "...",
//     "fields": [
//       { "key": "plugins.<name>.<...>",
//         "type": "text"|"url"|"secret"|"bool"|"enum",
//         "label": "...",
//         "default": "...",
//         "help": "...",
//         "placeholder": "...",
//         "options": [{"value":"a","label":"A"}, ...]   // enum only
//       },
//       ...
//     ],
//     "status_probe": "mcp:<connector_id>"   // optional
//   }
//
// Core walks the schema and renders the form. The key field is a
// dot-path into the settings blob (``plugins.<name>.<...>``) — same
// path the backend reads. Save dispatches via ``setDotted`` /
// ``saveSettings``, so a single Save button at the parent level
// persists every field.
//
// Plugins that need bespoke UI (Google OAuth dance, etc.) opt out
// by shipping their own ``frontend/settings-panel.tsx`` — the parent
// SettingsView prefers it over this renderer.

import { useEffect, useState } from "preact/hooks";
import {
  getDotted,
  loadSettings,
  saveSettings,
  setDotted,
  subscribeSettings,
  type Settings,
} from "../lib/settings";

export interface PluginSchemaField {
  key: string;
  type: "text" | "url" | "secret" | "bool" | "enum";
  label: string;
  default?: unknown;
  help?: string;
  placeholder?: string;
  options?: { value: string; label: string }[];
}

export interface PluginSchema {
  title?: string;
  fields: PluginSchemaField[];
  status_probe?: string;            // "mcp:<connector_id>"
}

export interface PluginConnectorStatus {
  id: string;
  status: "ok" | "unauth" | "unreachable" | "not_configured" | "unknown";
  last_error: string | null;
  tool_count: number;
  tool_names: string[];
}

interface Props {
  pluginName: string;
  schema: PluginSchema;
  connectors: PluginConnectorStatus[];
  backendOrigin: string;
  onRefreshConnectors: () => void;
  refreshBusy: boolean;
}

export function PluginSettingsPanel(props: Props) {
  const { pluginName, schema, connectors, onRefreshConnectors, refreshBusy } = props;

  // Local snapshot so the inputs feel responsive without a re-render
  // round-trip. Settings is the source of truth; we read once on mount
  // and listen for outside changes.
  const [snapshot, setSnapshot] = useState<Settings>(() => loadSettings());
  useEffect(() => subscribeSettings(setSnapshot), []);

  function valueOf(field: PluginSchemaField): unknown {
    const v = getDotted(snapshot, field.key);
    return v === undefined ? field.default : v;
  }

  function patchField(field: PluginSchemaField, raw: unknown) {
    // setDotted operates on the cached settings — saveSettings then
    // notifies subscribers (us included) so other open views update.
    const next = setDotted(loadSettings(), field.key, raw);
    saveSettings(next);
  }

  // status_probe = "mcp:<connector_id>" — pull the connector whose id matches.
  const probedConnector = (() => {
    if (!schema.status_probe || !schema.status_probe.startsWith("mcp:")) return null;
    const cid = schema.status_probe.slice("mcp:".length);
    return connectors.find((c) => c.id === cid) || null;
  })();

  return (
    <div class="plugin-settings-panel">
      {schema.title && <h3 style={{ margin: "0 0 12px", fontSize: "14px" }}>{schema.title}</h3>}
      {schema.fields.map((f) => (
        <FieldRow key={f.key} field={f} value={valueOf(f)} onChange={(v) => patchField(f, v)} />
      ))}

      {probedConnector && (
        <ConnectorStatusRow
          conn={probedConnector}
          pluginName={pluginName}
          onRefresh={onRefreshConnectors}
          busy={refreshBusy}
        />
      )}
    </div>
  );
}

function FieldRow({
  field,
  value,
  onChange,
}: {
  field: PluginSchemaField;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const id = `pf-${field.key.replace(/[^a-zA-Z0-9]+/g, "-")}`;
  if (field.type === "bool") {
    return (
      <div style={{ marginTop: "12px" }}>
        <label
          style={{ display: "flex", alignItems: "center", gap: "8px", cursor: "pointer" }}
        >
          <input
            id={id}
            type="checkbox"
            checked={!!value}
            onChange={(e) => onChange((e.currentTarget as HTMLInputElement).checked)}
          />
          <span>{field.label}</span>
        </label>
        {field.help && <p class="muted">{field.help}</p>}
      </div>
    );
  }
  if (field.type === "enum") {
    return (
      <div style={{ marginTop: "12px" }}>
        <label htmlFor={id}>{field.label}</label>
        <select
          id={id}
          value={String(value ?? "")}
          onChange={(e) => onChange((e.currentTarget as HTMLSelectElement).value)}
        >
          {(field.options || []).map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
        {field.help && <p class="muted">{field.help}</p>}
      </div>
    );
  }

  // text | url | secret — same shape, slightly different hints.
  // ``secret`` uses type=text + visually-masked CSS class (same as the
  // global key fields) so Chrome doesn't pop the password-manager prompt.
  const inputClass = field.type === "secret" ? "secret" : undefined;
  const inputType = "text"; // see above
  return (
    <div style={{ marginTop: "12px" }}>
      <label htmlFor={id}>{field.label}</label>
      <input
        id={id}
        class={inputClass}
        type={inputType}
        spellcheck={false}
        autocomplete="off"
        autocorrect="off"
        autocapitalize="off"
        data-lpignore="true"
        data-1p-ignore="true"
        data-form-type="other"
        placeholder={field.placeholder}
        value={String(value ?? "")}
        onInput={(e) => onChange((e.currentTarget as HTMLInputElement).value)}
      />
      {field.help && <p class="muted">{field.help}</p>}
    </div>
  );
}

function ConnectorStatusRow({
  conn,
  pluginName,
  onRefresh,
  busy,
}: {
  conn: PluginConnectorStatus;
  pluginName: string;
  onRefresh: () => void;
  busy: boolean;
}) {
  const dot = STATUS_DOTS[conn.status] ?? "•";
  const label = STATUS_LABELS[conn.status] ?? conn.status;
  // Bound to ~6 names so a 14-tool server doesn't push the Save button off-screen.
  const preview = conn.tool_names.slice(0, 6).join(", ");
  const more = conn.tool_names.length > 6
    ? `, +${conn.tool_names.length - 6} more`
    : "";
  return (
    <div
      style={{
        marginTop: "16px",
        padding: "10px 12px",
        border: "1px solid var(--voitta-border)",
        borderRadius: "6px",
        background: "var(--voitta-accent-tint, #f9fafb)",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
        <span aria-hidden style={{ fontSize: "14px" }}>{dot}</span>
        <strong>{label}</strong>
        {conn.status === "ok" && (
          <span class="muted" style={{ marginLeft: "auto", fontSize: "11px" }}>
            {conn.tool_count} {conn.tool_count === 1 ? "tool" : "tools"}
          </span>
        )}
      </div>
      {conn.status === "ok" && preview && (
        <p class="muted" style={{ margin: "4px 0 0", fontSize: "11px" }}>
          {preview}{more}
        </p>
      )}
      {conn.last_error && (
        <p class="muted" style={{ margin: "4px 0 0", color: "#b00020", fontSize: "11px" }}>
          {conn.last_error}
        </p>
      )}
      <div style={{ marginTop: "8px", display: "flex", gap: "8px" }}>
        <button
          type="button"
          onClick={onRefresh}
          disabled={busy}
          class="save-btn"
          style={{ background: "#6b7280", padding: "4px 10px", fontSize: "12px" }}
        >
          {busy ? "Refreshing…" : "Refresh tool list"}
        </button>
        <span class="muted" style={{ fontSize: "11px", alignSelf: "center" }}>
          plugin <code>{pluginName}</code>
        </span>
      </div>
    </div>
  );
}

const STATUS_DOTS: Record<string, string> = {
  ok: "●",
  unauth: "✕",
  unreachable: "⚠",
  not_configured: "○",
  unknown: "·",
};
const STATUS_LABELS: Record<string, string> = {
  ok: "Connected",
  unauth: "Unauthorized — token rejected",
  unreachable: "Unreachable",
  not_configured: "Not configured",
  unknown: "Not probed yet",
};
