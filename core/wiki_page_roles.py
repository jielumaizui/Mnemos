"""Wiki page role classification shared by metrics and readiness checks."""

from __future__ import annotations

import re
from pathlib import Path

SOURCE_EXEMPT_ROOTS = frozenset(
    {
        "L2.4-KG",
        "L3-Observations",
        "L4-Reflections",
        "L5-Feedback",
        "07-Shadow",
        "08-Reminders",
        "99-Archive",
    }
)
EXPLICIT_DERIVED_PAGE_ROLE_PREFIXES = ("formal_derived:", "derived_report:")


def classify_wiki_page_role(content: str, rel_path: str) -> str:
    """Classify generated/support pages from content already read by scanners."""
    rel = Path(rel_path)
    root = rel.parts[0] if rel.parts else rel_path
    fm_text = _frontmatter_text(content)
    explicit_role = _explicit_page_role(fm_text)
    if explicit_role:
        return explicit_role
    if root in SOURCE_EXEMPT_ROOTS:
        return f"derived_artifact:{root}"
    if _is_vault_index(rel):
        return "vault_index"
    if re.search(
        r"(?m)^\s*(mnemos_type|report_type|type)\s*:\s*"
        r"(system_report|entropy_suggestions)\s*$",
        fm_text,
    ):
        return "system_report:system_report"
    for marker in ("MOC", "retrospective-reminder", "user-persona"):
        if re.search(rf"(?m)^\s*(类型|type)\s*:\s*{re.escape(marker)}\s*$", fm_text):
            return f"derived_artifact:{marker}"
    if root == "05-MOCs" or "/MOCs/" in rel_path:
        return "derived_artifact:MOC"
    retrospective_role = _retrospective_report_role(rel)
    if retrospective_role:
        return retrospective_role
    body = _body_without_frontmatter(content)
    lowered = body.lower()
    if "自动创建的占位/消歧页" in body:
        return "generated_placeholder"
    if "temporary page for" in lowered and "testing" in lowered:
        return "test_artifact"
    if "## 相关页面" in body and "## 待完善" in body:
        return "generated_skeleton"
    if body.lstrip().startswith("# 知识蒸馏复盘"):
        return "system_report:distill_retro"
    return "knowledge"


def source_exempt_reason(rel_path: str, page_role: str = "") -> str:
    """Return the source-coverage exemption reason for a Wiki path."""
    rel = Path(rel_path)
    root = rel.parts[0] if rel.parts else rel_path
    if page_role.startswith(EXPLICIT_DERIVED_PAGE_ROLE_PREFIXES):
        return page_role
    if root in SOURCE_EXEMPT_ROOTS:
        return f"derived_artifact:{root}"
    if _is_vault_index(rel):
        return "vault_index"
    if page_role and page_role != "knowledge":
        return page_role
    if root == "05-MOCs" or "/MOCs/" in rel_path:
        return "derived_artifact:MOC"
    retrospective_role = _retrospective_report_role(rel)
    if retrospective_role:
        return retrospective_role
    return ""


def _frontmatter_text(content: str) -> str:
    if not content.startswith("---"):
        return ""
    end = content.find("---", 3)
    if end == -1:
        return ""
    return content[3:end]


def _explicit_page_role(frontmatter: str) -> str:
    match = re.search(
        r'(?m)^\s*page_role\s*:\s*["\']?([^"\'\s#]+)["\']?\s*$',
        frontmatter,
    )
    if not match:
        return ""
    role = match.group(1).strip()
    return role if role.startswith(EXPLICIT_DERIVED_PAGE_ROLE_PREFIXES) else ""


def _body_without_frontmatter(content: str) -> str:
    if not content.startswith("---"):
        return content
    end = content.find("---", 3)
    if end == -1:
        return content
    return content[end + 3 :].lstrip("\n")


def _is_vault_index(rel: Path) -> bool:
    return len(rel.parts) == 1 and rel.name.endswith(".md") and rel.name[:2].isdigit()


def _retrospective_report_role(rel: Path) -> str:
    if len(rel.parts) > 1 and rel.parts[0] == "06-Retrospectives":
        if rel.parts[1] in {"entropy", "flywheel"}:
            return f"system_report:{rel.parts[1]}"
    return ""
