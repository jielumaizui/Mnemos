#!/usr/bin/env python3
"""强制使用 DeepSeek V3 API 蒸馏测试"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from core.hephaestus.document_processor import DocumentProcessor
from core.hephaestus.distillation_engine import DistillationEngine
from core.hephaestus.wiki_builder import generate_wiki_page
from integrations.styx import MemosClient


def get_session_content(session_id: str) -> str:
    client = MemosClient()
    memos = client.list_all_memos()
    session_memos = []
    for m in memos:
        tags = m.get('tags', [])
        if f'session={session_id}' in str(tags):
            turn = 0
            for t in tags:
                if str(t).startswith('turn='):
                    turn = int(str(t).split('=')[1])
                    break
            session_memos.append((turn, m.get('content', '')))
    session_memos.sort(key=lambda x: x[0])
    return '\n\n---\n\n'.join(content for _, content in session_memos)


def test_file():
    """测试文件蒸馏 - 强制 API 模式"""
    print("=" * 60)
    print("文件蒸馏测试 - DeepSeek V3")
    print("=" * 60)

    p = DocumentProcessor()
    f = Path.home() / 'Desktop' / '到家' / 'ai' / 'knowledge_inbox' / '.processed' / '_常州事业部到家业务数据分析报告_2026年5月.html'

    # process_and_distill 不支持 force_provider，直接用 pipeline
    from core.hephaestus.document_pipeline import DocumentDistillationPipeline
    from core.hephaestus.distillation_engine import HostAgentCaller
    import hashlib

    doc = p.process_document(f)
    session_id = f"doc-{hashlib.md5(str(f).encode()).hexdigest()[:8]}"
    messages = [{"role": "system", "content": doc.content}]
    meta = {"source": "human", "filename": doc.filename, "file_path": str(f), "doc_type": doc.doc_type.value}

    # 强制 API 模式
    caller = HostAgentCaller(force_provider="api")
    pipeline = DocumentDistillationPipeline(caller=caller)
    result = pipeline.process(session_id, messages, meta)

    print(f"Judgment: {result.judgment}")
    print(f"Fragments: {len(result.fragments)}")

    for i, frag in enumerate(result.fragments[:5]):
        print(f"\nFragment {i+1}: {frag.title}")
        print(f"  Relations: {frag.relations}")
        if frag.relations:
            for rel in frag.relations:
                print(f"    → {rel.get('target', '?')} ({rel.get('type', '?')}): {rel.get('context', '')[:60]}")

        wiki = generate_wiki_page(frag, session_id, source="human")
        print(f"  含关联字段: {'关联:' in wiki}")


def test_ai_session():
    """测试 AI 对话蒸馏 - 强制 API 模式"""
    print("\n" + "=" * 60)
    print("AI 对话蒸馏测试 - DeepSeek V3")
    print("=" * 60)

    sid = '74f412fa-5b74-4a58-835f-2c0db00f3c4d'
    content = get_session_content(sid)
    print(f"Session: {sid}, 内容长度: {len(content)} chars")

    engine = DistillationEngine()
    messages = [{"role": "user", "content": content}]
    meta = {"source": "claude", "session_id": sid}

    # DistillationEngine 默认用 HostAgentCaller()，需要改内部 caller
    # 直接调用 process，但 process 内部用的是 self._caller
    # 需要创建新的 engine 或修改 caller
    from core.hephaestus.distillation_engine import HostAgentCaller
    engine._caller = HostAgentCaller(force_provider="api")

    result = engine.process(sid, messages, meta)
    print(f"Judgment: {result.judgment}")
    print(f"Fragments: {len(result.fragments)}")

    for i, frag in enumerate(result.fragments[:5]):
        print(f"\nFragment {i+1}: {frag.title}")
        print(f"  Relations: {frag.relations}")
        if frag.relations:
            for rel in frag.relations:
                print(f"    → {rel.get('target', '?')} ({rel.get('type', '?')}): {rel.get('context', '')[:60]}")

        wiki = generate_wiki_page(frag, sid, source="claude")
        print(f"  含关联字段: {'关联:' in wiki}")


if __name__ == '__main__':
    test_file()
    test_ai_session()
