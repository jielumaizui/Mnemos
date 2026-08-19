# ROOT-20260712-001 Hermetic 可信文档路径证明设计

- 日期：2026-07-12
- 状态：✅ 设计已批准，待实现
- 审计来源：Desktop `mnemos深度全量审计-2026-07-12.md`
- 对应问题：`ROOT-20260712-001`

## 1. 目标与 Done 定义

修复 hermetic 测试根位于系统临时目录时，被可信文档导入策略无条件拒绝的问题，同时保持生产临时文件拒绝边界不降级。完成后必须同时满足：

1. 普通 `/tmp`、`/private/tmp`、`/var/tmp` 与 `/private/var/tmp` 文件继续被拒绝。
2. 只有当前 `HermeticRunEnvironment` 签发、manifest 完整性校验通过、解析后仍位于当前 `sandbox_root` 内的 synthetic fixture 可以通过临时目录检查。
3. `document_process`、`DocumentImportService`、`FileIngestor` 及其 caller 不新增 `allow_temp`、测试标志、路径 allowlist 或其他可选择绕过参数。
4. 缺失证明、旧 schema、manifest/环境错配、签名错误、路径逃逸、manifest 位于 run root 外、owned path 逃逸均 fail closed。
5. manifest、报告、日志和错误消息不泄露证明 secret；证明只在受控父子进程环境中传递。
6. Quick、Integration、Heavy 和 mock Wow E2E 恢复真实业务链路验证；run root 外负向测试仍稳定拒绝。
7. 仓库文档、Desktop 系统图谱与源审计文档同步实际代码和验证证据；ROOT-001 独立提交关闭。

## 2. 修复前根因

当前两个正确但互不认识的安全边界发生冲突：

- `core/ops/hermetic_run.py` 把 `TMPDIR/TEMP/TMP` 收进唯一 `sandbox_root`。在 macOS 上，这个 root 通常仍解析到 `/private/tmp/...`。
- `core/document_import.py::validate_trusted_user_document()` 对四类系统临时目录做无条件前缀拒绝。
- `scripts/run_tests.py`、pytest collection boundary 和 full-score 把 hermetic 环境传给子进程，但可信文档策略不读取或校验该所有权证明。

因此 `tmp_path` 中的正常 synthetic 文档在格式、隐私、raw receipt 和蒸馏链路之前就被拒绝。31 个三层测试失败与 Wow 的 `document_import=fail` 是同一根因，不是 32 个独立业务缺陷。

## 3. 方案比较

### 方案 A：caller/test flag 绕过（拒绝）

给校验函数增加 `allow_temp=True`、识别 `MNEMOS_TEST_RUN=1`，或在测试 fixture monkeypatch 临时目录前缀。

这会把安全边界交给 caller，测试也不再经过生产真实路径；属于假修复和测试弱化。

### 方案 B：把所有 sandbox 搬出系统临时目录（拒绝）

把 runner root 固定到仓库或用户目录可减少当前失败，但不能覆盖显式 `/tmp` fixture、跨平台 temp 语义和直接 pytest；还会引入工作树污染、清理和并发所有权问题。

### 方案 C：run-owned path attestation（采用）

深化现有 `HermeticRunEnvironment`：每轮生成高熵 secret，用 HMAC-SHA256 签署 v2 manifest。可信文档校验只在路径命中系统临时目录时，读取当前进程环境中的 manifest locator 与 secret，验证签名、schema、profile、environment hash、root containment 和 candidate containment。

该方案不新增平行测试策略，不改变普通路径逻辑，也不给业务 caller 暴露绕过开关。

## 4. 信任边界与非目标

### 4.1 要抵御的伪造

- 用户只提供文件路径，不能通过文件名、目录名、业务参数或配置项声明自己属于 hermetic run。
- 单独创建类似 `environment-manifest.json` 的 JSON 文件不能通过；没有当前 run secret 无法生成有效签名。
- 拷贝、编辑或局部替换 manifest 字段会导致签名或环境绑定失败。
- 当前证明不能授权 run root 外的路径，即使该路径也位于系统临时目录。

### 4.2 本机进程信任根

run secret 会传给该轮受控测试/门禁子进程，因此这不是抵御同一 OS 账户任意进程读取、注入和重写环境的隔离机制。能够完全控制 Mnemos 进程环境的本机主体已经位于当前 CLI/runner 的 OS 信任根内；本 Root 不把 HMAC 描述为跨账户授权。

如果未来威胁模型要求抵御同账户恶意进程，应单独设计 broker、OS peer credential 或 keyring-backed issuer，不能继续扩大本证明的语义。

