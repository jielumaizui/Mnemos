#!/usr/bin/env python3
"""测试 AI 对话蒸馏 - 跑几条 session 看 relations 效果"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from core.hephaestus.distillation_engine import DistillationEngine
from core.hephaestus.wiki_builder import generate_wiki_page
from integrations.styx import MemosClient

def get_session_content(session_id: str) -> str:
    """从 Memos 中重建 session 内容"""
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


def main():
    # 选 3 个有实质内容的 session
    session_ids = [
        '816c31f2-d5b7-4787-8829-ed06e2fdcc2a',  # 11 turns, 30k chars
        '74f412fa-5b74-4a58-835f-2c0db00f3c4d',  # 8 turns, 28k chars
        'be0ad5f9-29c5-46c9-8832-3d8a0f75ca93',  # 11 turns, 25k chars
    ]

    engine = DistillationEngine()

    for sid in session_ids:
        print(f"\n{'='*60}")
        print(f"Session: {sid}")
        print(f"{'='*60}")

        content = get_session_content(sid)
        print(f"内容长度: {len(content)} chars")

        # 构建 messages
        messages = [{"role": "user", "content": content}]
        meta = {"source": "claude", "session_id": sid}

        result = engine.process(sid, messages, meta)
        print(f"Judgment: {result.judgment}")
        print(f"Fragments: {len(result.fragments)}")

        for i, frag in enumerate(result.fragments[:3]):
            print(f"\nFragment {i+1}: {frag.title}")
            print(f"  Form: {frag.form}")
            print(f"  Relations: {frag.relations}")
            if frag.relations:
                for rel in frag.relations:
                    print(f"    → {rel.get('target', '?')} ({rel.get('type', '?')}): {rel.get('context', '')[:60]}")

            # 生成 wiki 页面预览
            wiki_md = generate_wiki_page(frag, sid, source="claude")
            if '关联:' in wiki_md:
                print(f"  Wiki 包含关联字段 ✓")
            else:
                print(f"  Wiki 无关联字段")


if __name__ == '__main__':
    main()
