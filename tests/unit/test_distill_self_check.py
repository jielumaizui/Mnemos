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

    assert "git status 可以用来查看当前改动" in clean_message_content(content)
    assert "npm install lodash" not in clean_message_content(content)
    assert "git status 可以用来查看当前改动" in _clean_message_content(content)
    assert "npm install lodash" not in _clean_message_content(content)


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
