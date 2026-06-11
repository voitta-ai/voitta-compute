"""User-extendable plugin activation hosts.

Plugin manifests pin ``host_patterns`` at authoring time (e.g.
``["enterprise.voitta.ai"]``), but users run equivalent apps on hosts
the author never knew about — say, a local voitta-rag-enterprise on
``127.0.0.1:8756``. The Settings → Plugins tab lets the user append
their own hosts per plugin; they live in the settings blob at

    plugins.<plugin_name>.extra_hosts = ["127.0.0.1:8756", "rag.corp.example"]

and are read LIVE on every match, so adding one takes effect on the
next chat turn with no restart.

Entries may carry a port. Manifest patterns are matched on the bare
hostname (port stripped), but a user entry with an explicit port only
matches that exact port. The distinction matters for the localhost
case: ``127.0.0.1`` would activate the plugin on EVERY local app,
``127.0.0.1:8756`` on just the one.
"""

from __future__ import annotations

import logging

from app.services import user_settings

logger = logging.getLogger(__name__)


def extra_hosts_map() -> dict[str, list[str]]:
    """``{plugin_name: [host_entries]}`` from the live settings blob.

    Reads the settings file once — callers matching many specs in a
    loop should call this once per request, not once per spec.
    """
    try:
        blob = user_settings.read()
    except Exception:
        logger.exception("extra_hosts_map: settings unreadable")
        return {}
    plugins = blob.get("plugins")
    if not isinstance(plugins, dict):
        return {}
    out: dict[str, list[str]] = {}
    for name, conf in plugins.items():
        if not isinstance(conf, dict):
            continue
        raw = conf.get("extra_hosts")
        if not isinstance(raw, list):
            continue
        entries = [
            e.strip().lower() for e in raw if isinstance(e, str) and e.strip()
        ]
        if entries:
            out[str(name)] = entries
    return out


def matches(host: str | None, entries: list[str]) -> bool:
    """Does ``host`` (possibly ``hostname:port``) match a user entry?

    Entry without a port: same bare-hostname rule the manifest patterns
    use (exact, or strict suffix so ``ebay.com`` covers ``www.ebay.com``).
    Entry with a port: hostname must match AND the page's port must
    equal it.
    """
    if not host or not entries:
        return False
    hostname, _, port = host.lower().partition(":")
    hostname = hostname.rstrip(".")
    if not hostname:
        return False
    for entry in entries:
        e_host, _, e_port = entry.partition(":")
        e_host = e_host.rstrip(".")
        if not e_host:
            continue
        if e_port and e_port != port:
            continue
        if hostname == e_host or hostname.endswith("." + e_host):
            return True
    return False
