// Settings — tabbed pane.
//
// Tabs:
//   - "Global"          → core fields (provider, key, model, layout, …)
//   - "<plugin>"        → one tab per loaded plugin that declares either
//                          a ``settings_schema`` (declarative) or a
//                          ``settings-panel.tsx`` (custom Preact).
//
// Plugin tab discovery is driven by GET /api/plugins — the same endpoint
// the SettingsTabs UI uses to pull MCP-connector status badges + the
// declarative schema. Custom panels are bundled via import.meta.glob;
// each plugin's ``frontend/settings-panel.tsx`` exports a default Preact
// component as ``({pluginName, backendOrigin}) => JSX``.
//
// One Save button at the bottom of the pane writes the entire
// settings blob (PUT /api/settings) regardless of which tab is active.
// Plugin fields are stored under ``plugins.<name>.<...>``; ``setDotted``
// in lib/settings.ts builds the nested path.

import { useEffect, useMemo, useState } from "preact/hooks";
import { GlobalSettings } from "./GlobalSettings";
import {
  PluginSettingsPanel,
  type PluginConnectorStatus,
  type PluginSchema,
} from "./PluginSettingsPanel";
import {
  loadSettings,
  saveSettings,
  subscribeSettings,
  type Settings,
} from "../lib/settings";

interface Props {
  backendOrigin: string;
}

interface PluginInfo {
  name: string;
  version?: string;
  description?: string;
  agent_name?: string;
  host_patterns: string[];
  settings_schema?: PluginSchema | null;
  settings_panel: "schema" | "custom";
  mcp_connectors: PluginConnectorStatus[];
}

// Eager-globbed custom settings panels. Same mechanism the widget uses
// for plugin frontends. The glob result is keyed by file path, so we
// walk it to find ``plugins/<name>/frontend/settings-panel.tsx`` →
// default export.
const CUSTOM_PANEL_MODULES = import.meta.glob<{
  default: (props: { pluginName: string; backendOrigin: string }) => preact.JSX.Element;
}>("../../../plugins/*/frontend/settings-panel.tsx", { eager: true });

function findCustomPanel(pluginName: string) {
  // Path shape: "../../../plugins/<name>/frontend/settings-panel.tsx"
  const needle = `/plugins/${pluginName}/frontend/settings-panel.tsx`;
  for (const path in CUSTOM_PANEL_MODULES) {
    if (path.endsWith(needle)) {
      return CUSTOM_PANEL_MODULES[path].default;
    }
  }
  return null;
}

export function SettingsView({ backendOrigin }: Props) {
  // Draft is the live settings blob; saveSettings persists.
  //
  // We don't keep a separate "savedSnapshot" + dirty flag anymore. The
  // store's own saveSettings is fire-and-forget against the backend
  // and updates the in-memory cache synchronously, so a single Save
  // button below saves the draft. The schema renderer ALSO calls
  // saveSettings on input (per-field auto-save) — both call paths
  // converge on the same store.
  //
  // For the global form we keep an explicit draft + manual Save action
  // because users expect a save button next to API keys.
  const [draft, setDraft] = useState<Settings>(() => loadSettings());
  const [serverSnapshot, setServerSnapshot] = useState<Settings>(() => loadSettings());
  const [status, setStatus] = useState<{ text: string; isError?: boolean } | null>(null);
  const [plugins, setPlugins] = useState<PluginInfo[]>([]);
  const [activeTab, setActiveTab] = useState<string>("global");
  const [refreshBusy, setRefreshBusy] = useState<Record<string, boolean>>({});

  useEffect(() => {
    // Subscribe so external saves (PluginSettingsPanel auto-save) update
    // the draft too. We re-seed the global form draft from the store
    // whenever the store changes — assumes plugin-tab edits don't
    // conflict with in-progress global-tab edits. If both tabs were
    // dirty simultaneously the Save button would win; that's a
    // theoretical case (the user can only be in one tab at a time).
    return subscribeSettings((s) => {
      setServerSnapshot(s);
      setDraft(s);
    });
  }, []);

  // Pull the plugin catalog once on mount. Refresh-on-tab-change would
  // be wasteful (the list doesn't change mid-session), but explicit
  // refresh buttons re-pull a single plugin.
  useEffect(() => {
    let cancelled = false;
    fetch(`${backendOrigin}/api/plugins`, { credentials: "include" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`status ${r.status}`))))
      .then((body: { plugins: PluginInfo[] }) => {
        if (!cancelled && body && Array.isArray(body.plugins)) {
          setPlugins(body.plugins);
        }
      })
      .catch((err) => {
        // Non-fatal — Settings pane still works without plugin info.
        console.warn("[settings] /api/plugins fetch failed:", err);
      });
    return () => {
      cancelled = true;
    };
  }, [backendOrigin]);

  const dirty = useMemo(
    () => !shallowEqual(draft as unknown as Record<string, unknown>, serverSnapshot as unknown as Record<string, unknown>),
    [draft, serverSnapshot],
  );

  function patch(p: Partial<Settings>) {
    setDraft((d) => ({ ...d, ...p }));
  }

  function onSave() {
    const next = saveSettings(draft);
    setServerSnapshot(next);
    setDraft(next);
    setStatus({ text: "Saved." });
    setTimeout(() => setStatus(null), 2200);
  }

  async function onRefreshPlugin(name: string) {
    setRefreshBusy((m) => ({ ...m, [name]: true }));
    try {
      const r = await fetch(`${backendOrigin}/api/plugins/${encodeURIComponent(name)}/refresh`, {
        method: "POST",
        credentials: "include",
      });
      if (!r.ok) throw new Error(`status ${r.status}`);
      const body = (await r.json()) as { plugin: string; connectors: PluginConnectorStatus[] };
      setPlugins((ps) =>
        ps.map((p) => (p.name === body.plugin ? { ...p, mcp_connectors: body.connectors } : p))
      );
    } catch (err) {
      setStatus({ text: `Refresh failed: ${String(err)}`, isError: true });
      setTimeout(() => setStatus(null), 4000);
    } finally {
      setRefreshBusy((m) => {
        const copy = { ...m };
        delete copy[name];
        return copy;
      });
    }
  }

  // Plugin tabs filter: a plugin gets a tab if it declares EITHER a
  // settings_schema or a custom panel file. Plugins with neither don't
  // need configuration UI — no tab. ``ebay`` today falls in this
  // bucket (no auth, no per-user config).
  const tabbedPlugins = plugins.filter(
    (p) => p.settings_schema || findCustomPanel(p.name),
  );

  return (
    <section class="view view-settings">
      <TabStrip
        plugins={tabbedPlugins}
        active={activeTab}
        onChange={setActiveTab}
      />

      <div class="settings-tab-body" style={{ marginTop: "12px" }}>
        {activeTab === "global" && (
          <GlobalSettings
            backendOrigin={backendOrigin}
            draft={draft}
            patch={patch}
          />
        )}
        {activeTab !== "global" && (
          <PluginTabBody
            pluginName={activeTab}
            plugin={tabbedPlugins.find((p) => p.name === activeTab)}
            backendOrigin={backendOrigin}
            onRefresh={() => onRefreshPlugin(activeTab)}
            refreshBusy={!!refreshBusy[activeTab]}
          />
        )}
      </div>

      <LoadedPluginsFooter plugins={plugins} />

      <p class="scope">
        Stored on the local backend at <code>~/.config/voitta-bookmarklet/settings.json</code> —
        shared across every host the bookmarklet runs on.
      </p>

      <div class="actions">
        <button
          class="save-btn"
          type="button"
          disabled={!dirty}
          onClick={onSave}
          title={dirty ? "Save changes" : "Nothing to save"}
        >
          Save
        </button>
        {status && (
          <span class={`status${status.isError ? " err" : ""}`} role="status" aria-live="polite">
            {status.text}
          </span>
        )}
      </div>
    </section>
  );
}

