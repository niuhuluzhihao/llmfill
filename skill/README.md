# llmfill skill（LLMFill Agent Skill）

将 [LLMFill](https://www.llmfill.com) 的 **Word 文档智能填写** 与 **知识库管理**
能力封装为 Agent Skill：上传 .docx 让 AI 自动填表并下载结果；建知识库、
传资料供填写时检索取答案。适用于 Claude Code / Claw 等 agent 应用。

## 安装（SkillHub）

```bash
npx clawhub install llmfill        # 或按所在平台的 skill 安装方式
```

## 快速开始

```bash
# 1. 首次配置（只填 API Key，服务地址内置；无 Python 见 references/curl-guide.md）
python scripts/llmfill.py config

# 2. 填一份表
python scripts/llmfill.py fill 表单.docx --json

# 3. 建知识库，填表时挂载
python scripts/llmfill.py kb create --name "资料库"
python scripts/llmfill.py kb upload <kb_id> 手册.pdf
python scripts/llmfill.py fill 表单.docx --kb <kb_id>
```

详见 `SKILL.md`（skill 入口）与 `examples/`。

## 特性

- **零依赖**：纯 Python 3.10+ 标准库，无需 pip install
- **端到端**：fill 命令内部完成 上传 -> 轮询 -> 下载 全流程
- **agent 友好**：全命令 `--json` 输出；`discover` 自动发现接口
- **统一错误**：网关级错误码（见 references/errors.md）

## 版本

- `0.1.0`（beta）：首发。填写/知识库/检索/whoami/discover。
  SkillHub 标签 `beta`；稳定后发布 `1.0.0` 并转 `latest`。

## 结构

```
skill-llmfill/
├── SKILL.md              # skill 入口（frontmatter: name/description）
├── scripts/
│   ├── llmfill.py        # 主 CLI
│   ├── api_client.py    # 统一 HTTP（X-Auth-Token、错误解析、multipart）
│   └── config.py        # ~/.llmfill/config.json 读写
├── references/           # 接口详解（渐进式披露）
├── examples/             # 端到端示例
└── tests/                # pytest（25 用例：配置/错误解析/multipart/轮询/CLI）
```

## 测试

```bash
python -m pytest tests/ -q
```
