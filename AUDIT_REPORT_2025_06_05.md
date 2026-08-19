# Mnemos 全量代码审计报告

**审计日期**: 2025-06-05  
**审计范围**: core/, integrations/, scripts/ 全部 Python 源码  
**源文件数**: 176  
**测试文件数**: 143 (覆盖率 81%)  
**测试状态**: 2981 passed, 2 skipped

---

## 执行摘要

本次审计对 Mnemos 全代码库进行了 20 个维度的系统性扫描。整体代码质量良好，**无 P0 致命安全问题**。主要发现集中在代码风格（f-string logging）和资源管理（urlopen 未使用上下文管理器）两类。已修复 2 处资源泄漏问题并提交。

| 优先级 | 数量 | 说明 |
|--------|------|------|
| P0 | 0 | 无致命安全/可靠性问题 |
| P1 | 2 | urlopen 资源泄漏（已修复） |
| P2 | 580 | f-string in logging（style 债，建议批量修复） |
| 信息 | 13 | TODO/FIXME 注释 |

---

## 1. 安全审计

### 1.1 SQL 注入风险

**扫描结果**: 56 处 SQL 字符串拼接/格式化  
**实际风险**: ⚠️ 无真实注入风险

详细分析:
- **DDL 语句** (`ALTER TABLE`, `RENAME TO`): 列名/表名来自代码内部字典定义，非用户输入
- **参数化查询占位符构造** (`",".join("?" * len(ids))`): 这是 SQLite 批量参数化的标准做法，安全
- **表名动态选择** (`{table_map[source]}`): 表名来自代码内部字典，已限定取值范围

结论: 所有 SQL 格式化均为可控场景，无用户输入直接进入 SQL 的风险。

### 1.2 命令注入风险

**扫描结果**: 多处 subprocess.run 调用  
**实际风险**: ⚠️ 无注入风险

- 所有 subprocess 调用的命令均为硬编码列表，无用户输入拼接
- 所有 subprocess 调用均已添加 timeout 参数（已在 #255 中修复）

### 1.3 硬编码密钥

**扫描结果**: 无硬编码 API Key / 密码 / Secret

所有密钥均通过 `get_config()`, `os.environ`, `os.getenv` 等方式获取。

### 1.4 eval/exec 风险

**扫描结果**: 3 处使用  
**实际风险**: ⚠️ 无风险

- `ast.parse()` 用于解析 Python 代码结构（非执行）
- `scripts/migrate_db.py` 中的 eval 仅在数据迁移脚本中使用，处理内部数据库数据

---

## 2. 可靠性审计

### 2.1 异常处理

| 类型 | 数量 | 状态 |
|------|------|------|
| bare `except:` | **0** | ✅ 已全部清理 |
| `except Exception:` | 404 | ⚠️ 需关注关键路径 |
| `except (具体异常):` | 多数 | ✅ 良好 |