### 4.3 非目标

- 不允许生产用户从任意系统临时目录导入文件。
- 不重构 document canonical raw、capture outbox 或 distillation ownership。
- 不用本修复掩盖 ROOT-002 的 empty sandbox/live-runtime certificate 冲突。
- 不修改文件大小、raw vault、自身摄入、符号链接和非普通文件的既有拒绝规则。

## 5. Manifest v2 与证明契约

`core.ops.hermetic_run` 继续作为唯一 owner，把 schema 升级为：

```text
mnemos.hermetic_run_environment.v2
```

新建 run 时生成至少 256 bit 随机 secret。环境只增加内部键：

```text
MNEMOS_RUN_ATTESTATION_SECRET
```

secret 每轮唯一，只存在于父进程构造的环境映射及其受控子进程环境中，不落盘。现有 `environment_hash` 继续作为非秘密的运行环境证据，计算时显式排除 secret；证明强度来自下述 HMAC，而不是把 secret 混进可公开的 hash。

manifest 增加：

```json
{
  "attestation": {
    "algorithm": "hmac-sha256",
    "key_id": "sha256(secret)",
    "signature": "hmac(secret, canonical_manifest_without_signature)"
  }
}
```

约束：

1. secret 不写入 manifest、`report()`、console、JSON gate report 或 exception。
2. `key_id` 只用于定位错配，不作为验证 secret。
3. 签名覆盖 schema、profile、sandbox root、manifest path、environment hash、owned paths、formal state targets 和动态结果字段。
4. `finalize()` 更新 `formal_state_diff` 时必须重新签名，不允许保留旧签名。
5. 比较 secret hash 与 HMAC signature 时使用 constant-time comparison。
6. v1 manifest 仍可作为历史运行证据读取，但没有路径授权能力；不得兼容性升级或推断签名。

## 6. 唯一验证 Interface

在 `core.ops.hermetic_run` 内新增只读、无副作用的深 interface：

```python
@dataclass(frozen=True)
class RunOwnedPathAttestation:
    ok: bool
    reason: str
    root: Path | None = None


def attest_current_run_owned_path(
    candidate: Path,
) -> RunOwnedPathAttestation:
    ...
```

公开函数固定通过 `core.runtime_environment.environment_snapshot()` 读取当前进程环境，不接收 caller 提供的环境或授权参数。模块内部可以保留 `_attest_run_owned_path(candidate, environment)` 纯函数作为实现 seam，只有 `core.ops.hermetic_run` 自身的确定性单元测试直接调用；`document_process`、import service 和其他业务模块不得导入该私有函数。

验证顺序固定为：

1. 必需环境键完整且 `MNEMOS_RUN_PROFILE=isolated`。
2. manifest 路径解析后位于声明的 `MNEMOS_RUN_ROOT` 内，并与 manifest 自述路径相等。
3. schema 精确为 v2，算法精确为 `hmac-sha256`。
4. manifest profile、sandbox root、environment hash 与当前环境声明精确相等。
5. manifest 的每个 owned path 解析后均为 root 本身或其后代。
6. `key_id` 与当前 secret 一致，HMAC signature 有效。
7. candidate 使用已解析 canonical path，且为 root 本身或其后代。

任何读取、JSON、类型、路径解析或比较错误都返回稳定 reason code，不抛出 secret 或 manifest 正文。v2 的 canonical reason 集合为：

```text
not_declared
unsupported_schema
profile_mismatch
manifest_outside_root
manifest_binding_mismatch
owned_path_escape
invalid_key_id
invalid_signature
candidate_outside_root
```

## 7. 可信文档校验数据流

`validate_trusted_user_document()` 保留现有前置文件类型和 canonical resolve。临时目录分支改为：

```text
candidate does not match blocked temp prefixes
  -> continue existing validation unchanged

candidate matches blocked temp prefixes
  -> attest_current_run_owned_path(candidate)
  -> ok: continue raw-vault and size validation
  -> deny/error: reject as system temporary file
```

外部 `TrustedDocumentValidation.reason` 继续使用“拒绝摄入系统临时目录文件”，避免把内部证明结构暴露给用户。稳定的 attestation reason 由 helper 单元测试和受控诊断证据验证；不得把 secret、signature 或完整环境写进错误文本。

`DocumentImportService`、`FileIngestor`、MCP、CLI 和 daemon 不新增分支。它们继续共享同一 canonical validator，从而保证测试走的就是生产导入链路。

