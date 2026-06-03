from core.hephaestus.distillation_engine import (
    DistillSelfCheck,
    KnowledgeFragment,
    clean_message_content,
)
from core.hephaestus.wiki_builder import _clean_message_content


def _fragment(content, frontmatter=None):
    return KnowledgeFragment(
        form="decision",
        title="Redis 集群方案",
        frontmatter=frontmatter or {},
        background="",
        core_content=content,
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
    )


def test_clean_message_content_keeps_chinese_shell_explanation():
    content = "git status 可以用来查看当前改动\nnpm install lodash"

    # 含中文解释的命令行保留
    assert "git status 可以用来查看当前改动" in clean_message_content(content)
    assert "git status 可以用来查看当前改动" in _clean_message_content(content)

    # 纯英文命令行改为压缩保留（而非删除）
    cleaned = clean_message_content(content)
    assert "npm install lodash" in cleaned
    cleaned_wb = _clean_message_content(content)
    assert "npm install lodash" in cleaned_wb


def test_clean_message_content_compresses_multiple_shell_commands():
    content = "git init\ngit add .\ngit commit -m 'init'\ngit push\ngit log"
    cleaned = clean_message_content(content)
    assert "git init" in cleaned
    assert "git add ." in cleaned
    assert "git commit" in cleaned
    # 前 3 条保留，第 4 条和第 5 条被压缩标记替代
    assert "git push" not in cleaned
    assert "[... 2 more shell commands omitted ...]" in cleaned
    assert "git log" not in cleaned  # 被压缩标记替代


def test_self_check_marks_contextual_and_url_pending():
    frag = _fragment(
        "目前 Redis Cluster 最新方案参考 https://redis.io/docs/latest/ ，需要按版本确认。"
    )

    passed, issues = DistillSelfCheck().check([frag], [])

    assert passed is False
    assert "contextual" == frag.frontmatter["时效性"]
    assert frag.frontmatter["external_links_pending_verification"] is True
    assert any("当前性表述" in issue for issue in issues)


def test_self_check_flags_python_syntax_error():
    frag = _fragment("```python\nif True print('bad')\n```")

    passed, issues = DistillSelfCheck().check([frag], [])

    assert passed is False
    assert frag.self_check_passed is False
    assert frag.frontmatter["verification"] == "pending-verification"
    assert any("Python代码块" in issue for issue in issues)


def test_self_check_flags_suspicious_url():
    frag = _fragment("请参考 https://localhost/path 这个临时地址。")

    passed, issues = DistillSelfCheck().check([frag], [])

    assert passed is False
    assert any("可疑URL" in issue for issue in issues)
