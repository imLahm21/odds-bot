# GPT-6.1 Sol 思考强度探针

测试日期：2026-10-04（Asia/Taipei）。使用本地 `.env` 已配置的 `ik_gpt` 端点。
测试的是参数接受情况与最小请求完成情况，不代表长报告可靠性或各档推理质量。

```powershell
python -B -m scripts.probe_llm_efforts --models gpt-6.1-sol --efforts none,low,medium,high,xhigh,max,ultra --force-efforts --all-endpoints --timeout 120 --max-tokens 4096
python -B -m scripts.probe_llm_efforts --models gpt-6.1-sol --efforts minimal --force-efforts --all-endpoints --timeout 30 --max-tokens 4096
```

请求内容：本金 100、十进制赔率 1.95、胜率 56%，计算四分之一凯利下注金额。
通过生产客户端的诊断入口发送 Chat Completions，请求明确包含 `reasoning_effort`。

| 强度 | IK-GPT-Codex-Mixed | IK-GPT-Codex-Pro |
|---|---|---|
| none | HTTP 400，模型不支持 | HTTP 400，模型不支持 |
| minimal | HTTP 400，模型不支持 | HTTP 400，模型不支持 |
| low | HTTP 200，stop，5118 ms | HTTP 200，stop，5730 ms |
| medium | HTTP 200，stop，5341 ms | HTTP 200，stop，4985 ms |
| high | HTTP 200，stop，10914 ms | HTTP 200，stop，6300 ms |
| xhigh | HTTP 200，stop，7377 ms | HTTP 200，stop，6399 ms |
| max | HTTP 200，stop，6262 ms | HTTP 200，stop，8122 ms |
| ultra | HTTP 400，无效参数值 | HTTP 400，无效参数值 |

两条可用端点的成功响应均回显 `model=gpt-6.1-sol`，`reasoning_tokens=0`；
这只证明端点接受请求且正常结束，无法据此确认内部推理深度或排除网关静默转换。
成功响应的 output_tokens：Mixed 为 42/43/52/72/87，Pro 为 42/43/50/66/112
（依次对应 low/medium/high/xhigh/max）。

`IK-GPT-Codex` 对全部八档均返回 HTTP 503、`model_not_found`，错误正文明确表示
Codex 分组当前不可用，要求使用 Mixed/Pro；属于端点分组状态，不能据此否定强度。

最终注册五档：`low / medium / high / xhigh / max`。新模型使用独立能力常量，
不继承旧 Sol 的 `none / ultra`。进入 heavy 和 balanced 池，默认及回退槽不变。

OpenAI Docs 的 [GPT-6.1 Sol 模型页](https://developers.openai.com/api/docs/models/gpt-6.1-sol)
也声明这五档，排除 none/minimal，最大输出 128000 tokens；该输出上限来自文档，
本次探针预算为 4096，没有测试最大输出能力。
