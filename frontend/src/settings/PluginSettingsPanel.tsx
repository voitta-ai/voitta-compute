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
// The renderer walks the schema, reads current values via dotted-path
// lookup against the local settings cache, and writes back via the
// ``dotted`` patch field of ``saveSettings``. The status_probe block
// pulls connector status from /api/plugins and exposes a "Refresh tool
// list" button.

import { useEffect, useState } from "react";
import {
  getDotted,
  getSettings,
  saveSettings,
  subscribeSettings,
  type PublicSettings,
} from "../lib/settings";
import type {
  PluginConnectorStatus,
  PluginSchema,
  PluginSchemaField,
} from "./types";

interface Props {
  pluginName: string;
  schema: PluginSchema;
  connectors: PluginConnectorStatus[];
  backendOrigin: string;
  onRefreshConnectors: () => void;
  refreshBusy: boolean;
}

export default function PluginSettingsPanel({
  pluginName,
  schema,
  connectors,
  backendOrigin,
  onRefreshConnectors,
  refreshBusy,
}: Props) {
  const [snapshot, setSnapshot] = useState<PublicSettings>(getSettings);
  useEffect(() => subscribeSettings(setSnapshot), []);

  function valueOf(field: PluginSchemaField): unknown {
    const v = getDotted(snapshot as unknown as Record<string, unknown>, field.key);
    return v === undefined ? field.default : v;
  }

  async function patchField(field: PluginSchemaField, raw: unknown) {
    await saveSettings(backendOrigin, { dotted: { [field.key]: raw } });
  }

  // status_probe = "mcp:<connector_id>" — pull the matching connector.
  const probed = (() => {
    if (!schema.status_probe || !schema.status_probe.startsWith("mcp:")) return null;
    const cid = schema.status_probe.slice("mcp:".length);
    return connectors.find((c) => c.id === cid) || null;
  })();

  return (
    <div className="plugin-settings-panel">
      {schema.title && (
        <h3 style={{ margin: "0 0 12px", fontSize: 14 }}>{schema.title}</h3>
      )}
      {schema.fields.map((f) => (
        <FieldRow
          key={f.key}
          field={f}
          value={valueOf(f)}
          onChange={(v) => patchField(f, v)}
        />
      ))}

      {probed && (
        <ConnectorStatusRow
          conn={probed}
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
      <div style={{ marginTop: 12 }}>
        <label
          style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}
        >
          <input
            id={id}
            type="checkbox"
            checked={!!value}
            onChange={(e) => onChange(e.currentTarget.checked)}
          />
          <span>{field.label}</span>
        </label>
        {field.help && <p className="muted">{field.help}</p>}
      </div>
    );
  }
  if (field.type === "enum") {
    return (
      <div style={{ marginTop: 12 }}>
        <label htmlFor={id}>{field.label}</label>
        <select
          id={id}
          value={String(value ?? "")}
          onChange={(e) => onChange(e.currentTarget.value)}
        >
          {(field.options || []).map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
        {field.help && <p className="muted">{field.help}</p>}
      </div>
    );
  }
  // text | url | secret — same shape, ``secret`` masks visually via CSS.
  // We use type=text everywhere so Chrome doesn't pop the password manager.
  return (
    <div style={{ marginTop: 12 }}>
      <label htmlFor={id}>{field.label}</label>
      <input
        id={id}
        className={field.type === "secret" ? "secret" : undefined}
        type="text"
        spellCheck={false}
        autoComplete="off"
        autoCorrect="off"
        autoCapitalize="off"
        placeholder={field.placeholder}
        value={String(value ?? "")}
        onChange={(e) => onChange(e.currentTarget.value)}
      />
      {field.help && <p className="muted">{field.help}</p>}
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
  const more =
    conn.tool_names.length > 6 ? `, +${conn.tool_names.length - 6} more` : "";
  return (
    <div
      style={{
        marginTop: 16,
        padding: "10px 12px",
        border: "1px solid var(--voitta-border, #d1d5db)",
        borderRadius: 6,
        background: "var(--voitta-accent-tint, #f9fafb)",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span aria-hidden style={{ fontSize: 14 }}>{dot}</span>
        <strong>{label}</strong>
        {conn.status === "ok" && (
          <span className="muted" style={{ marginLeft: "auto", fontSize: 11 }}>
            {conn.tool_count} {conn.tool_count === 1 ? "tool" : "tools"}
          </span>
        )}
      </div>
      {conn.status === "ok" && preview && (
        <p className="muted" style={{ margin: "4px 0 0", fontSize: 11 }}>
          {preview}{more}
        </p>
      )}
      {conn.last_error && (
        <p className="muted" style={{ margin: "4px 0 0", color: "#b00020", fontSize: 11 }}>
          {conn.last_error}
        </p>
      )}
      <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
        <button
          type="button"
          onClick={onRefresh}
          disabled={busy}
          className="save-btn"
          style={{ background: "#6b7280", padding: "4px 10px", fontSize: 12 }}
        >
          {busy ? "Refreshing…" : "Refresh tool list"}
        </button>
        <span className="muted" style={{ fontSize: 11, alignSelf: "center" }}>
          plugin <code>{pluginName}</code>
        </span>
      </div>
    </div>
  );
}