function TabStrip({
  plugins,
  active,
  onChange,
}: {
  plugins: PluginInfo[];
  active: string;
  onChange: (name: string) => void;
}) {
  // Tab strip is light DOM — no fancy roving-tabindex / ARIA tabs
  // pattern. Each tab is a button; the active one carries an
  // ``is-active`` class for CSS to style. The shipping styles.css
  // didn't have tab rules so we inline a minimal styling that respects
  // the existing CSS vars (--voitta-border / --voitta-accent-tint).
  const tabs: { id: string; label: string }[] = [
    { id: "global", label: "Global" },
    ...plugins.map((p) => ({ id: p.name, label: p.agent_name || p.name })),
  ];
  return (
    <div class="settings-tabs" role="tablist">
      {tabs.map((t) => {
        const isActive = t.id === active;
        return (
          <button
            key={t.id}
            type="button"
            role="tab"
            class={`settings-tab${isActive ? " is-active" : ""}`}
            aria-selected={isActive}
            onClick={() => onChange(t.id)}
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}

function PluginTabBody({
  pluginName,
  plugin,
  backendOrigin,
  onRefresh,
  refreshBusy,
}: {
  pluginName: string;
  plugin: PluginInfo | undefined;
  backendOrigin: string;
  onRefresh: () => void;
  refreshBusy: boolean;
}) {
  if (!plugin) {
    return <p class="muted">Plugin {pluginName} no longer loaded; switch to another tab.</p>;
  }
  // Custom panel beats schema if both are present — that's the
  // "opt-out" escape hatch documented in the design.
  const CustomPanel = findCustomPanel(plugin.name);
  if (CustomPanel) {
    return <CustomPanel pluginName={plugin.name} backendOrigin={backendOrigin} />;
  }
  if (plugin.settings_schema) {
    return (
      <PluginSettingsPanel
        pluginName={plugin.name}
        schema={plugin.settings_schema}
        connectors={plugin.mcp_connectors}
        backendOrigin={backendOrigin}
        onRefreshConnectors={onRefresh}
        refreshBusy={refreshBusy}
      />
    );
  }
  return <p class="muted">This plugin has no configurable settings.</p>;
}

function LoadedPluginsFooter({ plugins }: { plugins: PluginInfo[] }) {
  if (plugins.length === 0) return null;
  return (
    <details style={{ marginTop: "16px" }}>
      <summary class="muted" style={{ cursor: "pointer", fontSize: "12px" }}>
        Loaded plugins ({plugins.length})
      </summary>
      <ul style={{ margin: "6px 0 0", padding: "0 0 0 16px", fontSize: "12px" }}>
        {plugins.map((p) => (
          <li key={p.name}>
            <code>{p.name}</code>
            {p.version && <span class="muted"> v{p.version}</span>}
            {p.host_patterns.length > 0 && (
              <span class="muted"> · {p.host_patterns.join(", ")}</span>
            )}
            {p.mcp_connectors.length > 0 && (
              <span class="muted">
                {" · "}MCP: {p.mcp_connectors.map((c) => `${c.id} (${c.status})`).join(", ")}
              </span>
            )}
          </li>
        ))}
      </ul>
    </details>
  );
}

function shallowEqual<T extends Record<string, unknown>>(a: T, b: T): boolean {
  // Deep-ish: nested objects compared by reference. The settings store
  // returns new object references after every patch, so reference
  // equality is the right granularity for the dirty flag.
  const keys = Object.keys(a) as (keyof T)[];
  if (keys.length !== Object.keys(b).length) return false;
  return keys.every((k) => a[k] === b[k]);
}
