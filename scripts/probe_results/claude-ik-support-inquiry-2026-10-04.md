# IK Claude 参数透传确认询问

状态：未发送。用户已决定不再将 IK 参数确认作为接入前置条件，随后授权正式添加 Claude。
日期：2026-10-04（Asia/Taipei）。本文件为历史询问草稿，不含测试密钥。

收件人：support@ikuncode.cc

主题：请确认 Claude Opus/Sonnet 5.5 的 max_tokens 与思考强度透传规则

## 可直接发送的正文

您好，我们准备通过贵方 `https://api.ikuncode.ai/v1` 接入
`claude-opus-5-5` 和 `claude-sonnet-5-5`，需要明确输出预算和思考强度是否被改写。
测试日期为 2026-10-04；请求只含一道四分之一凯利小算题，没有发送业务数据或长报告。

两款模型在原生 Messages 和 OpenAI Chat Completions 接口均正常返回，
OpenAI 兼容流式解析也正常。但参数对照测试发现以下现象。

### 1. 输出预算对照

| 接口 | 模型 | 请求 max_tokens | usage 输出 tokens | 结束原因 |
|---|---|---:|---:|---|
| /v1/messages | claude-opus-5-5 | 1 | 47 | end_turn |
| /v1/messages | claude-sonnet-5-5 | 1 | 56 | end_turn |
| /v1/chat/completions | claude-opus-5-5 | 1 | 52 | stop |
| /v1/chat/completions | claude-sonnet-5-5 | 1 | 95 | stop |

兼容流式接口的 `max_tokens=128000` 请求被接受；兼容非流式接口的
`max_tokens=128001` 请求也被接受。我们没有要求模型生成 128K 正文，
所以这些结果仅反映参数接受情况，不代表测出了实际输出上限。

原生复现请求如下（密钥应由本地环境提供，不要贴入公开工单）：

```bash
curl https://api.ikuncode.ai/v1/messages \
  -H "x-api-key: $IK_CLAUDE_TEST_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-opus-5-5",
    "max_tokens": 1,
    "output_config": {"effort": "high"},
    "messages": [{
      "role": "user",
      "content": "Compute quarter Kelly for bankroll 100, decimal odds 1.95, win probability 0.56. Reply only with the final amount rounded to two decimals."
    }]
  }'
```

将 model 换为 `claude-sonnet-5-5` 即可复测另一模型。
兼容接口使用 Bearer 认证、同一道算题及 `reasoning_effort: "high"`。

### 2. 思考强度与无效参数对照

- 原生接口发送 `output_config.effort` 为 low/medium/high/xhigh/max 时，两款模型均返回 200。
- 发送 none/ultra/definitely_invalid_effort 时，两款模型也均返回 200。
- 显式加入 `thinking: {"type": "adaptive"}` 后，无效 effort 仍被接受。
- 原生接口发送 `thinking: {"type": "definitely_invalid"}` 也被接受。
- 兼容接口发送 `reasoning_effort: "definitely_invalid_effort"` 同样返回 200。

我们理解 HTTP 200 不能证明强度实际生效；需要贵方确认是否过滤、映射或回落这些字段。

### 3. 请逐项确认

1. 两个接口是否原样透传 max_tokens？是否存在最小值、固定默认值、自动提升、
   上游覆盖或上限裁剪？两款模型在这条线路上的实际支持范围分别是多少？
2. 原生接口是否透传 output_config.effort？支持哪些档位？未知值会报错、忽略，
   还是回落到指定默认值？两个模型的实际默认强度是什么？
3. 兼容接口的 reasoning_effort 是否映射到原生 output_config.effort？
   是否需要同时传 thinking？请提供当前线路有效的原生与兼容请求示例。
4. 哪个接口/令牌服务分组可以可靠控制这些参数？若受令牌设置或分组策略影响，
   请指出需要查看的具体设置。我们需在保留长输入的条件下控制输出预算并选择推理强度。

希望答复明确到这两个模型及该 Base URL 的当前线路，而不只是模型官方通用规格。
谢谢。

## 官方资料及后续判定

- [IK 官方售后渠道](https://docs.ikuncode.ai/support/after-sales)：列出上述支持邮箱。
- [IK Claude 配置说明](https://docs.ikuncode.ai/deploy/claude-code)：要求 Claude 专用令牌分组。
- [IK Token 设置](https://docs.ikuncode.ai/guide/modify-token)：说明配额、速率和开关，未说明生成参数透传。
- [Claude 官方 effort 文档](https://platform.claude.com/docs/en/build-with-claude/effort)：原生字段为 output_config.effort。

收到回复后，按 IK 提供的有效请求格式重新测试：低预算是否截断/报错、
非法强度是否按说明处理、合法五档是否接受，以及流式的 usage 和结束原因。
兼容接口满足要求时优先复用现有客户端；只有原生接口满足要求时再评估原生适配；
两者都不满足时，新增本地 ik_claude 名称也不能保证参数控制有效。

本地计划分组 `ik_claude` 仅是项目路由名称，不等于 IK 服务端的令牌服务分组。
编写询问时尚未注册该分组；目前已按用户授权登记 ik_claude 和两款 Claude，本文不再待发送。