## 8. 失败与恢复语义

- manifest 缺失、半写、损坏或暂时不可读：拒绝当前临时路径；不重写、不修复 manifest。
- v1/未知 schema：拒绝路径授权；不自动补签。
- secret 缺失或不匹配：拒绝；不降级为 environment hash 校验。
- manifest finalize 中断：旧完整签名仍只对应旧内容；半写由原子写保护，任何不一致 fail closed。
- root 已删除或 candidate 已消失：沿用文件不存在/无法解析错误，不尝试恢复。
- 验证失败不得写 import ledger 之外的新安全状态；既有 import failure receipt 语义不变。

## 9. 测试设计

先写失败测试，再修改实现。

### 9.1 `tests/unit/test_hermetic_run_environment.py`

1. v2 manifest 有签名且无 secret 明文。
2. 当前 environment + root 内 candidate 验证通过。
3. root 外 candidate 拒绝。
4. 缺 secret、错误 secret、错误 environment hash 拒绝。
5. manifest 字段或 signature 篡改拒绝。
6. manifest path/owned path 逃逸拒绝。
7. v1/未知 schema 无授权能力。
8. finalize 后新签名有效，旧签名不再匹配新 payload。

### 9.2 可信文档边界测试

1. 无 hermetic 环境的普通 `/private/tmp` 文件继续拒绝。
2. 有效 run root 内 synthetic 文档通过，并继续执行 raw-vault、大小和隐私校验。
3. 同一有效 manifest 下的 root 外临时文件拒绝。
4. 伪造 manifest、只设置 `MNEMOS_TEST_RUN=1`、只设置 root/hash 均拒绝。
5. symlink、symlink parent 逃逸、FIFO/设备等既有负向标准不降低。
6. FileIngestor 与 DocumentImportService 对同一路径给出相同判定。

### 9.3 上层真实链路

```text
python3 scripts/run_tests.py quick
python3 scripts/run_tests.py integration
python3 scripts/run_tests.py heavy
python3 scripts/e2e_wow_probe.py --mock-llm
```

不得把失败测试移层、skip、xfail、改写期望，或通过扩大临时目录 allowlist 获得绿色结果。

## 10. 文档与审计同步

实现完成后同步所有受影响的正式资产：

1. `README.md`、`README-en.md`、`docs/ARCHITECTURE.md`、`docs/OPS_MANUAL.md` 与测试验证层审计报告中的 hermetic schema/安全边界。
2. `docs/CHANGELOG.md` 记录根因、实现、测试数字和提交证据。
3. `docs/acceptance/document_asset_manifest.json` 中受影响文档的精确 hash/consumer 契约。
4. Desktop `mnemos系统图谱` 当前说明和生成的 `86-99` 索引/facts，绑定实现后的 repo commit。
5. Desktop `mnemos深度全量审计-2026-07-12.md` 的 ROOT-001 原位更新为已修复，并列出 focused、Quick、Integration、Heavy、Wow、local gates 与残余 ROOT-002 边界。

文档不得先写“已修复”再补代码证据；只有实现、复验和提交完成后才能更新状态。

## 11. 验收顺序与关闭条件

实现阶段按以下顺序闭环：

1. attestation helper 负向/正向测试红。
2. 实现 manifest v2 签发与验证，focused tests 绿。
3. 接入 canonical document validator，文档导入相关测试绿。
4. 运行 Quick、Integration、Heavy 和 mock Wow，确认原 31+1 失败全部消失。
5. 运行 `python3 scripts/run_local_gates.py`、文档资产 strict 审计与 `git diff --check`。
6. 深审安全边界：搜索 caller bypass、测试 monkeypatch、secret 输出、v1 fallback 和 run-root 外放行。
7. 同步 repo/Desktop 文档与源审计文档，独立提交 ROOT-001。

ROOT-001 关闭的机器可断言证据至少包含：

```text
manifest_schema == mnemos.hermetic_run_environment.v2
attestation_algorithm == hmac-sha256
secret_leak_count == 0
valid_run_owned_temp_document == accepted
unsigned_temp_document == rejected
outside_run_temp_document == rejected
quick_failures_attributed_to_ROOT_001 == 0
integration_failures_attributed_to_ROOT_001 == 0
heavy_failures_attributed_to_ROOT_001 == 0
wow_document_import == pass
```

若 full-score 仍因 empty sandbox 的 live-runtime prerequisites 失败，只能归入 ROOT-002，不能回退 ROOT-001 的路径证明，也不能把 full-score 假标为 release eligible。
