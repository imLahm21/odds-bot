# Claude 5.5 接入验证

日期：2026-10-04（Asia/Taipei）。用户已明确授权正式添加，参数透传确认不再作为前置条件。

## 注册与配置

| 模型 | 可选池 | 密钥组 | 思考强度 | 本地输出预算 | 本地输入预警线 |
|---|---|---|---|---:|---:|
| claude-opus-5-5 | heavy | ik_claude | low/medium/high/xhigh/max | 128000 | 700000 |
| claude-sonnet-5-5 | heavy、balanced | ik_claude | low/medium/high/xhigh/max | 128000 | 700000 |

使用 `https://api.ikuncode.ai/v1/chat/completions` 的 OpenAI 兼容路径，
复用现有客户端的 Bearer 认证、阻塞/流式解析与 reasoning_effort 请求字段。
没有新增原生 Messages 适配。默认主模型、回退槽、固定全模型会诊任务保持原配置。
管理员开放五档，访客沿用 low/medium/high 限制。两款都不进入 light 池。

正式凭据由用户在现有 LLM_ROUTE_ENDPOINTS 值末尾追加以下条目（先加逗号）：

```text
ik_claude|<正式Claude密钥>|https://api.ikuncode.ai/v1|IK-Claude
```

两款的预算映射已写入本地 .env；.env 不随 Git 同步，部署时需同步正式凭据与预算。

## 接入后的真实小请求

在独立 Python 子进程中临时追加测试端点，不写入 .env 或其他项目文件。
通过现有 `chat_model` 和 `stream_chat_model` 公开调用入口测试，均由正式模型注册与
ik_claude 路由解析。并行度为 2，每款仅测试一次阻塞与一次流式请求。

题目：本金 100、十进制赔率 1.95、胜率 56%，四分之一凯利，正确金额为 2.42。
均指定 high；阻塞请求预算 4096，流式 max_tokens=0 从 .env 解析到 128000。

| 模型 | 阻塞耗时 ms | 阻塞答对 | 流式耗时 ms | 流式事件 | 流式答对 |
|---|---:|---|---:|---|---|
| claude-opus-5-5 | 4866 | 是 | 5184 | 1 delta、1 done | 是 |
| claude-sonnet-5-5 | 3260 | 是 | 2942 | 25 delta、1 done | 是 |

两款配置解析均为 ik_claude，测试端点数为 1，输入预警线为 700000，无错误事件。
这些小请求验证接入与正文处理，不是长 SOP 报告评测，也不证明第三方严格执行参数上限。

## 离线验证

- test_llm_models.py：54 项通过。
- test_multi_analyzer.py：12 项通过。
- Python 编译检查与 git diff --check 通过。
- 覆盖新增池、管理员/访客主模型和回退选择保存、轻档隔离、独立密钥选择、
  思考强度键盘、兼容请求字段及探针端点筛选。

## 参考

- [Claude 官方强度文档](https://platform.claude.com/docs/en/build-with-claude/effort)：两款官方五档。
- [Claude 模型规格](https://platform.claude.com/docs/en/models/overview)：1M 上下文、128K 输出。
- 先前参数对照证据保留在 claude-ik-support-inquiry-2026-10-04.md，询问未发送，
  用户已明确选择不将该问题作为接入阻碍。
