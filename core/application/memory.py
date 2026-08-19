# -*- coding: utf-8 -*-
"""Scoped-memory application service used by the integration facade."""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Tuple

from core.access_policy import AccessNarrowing, PrincipalEnvelope


class MemoryApplicationService:
    """Default implementation for scoped memory operations."""

    def __init__(
        self,
        wiki_write: Callable[..., Dict],
        wiki_search: Callable[..., Tuple[List[Dict], Dict[str, int]]],
    ):
        self._wiki_write = wiki_write
        self._wiki_search = wiki_search

    @staticmethod
    def infer_type_from_path(page_path: str) -> str:
        """Infer knowledge type from a wiki page path."""
        if "/" in page_path:
            return page_path.split("/")[0]
        return "00-Inbox"

    @staticmethod
    def scope_slug(value: str) -> str:
        """Return a filesystem-friendly ASCII slug for scope pages."""
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip().lower())
        slug = slug.strip("-._")
        return slug or "untitled"

    def scope_page_path(
        self,
        scope: str,
        title: str,
        page_path: str = "",
        scope_name: str = "",
    ) -> str:
        base = f"scopes/{scope}"
        if scope_name:
            base = f"{base}/{self.scope_slug(scope_name)}"
        if page_path:
            safe_path = page_path.lstrip("/")
            if safe_path.startswith(f"{base}/"):
                return safe_path
            return f"{base}/{safe_path}"
        return f"{base}/{self.scope_slug(title)}.md"

    def memory_write_project(
        self,
        title: str,
        content: str,
        project: str = "",
        page_path: str = "",
        frontmatter: Dict | None = None,
        *,
        principal: PrincipalEnvelope,
    ) -> Dict:
        """Write project-scoped memory."""
        project_name = project or "default"
        frontmatter_data = dict(frontmatter or {})
        frontmatter_data.update(
            {
                "title": title,
                "scope": "project",
                "project": project_name,
                "source": "memory_write_project",
            }
        )
        tags = list(frontmatter_data.get("tags", []) or [])
        for tag in ("scope/project", f"project/{self.scope_slug(project_name)}"):
            if tag not in tags:
                tags.append(tag)
        frontmatter_data["tags"] = tags
        path = self.scope_page_path("project", title, page_path, project_name)
        result = self._wiki_write(
            path,
            content,
            frontmatter_data,
            principal=principal,
            project=project_name,
        )
        result["scope"] = "project"
        result["project"] = project_name
        return result

    def memory_write_framework(
        self,
        title: str,
        content: str,
        framework: str = "",
        page_path: str = "",
        frontmatter: Dict | None = None,
        *,
        principal: PrincipalEnvelope,
    ) -> Dict:
        """Write framework-scoped memory."""
        framework_name = framework or "general"
        frontmatter_data = dict(frontmatter or {})
        frontmatter_data.update(
            {
                "title": title,
                "scope": "framework",
                "framework": framework_name,
                "source": "memory_write_framework",
            }
        )
        tags = list(frontmatter_data.get("tags", []) or [])
        for tag in ("scope/framework", f"framework/{self.scope_slug(framework_name)}"):
            if tag not in tags:
                tags.append(tag)
        frontmatter_data["tags"] = tags
        path = self.scope_page_path("framework", title, page_path, framework_name)
        result = self._wiki_write(
            path,
            content,
            frontmatter_data,
            principal=principal,
        )
        result["scope"] = "framework"
        result["framework"] = framework_name
        return result

    def memory_write_global(
        self,
        title: str,
        content: str,
        page_path: str = "",
        frontmatter: Dict | None = None,
        *,
        principal: PrincipalEnvelope,
    ) -> Dict:
        """Write global-scoped memory."""
        frontmatter_data = dict(frontmatter or {})
        frontmatter_data.update(
            {
                "title": title,
                "scope": "global",
                "source": "memory_write_global",
            }
        )
        tags = list(frontmatter_data.get("tags", []) or [])
        if "scope/global" not in tags:
            tags.append("scope/global")
        frontmatter_data["tags"] = tags
        path = self.scope_page_path("global", title, page_path)
        result = self._wiki_write(
            path,
            content,
            frontmatter_data,
            principal=principal,
        )
        result["scope"] = "global"
        return result

    def memory_search(
        self,
        query: str,
        scope: str = "all",
        limit: int = 5,
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
    ) -> Dict:
        """Search wiki results and constrain them to a memory scope."""
        allowed = {"all", "project", "framework", "global"}
        if scope not in allowed:
            return {
                "success": False,
                "error": f"scope must be one of {sorted(allowed)}",
            }
        results, access_summary = self._wiki_search(
            query,
            limit=limit,
            principal=principal,
            narrowing=narrowing,
        )
        result: Dict[str, Any] = {
            "success": True,
            "query": query,
            "results": results,
            "count": len(results),
            "access_filter": access_summary,
        }
        if scope == "all":
            result["scope"] = scope
            return result

        prefix = f"scopes/{scope}/"
        filtered = [
            item for item in results if str(item.get("page_id", "")).startswith(prefix)
        ]
        result["results"] = filtered[:limit]
        result["count"] = len(result["results"])
        result["scope"] = scope
        return result
