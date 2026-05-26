// Shared types between the SettingsView, plugin panels, and the
// /api/plugins fetcher.

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
  // ``mcp:<connector_id>`` — pulls the connector with this id from the
  // plugin's ``mcp_connectors`` list and renders a status badge below
  // the form.
  status_probe?: string;
}

export interface PluginConnectorStatus {
  id: string;
  status: "ok" | "unauth" | "unreachable" | "not_configured" | "unknown";
  last_error: string | null;
  tool_count: number;
  tool_names: string[];
}

export interface PluginInfo {
  name: string;
  version?: string | null;
  description?: string | null;
  agent_name?: string | null;
  host_patterns: string[];
  settings_schema: PluginSchema | null;
  settings_panel: "schema" | "custom";
  mcp_connectors: PluginConnectorStatus[];
}

export interface CustomPanelProps {
  pluginName: string;
  backendOrigin: string;
}
