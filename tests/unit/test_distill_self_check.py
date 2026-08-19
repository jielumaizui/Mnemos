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

    # 纯英文命令行同样属于完整证据。
    cleaned = clean_message_content(content)
    assert "npm install lodash" in cleaned
    cleaned_wb = _clean_message_content(content)
    assert "npm install lodash" in cleaned_wb


def test_clean_message_content_keeps_multiple_shell_commands_lossless():
    content = "git init\ngit add .\ngit commit -m 'init'\ngit push\ngit log"
    cleaned = clean_message_content(content)
    assert "git init" in cleaned
    assert "git add ." in cleaned
    assert "git commit" in cleaned
    assert "git push" in cleaned
    assert "git log" in cleaned
    assert "omitted" not in cleaned


def test_self_check_marks_contextual_and_url_pending():
    frag = _fragment(
        "目前 Redis Cluster 最新方案参考 https://redis.io/docs/latest/ ，需要按版本确认。"
    )

    passed, issues = DistillSelfCheck().check([frag], [])

    assert passed is False
    assert "contextual" == frag.frontmatter["时效性"]
    assert frag.frontmatter["external_links_pending_verification"] is True
    assert frag.self_check_severity == "warning"
    assert any("当前性表述" in issue for issue in issues)


def test_self_check_flags_python_syntax_error():
    frag = _fragment("```python\nif True print('bad')\n```")

    passed, issues = DistillSelfCheck().check([frag], [])

    assert passed is False
    assert frag.self_check_passed is False
    assert frag.self_check_severity == "fatal"
    assert frag.frontmatter["verification"] == "pending-verification"
    assert frag.frontmatter["verification_severity"] == "fatal"
    assert any("Python代码块" in issue for issue in issues)


def test_self_check_flags_suspicious_url():
    frag = _fragment("请参考 https://localhost/path 这个临时地址。")

    passed, issues = DistillSelfCheck().check([frag], [])

    assert passed is False
    assert frag.self_check_severity == "warning"
    assert any("可疑URL" in issue for issue in issues)


def test_self_check_records_decision_graph_root_count(monkeypatch):
    from core.kia import decision_dependency_extractor as dde

    foundation = dde.DecisionNode(id="d1", decision="Use PostgreSQL")
    dependent = dde.DecisionNode(
        id="d2",
        decision="Build audit views on PostgreSQL",
        dependencies=["d1"],
    )
    graph = dde.DecisionGraph(
        nodes={"d1": foundation, "d2": dependent},
        edges=[("d2", "d1", "depends_on")],
    )

    class FakeExtractor:
        def extract(self, content):
            return graph

    monkeypatch.setattr(dde, "DecisionDependencyExtractor", FakeExtractor)
    frag = _fragment("决定: 使用 PostgreSQL，因为需要稳定的审计视图。")

    DistillSelfCheck()._check_decision_dependencies(frag, frag.core_content)

    assert frag.frontmatter["decision_graph"] == {"nodes": 2, "edges": 1, "roots": 1}


def test_self_check_issue_classification():
    from core.hephaestus.distillation_engine import (
        classify_self_check_issue,
        max_self_check_severity,
    )

    assert classify_self_check_issue("Python代码块可能存在语法错误") == "fatal"
    assert classify_self_check_issue("可疑URL，待验证: https://localhost/path") == "warning"
    assert max_self_check_severity([]) == "ok"
    assert max_self_check_severity(["包含当前性表述，已标记为 contextual"]) == "warning"
    assert max_self_check_severity(["检测到断言内部冲突: A contradicts B"]) == "fatal"
