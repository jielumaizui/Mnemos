"""Internal wikilink integrity audit for Mnemos vaults."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from core.frontmatter import parse_frontmatter
from core.utils import atomic_write_text

WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
GENERATED_STEM_PREFIX_RE = re.compile(
    r"^(?:session__|[0-9a-f]{8}_|\d{2}-\d{2}-\d{2}_)+",
    re.IGNORECASE,
)
LINK_AUDIT_SCOPE_PREFIXES: dict[str, tuple[str, ...]] = {
    "all": (),
    "kg": ("L2.4-KG/",),
    "shadow": ("07-Shadow/",),
    "observation": ("L3-Observations/",),
    "reflection": ("L4-Reflections/",),
    "persona": ("L5-Feedback/",),
    "reminder": ("08-Reminders/",),
    "dispute": ("08-Disputes/",),
    "reports": ("99-Reports/",),
}


@dataclass(frozen=True)
class BrokenWikiLink:
    """A broken internal Obsidian wikilink occurrence."""

    page: str
    line: int
    target: str

    def to_dict(self) -> dict[str, Any]:
        return {"page": self.page, "line": self.line, "target": self.target}


@dataclass(frozen=True)
class WikiLinkRewrite:
    """A safe internal wikilink rewrite candidate."""

    page: str
    line: int
    before: str
    after: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "line": self.line,
            "before": self.before,
            "after": self.after,
        }


@dataclass(frozen=True)
class VaultLinkAuditReport:
    """Summary of an internal wikilink integrity audit."""

    vault_dir: str
    total_pages: int
    total_links: int
    broken_links: int
    pages_with_broken_links: int
    broken_by_top_dir: dict[str, int]
    samples: tuple[BrokenWikiLink, ...]
    scope: str = "all"
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.broken_links == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "vault_dir": self.vault_dir,
            "total_pages": self.total_pages,
            "total_links": self.total_links,
            "broken_links": self.broken_links,
            "pages_with_broken_links": self.pages_with_broken_links,
            "broken_by_top_dir": self.broken_by_top_dir,
            "samples": [sample.to_dict() for sample in self.samples],
            "scope": self.scope,
            "error": self.error,
            "ok": self.ok,
        }


@dataclass(frozen=True)
class VaultLinkRepairReport:
    """Summary of a vault-internal wikilink repair dry-run or apply run."""

    vault_dir: str
    scope: str
    scanned_pages: int
    candidate_links: int
    changed_pages: int
    applied: bool
    samples: tuple[WikiLinkRewrite, ...]
    error: str = ""
    mode: str = "absolute"

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict[str, Any]:
        return {
            "vault_dir": self.vault_dir,
            "scope": self.scope,
            "scanned_pages": self.scanned_pages,
            "candidate_links": self.candidate_links,
            "changed_pages": self.changed_pages,
            "applied": self.applied,
            "samples": [sample.to_dict() for sample in self.samples],
            "error": self.error,
            "mode": self.mode,
            "ok": self.ok,
        }


def _iter_vault_files(vault_dir: Path) -> list[Path]:
    if not vault_dir.exists():
        return []
    files: list[Path] = []
    for path in vault_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel_parts = path.relative_to(vault_dir).parts
        except ValueError:
            continue
        if any(part.startswith(".") for part in rel_parts):
            continue
        files.append(path)
    return sorted(files)


def _read_text_lossy(path: Path) -> str:
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        return handle.read()


def _canonical_target(target: str) -> str:
    clean = unquote(target).strip().replace("\\", "/")
    if "|" in clean:
        clean = clean.split("|", 1)[0].strip()
    if "#" in clean:
        clean = clean.split("#", 1)[0].strip()
    if "^" in clean:
        clean = clean.split("^", 1)[0].strip()
    while clean.startswith("./"):
        clean = clean[2:]
    if clean.casefold().endswith(".md"):
        clean = clean[:-3]
    return clean.strip("/")


def _alias_key(target: str) -> str:
    return _canonical_target(target).casefold()


def canonical_wiki_target_key(target: str) -> str:
    """Return the normalized lookup key used for internal wikilink targets."""

    return _alias_key(target)


def _space_variants(value: str) -> set[str]:
    return {value, value.replace(" ", "_")}


def _add_alias_variants(aliases: set[str], *values: str) -> None:
    for value in values:
        for variant in _space_variants(value):
            aliases.add(_alias_key(variant))


def _frontmatter_aliases(md_file: Path) -> list[str]:
    try:
        content = _read_text_lossy(md_file)
    except OSError:
        return []
    fm, _body = parse_frontmatter(content)
    if not isinstance(fm, dict):
        return []

    aliases: list[str] = []
    for raw_aliases in (
        fm.get("aliases"),
        fm.get("别名"),
        fm.get("title"),
        fm.get("标题"),
        fm.get("name"),
        fm.get("名称"),
    ):
        if isinstance(raw_aliases, str):
            aliases.append(raw_aliases)
        elif isinstance(raw_aliases, list):
            aliases.extend(str(alias) for alias in raw_aliases if str(alias).strip())
    return aliases


def _generated_name_aliases(md_file: Path, vault_dir: Path) -> list[str]:
    try:
        rel = md_file.relative_to(vault_dir)
    except ValueError:
        return []
    stripped_stem = GENERATED_STEM_PREFIX_RE.sub("", md_file.stem).strip()
    if not stripped_stem or stripped_stem == md_file.stem:
        return []
    parent = rel.parent.as_posix()
    aliases = [stripped_stem]
    if parent and parent != ".":
        aliases.append(f"{parent}/{stripped_stem}")
    return aliases


def _target_aliases(vault_files: list[Path], vault_dir: Path) -> set[str]:
    aliases: set[str] = set()
    for vault_file in vault_files:
        try:
            rel = vault_file.relative_to(vault_dir).as_posix()
        except ValueError:
            continue
        if vault_file.suffix.casefold() == ".md":
            rel_no_md = rel[:-3]
            _add_alias_variants(aliases, rel, rel_no_md, vault_file.name, vault_file.stem)
            _add_alias_variants(aliases, *_frontmatter_aliases(vault_file))
            _add_alias_variants(aliases, *_generated_name_aliases(vault_file, vault_dir))
        else:
            _add_alias_variants(aliases, rel, vault_file.name)
    aliases.discard("")
    return aliases


def build_vault_target_aliases(vault_dir: Path | str) -> set[str]:
    """Build the target alias set used by wikilink audit and rendering."""

    root = Path(vault_dir).expanduser()
    if not root.exists() or not root.is_dir():
        return set()
    return _target_aliases(_iter_vault_files(root), root)


def build_vault_target_index(vault_dir: Path | str) -> dict[str, tuple[str, ...]]:
    """Map each normalized Obsidian alias to its concrete vault-relative files."""

    root = Path(vault_dir).expanduser()
    if not root.exists() or not root.is_dir():
        return {}
    index: dict[str, set[str]] = {}
    exact: dict[str, tuple[str, ...]] = {}
    for vault_file in _iter_vault_files(root):
        try:
            rel = vault_file.relative_to(root).as_posix()
        except ValueError:
            continue
        aliases: set[str] = set()
        if vault_file.suffix.casefold() == ".md":
            rel_no_md = rel[:-3]
            exact[_alias_key(rel)] = (rel,)
            exact[_alias_key(rel_no_md)] = (rel,)
            _add_alias_variants(aliases, rel, rel_no_md, vault_file.name, vault_file.stem)
            _add_alias_variants(aliases, *_frontmatter_aliases(vault_file))
            _add_alias_variants(aliases, *_generated_name_aliases(vault_file, root))
        else:
            _add_alias_variants(aliases, rel, vault_file.name)
        for alias in aliases:
            if alias:
                index.setdefault(alias, set()).add(rel)
    resolved = {alias: tuple(sorted(paths)) for alias, paths in index.items()}
    resolved.update(exact)
    return resolved


def _extract_link_targets(line: str) -> list[str]:
    return [match.group(1) for match in WIKILINK_RE.finditer(line)]


def _filter_md_files_by_scope(md_files: list[Path], vault_dir: Path, scope: str) -> list[Path]:
    prefixes = LINK_AUDIT_SCOPE_PREFIXES.get(scope)
    if prefixes is None:
        raise ValueError(f"unsupported link audit scope: {scope}")
    if not prefixes:
        return md_files
    scoped: list[Path] = []
    for md_file in md_files:
        try:
            rel = md_file.relative_to(vault_dir).as_posix()
        except ValueError:
            continue
        if rel.startswith(prefixes):
            scoped.append(md_file)
    return scoped


def _split_target_suffix(target: str) -> tuple[str, str]:
    suffix_positions = [pos for marker in ("#", "^") if (pos := target.find(marker)) >= 0]
    if not suffix_positions:
        return target, ""
    split_at = min(suffix_positions)
    return target[:split_at], target[split_at:]


def _rewrite_vault_absolute_target(raw_target: str, vault_dir: Path) -> str | None:
    body, sep, alias = raw_target.partition("|")
    path_part, suffix = _split_target_suffix(body.strip())
    clean_path = unquote(path_part).replace("\\", "/")
    vault_prefix = vault_dir.resolve().as_posix().rstrip("/") + "/"
    if not clean_path.startswith(vault_prefix):
        return None

    rel = clean_path[len(vault_prefix) :].strip("/")
    if rel.casefold().endswith(".md"):
        rel = rel[:-3]
    if not rel:
        return None

    rewritten = f"{rel}{suffix}"
    if sep:
        rewritten = f"{rewritten}{sep}{alias}"
    return rewritten if rewritten != raw_target else None


def _rewrite_vault_absolute_links_in_text(
    text: str,
    rel_page: str,
    vault_dir: Path,
    sample_cap: int,
) -> tuple[str, int, list[WikiLinkRewrite]]:
    samples: list[WikiLinkRewrite] = []
    candidate_count = 0
    rewritten_lines: list[str] = []

    for line_no, line in enumerate(text.splitlines(keepends=True), start=1):
        def replace_match(match: re.Match[str]) -> str:
            nonlocal candidate_count
            before = match.group(1)
            after = _rewrite_vault_absolute_target(before, vault_dir)
            if after is None:
                return match.group(0)
            candidate_count += 1
            if len(samples) < sample_cap:
                samples.append(
                    WikiLinkRewrite(
                        page=rel_page,
                        line=line_no,
                        before=before,
                        after=after,
                    )
                )
            return f"[[{after}]]"

        rewritten_lines.append(WIKILINK_RE.sub(replace_match, line))

    return "".join(rewritten_lines), candidate_count, samples


def _broken_link_plain_text(raw_target: str) -> str:
    body, sep, alias = raw_target.partition("|")
    if sep and alias.strip():
        return alias.strip()
    return _canonical_target(body) or raw_target


def _rewrite_broken_links_in_text(
    text: str,
    rel_page: str,
    aliases: set[str],
    sample_cap: int,
) -> tuple[str, int, list[WikiLinkRewrite]]:
    samples: list[WikiLinkRewrite] = []
    candidate_count = 0
    rewritten_lines: list[str] = []

    for line_no, line in enumerate(text.splitlines(keepends=True), start=1):
        def replace_match(match: re.Match[str]) -> str:
            nonlocal candidate_count
            before = match.group(1)
            target = _canonical_target(before)
            if not target or "://" in target or _alias_key(target) in aliases:
                return match.group(0)
            after = _broken_link_plain_text(before)
            candidate_count += 1
            if len(samples) < sample_cap:
                samples.append(
                    WikiLinkRewrite(
                        page=rel_page,
                        line=line_no,
                        before=before,
                        after=after,
                    )
                )
            return after

        rewritten_lines.append(WIKILINK_RE.sub(replace_match, line))

    return "".join(rewritten_lines), candidate_count, samples


def audit_vault_links(
    vault_dir: Path | str,
    sample_limit: int = 20,
    scope: str = "all",
) -> VaultLinkAuditReport:
    """Audit internal Obsidian wikilinks and summarize broken targets.

    Heading-only links such as ``[[#Section]]`` are page-local anchors and are
    skipped. External URLs are outside this audit's scope; they are handled by
    the link-probe queue.
    """

    root = Path(vault_dir).expanduser()
    if not root.exists() or not root.is_dir():
        return VaultLinkAuditReport(
            vault_dir=str(root),
            total_pages=0,
            total_links=0,
            broken_links=0,
            pages_with_broken_links=0,
            broken_by_top_dir={},
            samples=(),
            scope=scope,
            error=f"vault path does not exist or is not a directory: {root}",
        )
    vault_files = _iter_vault_files(root)
    md_files = _filter_md_files_by_scope(
        [path for path in vault_files if path.suffix.casefold() == ".md"],
        root,
        scope,
    )
    aliases = _target_aliases(vault_files, root)
    broken_samples: list[BrokenWikiLink] = []
    broken_pages: set[str] = set()
    broken_by_top_dir: Counter[str] = Counter()
    total_links = 0
    broken_links = 0
    sample_cap = max(0, sample_limit)

    for md_file in md_files:
        try:
            rel_page = md_file.relative_to(root).as_posix()
            lines = _read_text_lossy(md_file).splitlines()
        except OSError:
            continue
        for line_no, line in enumerate(lines, start=1):
            for raw_target in _extract_link_targets(line):
                target = _canonical_target(raw_target)
                if not target or "://" in target:
                    continue
                total_links += 1
                if _alias_key(target) in aliases:
                    continue
                broken_links += 1
                broken_pages.add(rel_page)
                broken_by_top_dir[rel_page.split("/", 1)[0]] += 1
                if len(broken_samples) < sample_cap:
                    broken_samples.append(
                        BrokenWikiLink(page=rel_page, line=line_no, target=target)
                    )

    return VaultLinkAuditReport(
        vault_dir=str(root),
        total_pages=len(md_files),
        total_links=total_links,
        broken_links=broken_links,
        pages_with_broken_links=len(broken_pages),
        broken_by_top_dir=dict(sorted(broken_by_top_dir.items())),
        samples=tuple(broken_samples),
        scope=scope,
    )


def repair_vault_absolute_wikilinks(
    vault_dir: Path | str,
    sample_limit: int = 20,
    scope: str = "all",
    apply: bool = False,
) -> VaultLinkRepairReport:
    """Rewrite vault-internal absolute wikilinks to vault-relative targets.

    The default is a dry-run. Only links that point inside ``vault_dir`` are
    candidates; external absolute paths and ordinary broken links are left
    untouched.
    """

    root = Path(vault_dir).expanduser()
    if not root.exists() or not root.is_dir():
        return VaultLinkRepairReport(
            vault_dir=str(root),
            scope=scope,
            scanned_pages=0,
            candidate_links=0,
            changed_pages=0,
            applied=apply,
            samples=(),
            error=f"vault path does not exist or is not a directory: {root}",
        )

    vault_files = _iter_vault_files(root)
    md_files = _filter_md_files_by_scope(
        [path for path in vault_files if path.suffix.casefold() == ".md"],
        root,
        scope,
    )
    sample_cap = max(0, sample_limit)
    all_samples: list[WikiLinkRewrite] = []
    candidate_links = 0
    changed_pages = 0

    for md_file in md_files:
        try:
            rel_page = md_file.relative_to(root).as_posix()
            text = _read_text_lossy(md_file)
        except OSError:
            continue
        new_text, page_candidates, page_samples = _rewrite_vault_absolute_links_in_text(
            text,
            rel_page,
            root,
            max(0, sample_cap - len(all_samples)),
        )
        if page_candidates == 0:
            continue
        candidate_links += page_candidates
        changed_pages += 1
        all_samples.extend(page_samples)
        if apply and new_text != text:
            atomic_write_text(md_file, new_text, encoding="utf-8")

    return VaultLinkRepairReport(
        vault_dir=str(root),
        scope=scope,
        scanned_pages=len(md_files),
        candidate_links=candidate_links,
        changed_pages=changed_pages,
        applied=apply,
        samples=tuple(all_samples),
    )


def repair_broken_wikilinks(
    vault_dir: Path | str,
    sample_limit: int = 20,
    scope: str = "all",
    apply: bool = False,
) -> VaultLinkRepairReport:
    """Strip unresolved internal wikilinks to readable plain text.

    The default is a dry-run. Existing resolvable wikilinks are left untouched;
    only links that the audit alias set cannot resolve are candidates.
    """

    root = Path(vault_dir).expanduser()
    if not root.exists() or not root.is_dir():
        return VaultLinkRepairReport(
            vault_dir=str(root),
            scope=scope,
            scanned_pages=0,
            candidate_links=0,
            changed_pages=0,
            applied=apply,
            samples=(),
            error=f"vault path does not exist or is not a directory: {root}",
            mode="strip-broken",
        )

    vault_files = _iter_vault_files(root)
    md_files = _filter_md_files_by_scope(
        [path for path in vault_files if path.suffix.casefold() == ".md"],
        root,
        scope,
    )
    aliases = _target_aliases(vault_files, root)
    sample_cap = max(0, sample_limit)
    all_samples: list[WikiLinkRewrite] = []
    candidate_links = 0
    changed_pages = 0

    for md_file in md_files:
        try:
            rel_page = md_file.relative_to(root).as_posix()
            text = _read_text_lossy(md_file)
        except OSError:
            continue
        new_text, page_candidates, page_samples = _rewrite_broken_links_in_text(
            text,
            rel_page,
            aliases,
            max(0, sample_cap - len(all_samples)),
        )
        if page_candidates == 0:
            continue
        candidate_links += page_candidates
        changed_pages += 1
        all_samples.extend(page_samples)
        if apply and new_text != text:
            atomic_write_text(md_file, new_text, encoding="utf-8")

    return VaultLinkRepairReport(
        vault_dir=str(root),
        scope=scope,
        scanned_pages=len(md_files),
        candidate_links=candidate_links,
        changed_pages=changed_pages,
        applied=apply,
        samples=tuple(all_samples),
        mode="strip-broken",
    )


def render_link_audit_report(report: VaultLinkAuditReport) -> str:
    """Render a human-readable link audit report."""

    lines = [
        "Vault 断链审计",
        f"目标 vault: {report.vault_dir}",
        f"审计范围: {report.scope}",
        f"页面数: {report.total_pages}",
        f"内部 wikilink: {report.total_links}",
        f"断链: {report.broken_links}",
        f"受影响页面: {report.pages_with_broken_links}",
    ]
    if report.broken_by_top_dir:
        lines.append("断链目录分布:")
        for top_dir, count in report.broken_by_top_dir.items():
            lines.append(f"  - {top_dir}: {count}")
    if report.samples:
        lines.append("样本:")
        for sample in report.samples:
            lines.append(f"  - {sample.page}:{sample.line} -> [[{sample.target}]]")
    if report.error:
        lines.append(f"错误: {report.error}")
    elif not report.samples and report.broken_links == 0:
        lines.append("未发现内部 wikilink 断链。")
    return "\n".join(lines)


def render_link_repair_report(report: VaultLinkRepairReport) -> str:
    """Render a human-readable absolute wikilink repair report."""

    mode = "apply" if report.applied else "dry-run"
    title = (
        "Vault 断链 wikilink 纯文本修复"
        if report.mode == "strip-broken"
        else "Vault 绝对路径 wikilink 修复"
    )
    lines = [
        title,
        f"目标 vault: {report.vault_dir}",
        f"审计范围: {report.scope}",
        f"模式: {mode}",
        f"扫描页面: {report.scanned_pages}",
        f"候选链接: {report.candidate_links}",
        f"涉及页面: {report.changed_pages}",
    ]
    if report.samples:
        lines.append("样本:")
        for sample in report.samples:
            if report.mode == "strip-broken":
                lines.append(
                    f"  - {sample.page}:{sample.line} [[{sample.before}]] -> {sample.after}"
                )
            else:
                lines.append(
                    f"  - {sample.page}:{sample.line} "
                    f"[[{sample.before}]] -> [[{sample.after}]]"
                )
    if report.error:
        lines.append(f"错误: {report.error}")
    elif report.candidate_links == 0 and report.mode == "strip-broken":
        lines.append("未发现需要剥离的断链 wikilink。")
    elif report.candidate_links == 0:
        lines.append("未发现可安全改写的 vault 内绝对路径 wikilink。")
    elif not report.applied:
        lines.append("dry-run 未写入；如需改写，显式加 --apply。")
    return "\n".join(lines)
