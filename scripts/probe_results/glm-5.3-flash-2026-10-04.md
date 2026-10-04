# GLM-5.3 Flash 思考强度复测

日期：2026-10-04（Asia/Taipei）。使用本地 `.env` 的两条 `ik_glm` 端点。
端点标签中的 official 不等于独立直连 Z.ai 官方；本记录只描述这些已配置端点的行为。

```powershell
python -B -m scripts.probe_llm_efforts --models glm-5.3-flash --efforts none,low,medium,high,xhigh,max,ultra --force-efforts --all-endpoints --timeout 120 --max-tokens 4096
python -B -m scripts.probe_llm_efforts --models glm-5.3-flash --efforts definitely_invalid_effort --force-efforts --all-endpoints --timeout 60 --max-tokens 4096
```

提示词与 GPT-6.1 Sol 探针相同，为四分之一凯利小算题。强制发送指定 reasoning_effort，
绕过本地能力过滤，仅用于诊断。通过生产客户端的 Chat Completions 探针入口发送。

| 请求强度 | IK-GLM 状态 / 耗时 ms / 推理 tokens | IK-GLM-official 状态 / 耗时 ms / 推理 tokens |
|---|---|---|
| none | 200 / 3445 / 86 | 200 / 5666 / 142 |
| low | 200 / 10760 / 108 | 200 / 4929 / 180 |
| medium | 200 / 9940 / 119 | 200 / 5300 / 192 |
| high | 200 / 3539 / 117 | 200 / 4884 / 190 |
| xhigh | 200 / 2387 / 72 | 200 / 5813 / 192 |
| max | 200 / 6113 / 104 | 200 / 5531 / 265 |
| ultra | 200 / 2403 / 71 | 200 / 5180 / 212 |
| definitely_invalid_effort（无效对照） | 400 / 1141 / 无 usage | 200 / 5617 / 193 |

所有 HTTP 200 响应均回显 `model=glm-5.3-flash`、`finish_reason=stop`，输入为 48 tokens。
max 的输出 tokens 为 111 / 270。本次没有复现 max 被端点拒绝。

无效对照结果说明：IK-GLM 会校验 OpenAI 风格的枚举，但这不证明各枚举都对应 GLM
独立强度；IK-GLM-official 对无效枚举也正常返回，不能用 HTTP 200 判断强度生效。

[Z.ai 官方模型卡](https://huggingface.co/zai-org/GLM-5.3-Flash/blob/main/README.md)
声明仅有 low/high/max，省略或其他值会回落 max。
[vLLM 模型配方](https://recipes.vllm.ai/zai-org/GLM-5.3-Flash)
也说明模型持续启用思考，模板只区分 low/high，其余值解析为 max。
这能解释 none 仍出现推理用量，但不能证明第三方部署内部完全遵循该模板。

最终继续只开放 `low / high / max`。none/minimal/medium/xhigh/ultra 不登记为可选强度。
本次参数接受测试不能证明长报告可靠性，也不能将单次推理 token 数用于比较强度质量。
已有 finish_reason=length 仍属于输出预算耗尽，与 max 不支持是不同问题。
