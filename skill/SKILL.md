---
name: llmfill
description: |
  调用 LLMFill（llmfill.com）远程服务，用 AI 自动填写 Word 文档中的
  表单/问卷/清单等，并管理供填写时检索的知识库。当用户要求"AI 填表/
  自动作答/批量填写 .docx"、"建知识库/上传参考资料"、"查余额或验证
  API 令牌"时使用。核心能力：上传 .docx -> 异步填写（自动轮询）-> 下载
  带颜色标注或纯净版结果；答案来源可选知识库/联网/混合/纯大模型。
  首次使用需在 https://www.llmfill.com/profile 「API 密钥」生成 aif_
  开头的令牌；无 Python 环境时可用 curl 等价调用（见 curl-guide.md）。
  不提供直接检索接口 -- 知识库通过 fill --kb 挂载使用。
---

# LLMFill Skill

## 何时使用

- 用户给 .docx 模板，要求"AI 填一下""自动作答""批量填表" -> `fill`
- 用户要"建知识库""把资料传上去给 AI 用" -> `kb create` / `kb upload`
- 用户要"查余额/令牌是否有效" -> `whoami`
- 需要了解服务当前有哪些可用接口 -> `discover`

注：本 skill 不提供直接检索（search）--检索是服务端填写链路的内部行为，
知识库通过 `fill --kb` 挂载使用。

## 首次配置（必须）

**第一步：确认运行通道**

```bash
python --version 2>/dev/null || python3 --version 2>/dev/null
```

- **有 Python（3.10+）**-> 用下方 CLI 命令（推荐：自动轮询/下载/错误解析）
- **无 Python** -> 用 curl 直接调 API，完整操作手册见 `references/curl-guide.md`
  （curl 在 Windows 10+/Linux/macOS 均自带，功能完全等价）

**第二步：配置 API Key（两通道通用）**

CLI 方式（只填 Key，服务地址已内置）：

```bash
python scripts/llmfill.py config
# 按提示粘贴 aif_ 开头的令牌即可；完成后自动自检并显示余额
# 也可带参数：python scripts/llmfill.py config --token aif_xxx
```

令牌获取：引导用户去 https://www.llmfill.com/profile 注册登录 ->「API 密钥」
-> 创建令牌 -> 复制 `aif_` 开头字符串（忘记可随时点"查看令牌"再次获取，令牌永久不变）。

配置存于 `~/.llmfill/config.json`（权限 600）。**服务地址默认内置
`https://www.llmfill.com`，无需输入**；自建/代理部署时可直接编辑该 JSON
的 `base_url` 字段（或 `config --base`、环境变量 `LLMFILL_BASE_URL`），
修改后所有命令即用新地址。

注意：注册新用户（邮箱验证/微信绑定）必须在网页完成，无法通过本 skill 注册。

## 核心命令

所有命令加 `--json` 可获得机器可读输出（推荐 agent 使用）。

```bash
# 智能填写：上传 -> 自动轮询 -> 下载结果到模板所在目录
python scripts/llmfill.py fill 表单.docx
python scripts/llmfill.py fill 尽调问卷.docx --kb kb-xxx --source hybrid
python scripts/llmfill.py fill a.docx b.docx --clean          # 只下纯净版（默认两个版本都下）
python scripts/llmfill.py fill-status b_xxx                   # 查批次进度
python scripts/llmfill.py fill-download b_xxx --clean --out ./结果/

# 知识库
python scripts/llmfill.py kb ls                               # 列出
python scripts/llmfill.py kb create --name "公司资料" --desc "..."
python scripts/llmfill.py kb upload kb-xxx 资料.pdf 手册.docx  # 入库（自动轮询）
python scripts/llmfill.py kb docs kb-xxx                      # 列文档（含入库状态）
python scripts/llmfill.py kb rm kb-xxx                        # 删除

# 账号
python scripts/llmfill.py whoami                              # 令牌自检 + 余额
python scripts/llmfill.py discover                            # 接口自动发现
```

## 关键语义

- **计费**（按 token 用量）：费用知情的三个节点--
  ① **提交后**：fill 立即打印「预估费用约 X 元（实际以处理结果为准）」，
  取 upload 响应的 `cost_estimate`，与后端入队前校验同口径；
  ② **上传时**：余额不足 llm-office 入队前直接拒单（402 `INSUFFICIENT_QUOTA`），
  message 含本次预估费用与充值指引；
  ③ **完成时**：实扣金额从 status 的 `tasks[].cost` 提取，fill 返回
  `actual_cost`，**向用户汇报以它为准**。不会白跑算力。
- **答案来源** `--source`：`knowledge_base`（仅知识库）/ `internet`（联网）/
  `hybrid`（混合，默认）/ `llm_only`（纯大模型，忽略 kb）。
- **异步与完成通知**：fill 与 kb upload 是异步任务，命令内部自动轮询（约
  30 分钟超时），**无需用户/agent 手动建轮询 automation**——fill 单条命令
  完成「上传 → 轮询 → 下载 → 主动汇报实际费用与结果绝对路径」，完成即报告。
  kb upload 的入库进度由文档列表 `parse_status` 派生（kb docs 可查）。
- **阶段进度**：处理分 5 阶段 `upload → generate_questions → retrieve_questions
  → fill_answers → finalize`，其中 `retrieve_questions`（检索知识库）最耗时，
  进度可能长时间停在 60% 附近——这是正常的检索等待，不是卡死。fill 会在进度
  后附带「阶段：检索知识库」提示（服务端 status 的 `tasks[].stages` 提供）。
- **结果路径**：fill 默认下载到**第一个模板所在目录**（`--out` 覆盖），并**同时
  下载标注版（`_processed.docx`）与纯净版（`_clean.docx`）两个文件**，完成时打印
  两个绝对路径；`--clean` 只下纯净版。
- **认证**：所有请求带 `X-Auth-Token: aif_xxx` 头（不是 Authorization: Bearer）。

## 排错

- `INVALID_TOKEN`：令牌无效/已删除 -> 重新 `config`（网页上删除令牌会使其立即失效；令牌永久有效、可反复查看，不存在"重置"）
- `NETWORK_ERROR`：base URL 不通或服务维护
- `QUOTA_EXCEEDED`：知识库数/文件数超上限（默认 10 个/库）
- `FILE_TOO_LARGE`：单文件超 10MB
- `METHOD_NOT_ALLOWED` / `NOT_FOUND`：接口不存在（如试图直接检索--本 skill 不提供）
- 完整错误码表见 `references/errors.md`

## 深入阅读（按需加载）

- `references/curl-guide.md` - **无 Python 环境的 curl 操作指南**（等价功能）
- `references/api-documents.md` - 文档填写接口详解
- `references/api-knowledge.md` - 知识库接口详解
- `references/api-account.md` - 账号/配置/whoami
- `references/errors.md` - 统一错误码表
- `examples/fill-a-form.md` - 端到端填写示例（含轮询细节）
- `examples/build-kb.md` - 建库-入库示例
