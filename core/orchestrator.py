"""
Memos-Wiki v2.0.0 Orchestrator - 主控脚本

串联所有模块形成完整的知识进化循环：

    输入 → 蒸馏 → DNA → 图谱 → 免疫 → 熵减 → 压力测试 → 可证伪标记
                                                              ↓
    报告 ← 画像 ← 推送 ← 时间胶囊 ← 影子页面 ← 量子纠缠 ← 暗知识

运行模式：
- full:     完整循环
- distill:  仅运行蒸馏流水线
- immune:   仅运行免疫系统检查
- entropy:  仅运行熵减扫描
- stress:   仅运行压力测试
- dark:     仅运行暗知识挖掘
- entangle: 仅运行量子纠缠发现
- falsify:  仅运行可证伪性标记
- shadow:   仅更新影子页面
- capsule:  仅检查时间胶囊
- snapshot: 仅创建版本快照
- push:     仅运行推送引擎
- profile:  仅生成知识画像

用法：
    python -m core.orchestrator --mode full --wiki $(get_config().wiki_dir)
"""

from __future__ import annotations
import logging
logger = logging.getLogger(__name__)

import argparse
import sys
import traceback
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict

from core.config import get_config


class Orchestrator:
    """主控编排器"""

    def __init__(self, wiki_base: str | None = None, dry_run: bool = False,
                 limit: int | None = None, verbose: bool = False):
        if wiki_base:
            self.wiki_base = Path(wiki_base).expanduser()
        else:
            self.wiki_base = get_config().wiki_dir
        self.inbox = self.wiki_base / "00-Inbox"
        self.dry_run = dry_run
        self.limit = limit
        self.verbose = verbose
        self.logs: List[str] = []
        self.errors: List[tuple] = []

    def log(self, msg: str, level: str = "INFO"):
        line = f"[{level}] {msg}"
        self.logs.append(line)
        if self.verbose or level in ("ERROR", "WARN"):
            logger.info(line)

    # ========== 阶段 1: 蒸馏 ==========

    def run_distill(self) -> Dict:
        """运行蒸馏流水线（状态检查）

        实际蒸馏处理由 HephaestusWorker 负责（异步委托 Agent 处理 distill_queue）。
        本方法只检查链路状态，返回蒸馏队列和 Inbox 的当前情况。
        """
        self.log("阶段 1: 蒸馏状态检查")
        try:
            from core.hephaestus_worker import HephaestusWorker
            from core.config import get_config

            worker = HephaestusWorker()
            stats = worker.get_stats()

            inbox_dir = self.wiki_base / "00-Inbox"
            inbox_count = len(list(inbox_dir.glob("*.md"))) if inbox_dir.exists() else 0

            self.log(f"蒸馏队列: {stats['pending']} 个待处理, {stats['delegated']} 个已委托")
            self.log(f"Wiki Inbox: {inbox_count} 个页面")

            if stats['pending'] > 0:
                self.log(f"有 {stats['pending']} 个任务等待蒸馏")

            return {
                "status": "ok",
                "pending": stats['pending'],
                "delegated": stats['delegated'],
                "inbox_pages": inbox_count,
                "note": "蒸馏由 HephaestusWorker 异步处理",
            }
        except Exception as e:
            self.errors.append(("distill", str(e)))
            self.log(f"蒸馏状态检查失败: {e}", "ERROR")
            return {"status": "error", "error": str(e)}

    # ========== 阶段 2: DNA 指纹 ==========

    def run_dna(self) -> Dict:
        """计算知识 DNA 指纹"""
        self.log("阶段 2: DNA 指纹计算")
        try:
            from core.kia.genos import DNAEngine
            engine = DNAEngine(wiki_base=str(self.wiki_base))

            if self.limit:
                pages = engine._list_pages()[:self.limit]
                computed = 0
                for page in pages:
                    dna = engine.compute_dna(page)
                    if dna:
                        engine.save_dna(dna)
                        computed += 1
                stats = {"scanned": len(pages), "computed": computed, "failed": len(pages) - computed}
            else:
                stats = engine.scan_all_pages()

            self.log(f"DNA 计算完成: {stats['computed']} 个页面")
            return {"status": "ok", **stats}
        except Exception as e:
            self.errors.append(("dna", str(e)))
            self.log(f"DNA 计算失败: {e}", "ERROR")
            return {"status": "error", "error": str(e)}

    # ========== 阶段 3: 知识图谱 ==========

    def run_graph(self) -> Dict:
        """构建知识图谱关系"""
        self.log("阶段 3: 知识图谱关系构建")
        try:
            from core.kia.knowledge_graph import KnowledgeGraph, Relation, RelationType
            kg = KnowledgeGraph(wiki_base=str(self.wiki_base))

            if not self.inbox.exists():
                return {"status": "ok", "relations_added": 0}

            pages = list(self.inbox.glob("*.md"))
            if self.limit:
                pages = pages[:self.limit]

            added = 0
            for page in pages:
                new_rels = kg.discover_relations(page)
                for rel in new_rels:
                    if not self.dry_run:
                        if kg.add_relation(rel):
                            added += 1
                    else:
                        added += 1

            self.log(f"图谱构建完成: {added} 个新关系")
            return {"status": "ok", "relations_added": added}
        except Exception as e:
            self.errors.append(("graph", str(e)))
            self.log(f"图谱构建失败: {e}", "ERROR")
            return {"status": "error", "error": str(e)}

    # ========== 阶段 4: 免疫系统 ==========

    def run_immune(self) -> Dict:
        """运行免疫系统检查"""
        self.log("阶段 4: 免疫系统检查")
        try:
            from core.kia.hygieia import KnowledgeImmuneSystem
            immune = KnowledgeImmuneSystem(wiki_base=str(self.wiki_base))

            report = immune.full_scan()
            self.log(f"免疫扫描完成: 健康度 {report.health_score:.1f}/100, "
                    f"问题 {len(report.issues)}")

            if not self.dry_run and report.issues:
                auto_fixed = immune.auto_fix(report)
                self.log(f"自动修复: {len(auto_fixed)} 个问题")

            return {
                "status": "ok",
                "health_score": report.health_score,
                "issues": len(report.issues),
                "top_issues": [
                    {"type": i.issue_type, "severity": i.severity}
                    for i in report.issues[:5]
                ],
            }
        except Exception as e:
            self.errors.append(("immune", str(e)))
            self.log(f"免疫检查失败: {e}", "ERROR")
            return {"status": "error", "error": str(e)}

    # ========== 阶段 5: 熵减 ==========

    def run_entropy(self) -> Dict:
        """运行熵减扫描"""
        self.log("阶段 5: 熵减扫描")
        try:
            from core.kia.eris import EntropyEngine
            engine = EntropyEngine(wiki_base=str(self.wiki_base))

            sample = self.limit or 200
            report = engine.scan(sample_size=sample)
            self.log(f"熵减扫描完成: {report.duplicate_count} 重复, "
                    f"{report.mergeable_count} 可合并, "
                    f"{report.linkable_count} 可关联")

            return {
                "status": "ok",
                "duplicates": report.duplicate_count,
                "mergeable": report.mergeable_count,
                "linkable": report.linkable_count,
                "candidates": len(report.candidates),
            }
        except Exception as e:
            self.errors.append(("entropy", str(e)))
            self.log(f"熵减扫描失败: {e}", "ERROR")
            return {"status": "error", "error": str(e)}

    # ========== 阶段 6: 压力测试 ==========

    def run_stress(self) -> Dict:
        """运行知识压力测试"""
        self.log("阶段 6: 知识压力测试")
        try:
            from core.kia.stress_test import StressTestEngine
            engine = StressTestEngine(wiki_base=str(self.wiki_base))

            results = engine.batch_test(limit=self.limit)
            avg_score = sum(r.resilience_score for r in results) / max(len(results), 1)
            total_challenges = sum(len(r.challenges) for r in results)

            self.log(f"压力测试完成: {len(results)} 个页面, "
                    f"平均韧性 {avg_score:.1f}, {total_challenges} 个挑战")

            return {
                "status": "ok",
                "pages_tested": len(results),
                "avg_resilience": round(avg_score, 2),
                "total_challenges": total_challenges,
            }
        except Exception as e:
            self.errors.append(("stress", str(e)))
            self.log(f"压力测试失败: {e}", "ERROR")
            return {"status": "error", "error": str(e)}

    # ========== 阶段 7: 可证伪性标记 ==========

    def run_falsify(self) -> Dict:
        """运行可证伪性标记"""
        self.log("阶段 7: 可证伪性标记")
        try:
            from core.kia.aporia import FalsifiabilityMarker
            marker = FalsifiabilityMarker(wiki_base=str(self.wiki_base))

            if not self.inbox.exists():
                return {"status": "ok", "marks_created": 0}

            pages = list(self.inbox.glob("*.md"))
            if self.limit:
                pages = pages[:self.limit]

            created = 0
            for page in pages:
                if marker.get_mark(str(page)) is None:
                    mark = marker.init_mark_for_page(page)
                    if mark:
                        created += 1

            # 扫描待测试的标记
            to_test = marker.scan_all_marks()
            self.log(f"可证伪性标记完成: {created} 个新标记, {len(to_test)} 个待测试")

            return {
                "status": "ok",
                "marks_created": created,
                "pending_tests": len(to_test),
            }
        except Exception as e:
            self.errors.append(("falsify", str(e)))
            self.log(f"可证伪性标记失败: {e}", "ERROR")
            return {"status": "error", "error": str(e)}

    # ========== 阶段 8: 暗知识挖掘 ==========

    def run_dark(self) -> Dict:
        """运行暗知识挖掘（盲区检测）

        TODO: erebus 模块已按蓝图合并到知识图谱 + 免疫系统。
        当前降级为调用 hygieia.detect_knowledge_gaps() 进行盲区扫描。
        """
        self.log("阶段 8: 暗知识挖掘（盲区检测）")
        try:
            from core.kia.hygieia import KnowledgeImmuneSystem
            immune = KnowledgeImmuneSystem(wiki_base=str(self.wiki_base))
            gaps = immune.detect_knowledge_gaps()
            self.log(f"盲区检测完成: {len(gaps)} 项盲区")
            return {
                "status": "ok",
                "gaps": len(gaps),
                "note": "erebus 已合并，降级为盲区检测",
            }
        except Exception as e:
            self.errors.append(("dark", str(e)))
            self.log(f"盲区检测失败: {e}", "ERROR")
            return {"status": "error", "error": str(e)}

    # ========== 阶段 9: 量子纠缠 ==========

    def run_entangle(self) -> Dict:
        """运行量子纠缠发现（关系网络分析）

        TODO: moirai 模块已按蓝图合并到知识图谱。
        当前降级为调用知识图谱的 discover_relations 进行关系发现。
        """
        self.log("阶段 9: 量子纠缠发现（关系网络分析）")
        try:
            from core.kia.knowledge_graph import KnowledgeGraph
            kg = KnowledgeGraph(wiki_base=str(self.wiki_base))
            # 获取所有页面，进行批量关系发现
            all_pages = list(kg.wiki_base.rglob("*.md"))
            discovered = []
            for p in all_pages[:50]:  # 限制范围避免超时
                discovered.extend(kg.discover_relations(p))
            self.log(f"关系发现完成: {len(discovered)} 条新关系")
            return {
                "status": "ok",
                "relations_discovered": len(discovered),
                "note": "moirai 已合并，降级为关系发现",
            }
        except Exception as e:
            self.errors.append(("entangle", str(e)))
            self.log(f"关系发现失败: {e}", "ERROR")
            return {"status": "error", "error": str(e)}

    # ========== 阶段 10: 影子页面 ==========

    def run_shadow(self) -> Dict:
        """更新影子页面"""
        self.log("阶段 10: 影子页面更新")
        try:
            from core.kia.hecate import ShadowPageManager
            spm = ShadowPageManager(wiki_base=str(self.wiki_base))

            if not self.inbox.exists():
                return {"status": "ok", "updated": 0}

            pages = list(self.inbox.glob("*.md"))
            if self.limit:
                pages = pages[:self.limit]

            updated = 0
            for page in pages:
                if not self.dry_run:
                    result = spm.sync_shadow(page)
                    if result:
                        updated += 1
                else:
                    updated += 1

            self.log(f"影子页面更新完成: {updated} 个页面")
            return {"status": "ok", "updated": updated}
        except Exception as e:
            self.errors.append(("shadow", str(e)))
            self.log(f"影子页面更新失败: {e}", "ERROR")
            return {"status": "error", "error": str(e)}

    # ========== 阶段 11: 时间胶囊 ==========

    def run_capsule(self) -> Dict:
        """检查时间胶囊"""
        self.log("阶段 11: 时间胶囊检查")
        try:
            from core.kia.aion import TimeCapsule
            capsule = TimeCapsule(wiki_base=str(self.wiki_base))

            new_reminders = capsule.scan_for_auto_reminders()
            due = capsule.get_due_reminders(days_ahead=7)
            overdue = capsule.get_overdue_reminders()

            self.log(f"时间胶囊检查完成: {new_reminders} 新提醒, "
                    f"{len(due)} 即将到期, {len(overdue)} 已逾期")

            return {
                "status": "ok",
                "new_reminders": new_reminders,
                "due_soon": len(due),
                "overdue": len(overdue),
            }
        except Exception as e:
            self.errors.append(("capsule", str(e)))
            self.log(f"时间胶囊检查失败: {e}", "ERROR")
            return {"status": "error", "error": str(e)}

    # ========== 阶段 12: 版本快照 ==========

    def run_snapshot(self) -> Dict:
        """创建版本快照"""
        self.log("阶段 12: 版本快照")
        try:
            from core.kia.ananke import VersionTimeTravel
            vtt = VersionTimeTravel(wiki_base=str(self.wiki_base))

            stats = vtt.scan_and_snapshot_all()
            self.log(f"版本快照完成: {stats}")

            return {"status": "ok", **stats}
        except Exception as e:
            self.errors.append(("snapshot", str(e)))
            self.log(f"版本快照失败: {e}", "ERROR")
            return {"status": "error", "error": str(e)}

    # ========== 阶段 13: 推送引擎 ==========

    def run_push(self, context: str | None = None) -> Dict:
        """运行推送引擎"""
        self.log("阶段 13: 推送引擎")
        try:
            from core.kia.teiresias import PredictivePushEngine
            engine = PredictivePushEngine(wiki_base=str(self.wiki_base))

            if context:
                decision = engine.decide_push(context)
                triggered = 1 if decision.should_push else 0
                self.log(f"推送引擎完成: 上下文分析, {triggered} 条推送")
                return {
                    "status": "ok",
                    "context": context[:50],
                    "decisions": 1,
                    "triggered": triggered,
                    "reason": decision.reason,
                }
            else:
                self.log("推送引擎: 无上下文，跳过")
                return {"status": "ok", "skipped": True}
        except Exception as e:
            self.errors.append(("push", str(e)))
            self.log(f"推送引擎失败: {e}", "ERROR")
            return {"status": "error", "error": str(e)}

    # ========== 阶段 14: 知识画像 ==========

    def run_profile(self) -> Dict:
        """生成知识画像"""
        self.log("阶段 14: 知识画像")
        try:
            from core.kia.metis import ProfileGenerator
            gen = ProfileGenerator(wiki_base=str(self.wiki_base))
            profile = gen.generate()
            report = gen.generate_report(profile)

            self.log(f"知识画像完成: {profile.total_knowledge} 条知识, "
                    f"质量分 {profile.quality_score:.1f}")

            return {
                "status": "ok",
                "total_knowledge": profile.total_knowledge,
                "quality_score": round(profile.quality_score, 2),
                "learning_mode": profile.learning_mode,
            }
        except Exception as e:
            self.errors.append(("profile", str(e)))
            self.log(f"知识画像失败: {e}", "ERROR")
            return {"status": "error", "error": str(e)}

    # ========== 完整循环 ==========

    def run_full(self, push_context: str | None = None) -> Dict:
        """运行完整循环"""
        self.log("=" * 50)
        self.log("Memos-Wiki v2.0.0 完整循环开始")
        self.log(f"Wiki 路径: {self.wiki_base}")
        self.log(f"Dry run: {self.dry_run}, Limit: {self.limit}")
        self.log("=" * 50)

        results = {}

        # 核心处理链
        results["distill"] = self.run_distill()
        results["dna"] = self.run_dna()
        results["graph"] = self.run_graph()
        results["immune"] = self.run_immune()
        results["entropy"] = self.run_entropy()
        results["stress"] = self.run_stress()
        results["falsify"] = self.run_falsify()

        # 洞察发现链
        results["dark"] = self.run_dark()
        results["entangle"] = self.run_entangle()

        # 外部联动链
        results["shadow"] = self.run_shadow()
        results["capsule"] = self.run_capsule()
        results["snapshot"] = self.run_snapshot()
        results["push"] = self.run_push(context=push_context)

        # 报告
        results["profile"] = self.run_profile()

        # 汇总
        self.log("=" * 50)
        self.log("循环完成")
        self.log(f"错误数: {len(self.errors)}")
        for stage, err in self.errors:
            self.log(f"  {stage}: {err}", "ERROR")

        return {
            "timestamp": datetime.now(timezone.utc).isoformat()[:19],
            "wiki_base": str(self.wiki_base),
            "dry_run": self.dry_run,
            "results": results,
            "errors": [(s, e) for s, e in self.errors],
        }

    # ========== 报告生成 ==========

    def generate_report(self, results: Dict) -> str:
        """生成 Markdown 报告"""
        lines = [
            "# Memos-Wiki v2.0.0 运行报告",
            f"时间: {results.get('timestamp', '')}",
            f"路径: {results.get('wiki_base', '')}",
            f"Dry run: {results.get('dry_run', False)}",
            "",
        ]

        stage_names = {
            "distill": "蒸馏",
            "dna": "DNA指纹",
            "graph": "知识图谱",
            "immune": "免疫系统",
            "entropy": "熵减扫描",
            "stress": "压力测试",
            "falsify": "可证伪性",
            "dark": "暗知识",
            "entangle": "量子纠缠",
            "shadow": "影子页面",
            "capsule": "时间胶囊",
            "snapshot": "版本快照",
            "push": "推送引擎",
            "profile": "知识画像",
        }

        lines.append("## 各阶段结果")
        lines.append("")

        for stage, result in results.get("results", {}).items():
            name = stage_names.get(stage, stage)
            status = result.get("status", "unknown")
            emoji = "✅" if status == "ok" else "❌"

            lines.append(f"### {emoji} {name}")
            if status == "ok":
                for key, val in result.items():
                    if key != "status" and not isinstance(val, (list, dict)):
                        lines.append(f"- {key}: {val}")
                    elif key == "top_issues" and val:
                        for issue in val:
                            lines.append(f"- 问题: {issue['type']} ({issue['severity']})")
            else:
                lines.append(f"- 错误: {result.get('error', 'unknown')}")
            lines.append("")

        if results.get("errors"):
            lines.append("## 错误汇总")
            lines.append("")
            for stage, err in results["errors"]:
                lines.append(f"- **{stage_names.get(stage, stage)}**: {err}")
            lines.append("")

        return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Memos-Wiki v2.0.0 Orchestrator")
    parser.add_argument("--mode", default="full",
                        choices=["full", "distill", "dna", "graph", "immune",
                                "entropy", "stress", "dark", "entangle",
                                "falsify", "shadow", "capsule", "snapshot",
                                "push", "profile"],
                        help="运行模式")
    parser.add_argument("--wiki", default=None,
                        help="Wiki 基础路径")
    parser.add_argument("--limit", type=int, default=None,
                        help="限制处理数量")
    parser.add_argument("--dry-run", action="store_true",
                        help="模拟运行，不实际修改")
    parser.add_argument("--verbose", action="store_true",
                        help="详细输出")
    parser.add_argument("--push-context", default=None,
                        help="推送引擎的上下文")
    parser.add_argument("--output", default=None,
                        help="报告输出路径")

    args = parser.parse_args()

    orch = Orchestrator(
        wiki_base=args.wiki,
        dry_run=args.dry_run,
        limit=args.limit,
        verbose=args.verbose,
    )

    mode_map = {
        "full": lambda: orch.run_full(push_context=args.push_context),
        "distill": orch.run_distill,
        "dna": orch.run_dna,
        "graph": orch.run_graph,
        "immune": orch.run_immune,
        "entropy": orch.run_entropy,
        "stress": orch.run_stress,
        "dark": orch.run_dark,
        "entangle": orch.run_entangle,
        "falsify": orch.run_falsify,
        "shadow": orch.run_shadow,
        "capsule": orch.run_capsule,
        "snapshot": orch.run_snapshot,
        "push": lambda: orch.run_push(context=args.push_context),
        "profile": orch.run_profile,
    }

    run_fn = mode_map.get(args.mode, orch.run_full)
    results = run_fn()

    # 生成报告
    if args.mode == "full":
        report = orch.generate_report(results)
    else:
        report = f"# {args.mode} 运行结果\n\n```json\n{results}\n```\n"

    # 输出
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        logger.info(f"报告已保存到: {args.output}")
    else:
        logger.info("\n" + "=" * 50)
        logger.info(report)

    # 返回码
    if orch.errors:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