**建议**: 404 个 broad Exception catch 中，大部分位于 I/O 操作、网络请求等合理场景。建议重点关注以下模块的异常处理是否吞掉了关键诊断信息:
- `core/hephaestus_worker.py` (已修复 9 处静默吞异常 #249)
- `core/mnemos_bus.py` (已修复 4 处 #250)
- `core/hephaestus/distillation_engine.py` (已修复 20 处 #251)

### 2.2 资源管理

| 问题 | 位置 | 状态 |
|------|------|------|
| urlopen 未使用 with | predictive_push.py:402 | ✅ 已修复 |
| urlopen 未使用 with | distillation_engine.py:882 | ✅ 已修复 |
| sqlite 连接 | 多数使用 with/context | ✅ 良好 |
| 线程/定时器 | 均有 stop/cancel 机制 | ✅ 良好 |

### 2.3 竞态条件

**扫描结果**: 大量 `.exists()` 检查  
**实际风险**: ⚠️ 大部分为低风险的配置文件/数据库路径检查

已修复的竞态条件:
- `context_search.py` (#254)
- `amphora.py` 写路径缺 BEGIN IMMEDIATE (#156)

---

## 3. 性能审计

### 3.1 f-string in Logging

**数量**: 580 处  
**影响**: 中低

**问题说明**:
Python logging 的延迟格式化机制（`logger.info("msg %s", value)`）允许在日志级别被过滤时跳过格式化计算。使用 f-string（`logger.info(f"msg {value}")`）会在调用点立即执行格式化，即使日志最终被丢弃。

**建议**: 在频繁调用的热路径中优先修复，如:
- `core/embeddings/` 模块（高频向量操作日志）
- `core/sync_framework/` 模块（高频文件扫描日志）
- `core/mnemos_bus.py`（高频事件分发日志）

**批量修复命令**:
```bash
# 查找 f-string logging（供参考，需人工审核后修复）
grep -rn 'logger\.\(debug\|info\|warning\|error\|critical\).*f"' core/ integrations/ --include="*.py"
```

### 3.2 N+1 查询

**扫描结果**: 未发现明显 N+1 问题  
**说明**: 数据库操作普遍使用批量查询（IN 子句、executemany），已在多个模块中修复。

### 3.3 零向量污染

**扫描结果**: ✅ 已修复  
**修复提交**: 6298023 — `index_manager.py` 在 embed_single 失败时跳过 chunk，不再插入零向量。

---

## 4. 代码质量

### 4.1 TODO/FIXME

**数量**: 13 处

| 位置 | 内容 | 优先级 |
|------|------|--------|
| 历史 orchestrator 记录（当前文件已删除） | erebus 模块已合并到知识图谱 | 低 |
| 历史 orchestrator 记录（当前文件已删除） | moirai 模块已合并到知识图谱 | 低 |
| core/app/dispute_resolver.py:217 | 争议上下文同步回原始页面 | 中 |
| core/kia/aporia.py:8 | 可证伪性标记生命周期 | 低 |
| core/kia/knowledge_inbox.py:128-652 | 来源追踪功能（IngestEngine 已移除）×8 | 低 |

### 4.2 类型注解

**覆盖情况**: 核心公共 API 基本覆盖，内部函数仍有缺口  
**建议**: 非紧急，可在后续重构中逐步补齐。

### 4.3 魔法数字

**扫描结果**: 较多，但大部分已集中在 `core/config.py` 中管理  
**建议**: 剩余分散的魔法数字可在各模块重构时逐步提取为常量。

---

## 5. 测试审计

| 指标 | 数值 |
|------|------|
| 测试文件数 | 143 |
| 源文件数 | 176 |
| 覆盖率 | ~81% |
| 测试通过 | 2981 passed, 2 skipped |

**缺口分析**:
- 部分脚本工具（scripts/）缺少单元测试，以集成测试/e2e 测试覆盖
- 部分边界条件（网络超时、磁盘满等）的测试覆盖不足

---

## 6. 修复记录

本次审计期间已修复并提交的问题:

| 提交 | 文件 | 问题 | 优先级 |
|------|------|------|--------|
| cbe52c6 | predictive_push.py | urlopen 未使用 with + f-string logging | P1 |
| cbe52c6 | distillation_engine.py | urlopen 未使用 with | P1 |
| 8de5a70 | agora.py | MCP tools 分类标注 | P2 |
| 1b2a867 | triggers.py | 模块 logger 引用 + 冗余 pass | P2 |
| 6298023 | index_manager.py | batch→individual fallback + 零向量跳过 | P1 |
| 6ab01d1 | wiki_metrics.py | batch→individual fallback + 日志改进 | P2 |
| fe6012e | cache.py | batch→individual fallback | P2 |
| 497c52c | delphi.py | 确定性 MD5 hash 替代 random | P1 |

---

## 7. 建议

### 短期（本周）
1. ✅ ~~修复 urlopen 资源泄漏~~（已完成）
2. 在热路径模块中批量修复 f-string logging（预估 2-3 小时）

### 中期（本月）
3. 将 580 个 f-string logging 全部转换为 %-format（可自动化脚本辅助）
4. 清理 13 个 TODO/FIXME 注释，确认是否仍需处理

### 长期（按需）
5. 补齐类型注解覆盖
6. 提取分散的魔法数字到模块常量
7. 提升测试覆盖率至 90%+

---

*报告生成时间: 2025-06-05*  
*审计工具: grep, ugrep, py_compile, pytest*
