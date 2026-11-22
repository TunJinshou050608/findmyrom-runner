"""Single source of truth loader for config/official_sources.yaml (TG Pipeline v2)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "official_sources.yaml"


def _norm_key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").strip().lower())


def _norm_channel(username: str | None) -> str:
    return (username or "").strip().lower().lstrip("@")


class OfficialSourcesRegistry:
    """Loaded registry from config/official_sources.yaml for Layer 1 & Layer 3 filtering."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.raw = data
        self.schema_version = int(data.get("schema_version") or 1)
        self.last_verified = str(data.get("last_verified") or "")

        blocklist = data.get("tg_layer1_channel_blocklist") or {}
        self.layer1_channels: dict[str, dict[str, Any]] = {}
        for category, entries in blocklist.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                uname = _norm_channel(entry.get("username"))
                if uname:
                    self.layer1_channels[uname] = {**entry, "category": category, "username": uname}

        self.oem_stock_domains: tuple[str, ...] = tuple(
            str(d).strip().lower() for d in (data.get("oem_stock_domains") or []) if str(d).strip()
        )

        self.projects: dict[str, dict[str, Any]] = {}
        self.archived_projects: dict[str, dict[str, Any]] = {}
        self._alias_index: dict[str, tuple[str, dict[str, Any]]] = {}

        for pid, cfg in (data.get("projects") or {}).items():
            if not isinstance(cfg, dict):
                continue
            entry = {**cfg, "id": pid, "archived": False}
            self.projects[pid] = entry
            for candidate in (pid, cfg.get("display_name"), *(cfg.get("aliases") or [])):
                nk = _norm_key(candidate)
                if nk:
                    self._alias_index[nk] = (pid, entry)

        for pid, cfg in (data.get("archived_or_unmanaged") or {}).items():
            if not isinstance(cfg, dict):
                continue
            entry = {**cfg, "id": pid, "archived": True}
            self.archived_projects[pid] = entry
            for candidate in (pid, cfg.get("display_name"), *(cfg.get("aliases") or [])):
                nk = _norm_key(candidate)
                if nk and nk not in self._alias_index:
                    self._alias_index[nk] = (pid, entry)

    def is_layer1_blocked_channel(self, channel_username: str | None) -> tuple[bool, dict[str, Any] | None]:
        """Layer 1 pure metadata channel check (no body-text regex)."""
        uname = _norm_channel(channel_username)
        if not uname:
            return False, None
        info = self.layer1_channels.get(uname)
        return (info is not None), info

    def is_oem_stock_domain(self, url: str | None) -> bool:
        if not url:
            return False
        try:
            host = (urlsplit(url.strip()).hostname or "").lower()
        except ValueError:
            return False
        if not host:
            return False
        return any(host == dom or host.endswith("." + dom) for dom in self.oem_stock_domains)

    def all_external_links_are_oem_stock(self, links: list[str] | list[dict[str, str]]) -> bool:
        external_hosts = []
        for item in links or []:
            url = item.get("url") if isinstance(item, dict) else str(item)
            if not url:
                continue
            try:
                host = (urlsplit(url.strip()).hostname or "").lower()
            except ValueError:
                continue
            if not host or host in ("t.me", "telegram.me", "telegram.dog"):
                continue
            external_hosts.append(url)
        return bool(external_hosts) and all(self.is_oem_stock_domain(u) for u in external_hosts)

    def match_project(self, project_name: str | None) -> dict[str, Any] | None:
        nk = _norm_key(project_name)
        if not nk:
            return None
        hit = self._alias_index.get(nk)
        return hit[1] if hit else None

    def should_exclude_official_project(
        self,
        project_name: str | None,
        release_status: str | None,
        resource_kind: str | None = None,
    ) -> tuple[bool, str | None]:
        """Layer 3 semantic exclusion: only exclude if release_status == 'official' AND
        the matched active project has exclude_official_in_tg == True.
        """
        status = (release_status or "").strip().lower()
        if status != "official":
            return False, None
        matched = self.match_project(project_name)
        if not matched:
            return False, None
        if matched.get("exclude_official_in_tg") is not True:
            return False, None
        expected_kind = matched.get("resource_kind")
        if resource_kind and expected_kind and resource_kind != expected_kind:
            return False, None
        return True, f"excluded_official_project:{matched['id']}"


@lru_cache(maxsize=4)
def load_official_registry(path: str | Path | None = None) -> OfficialSourcesRegistry:
    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    return OfficialSourcesRegistry(data)
