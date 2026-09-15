"""逐模型、逐档位发送最小真实请求；输出不含密钥和正文。"""

from __future__ import annotations

import argparse
import json

from bot import config, llm_client


DEFAULT_MODELS = (
    "gpt-6-astra",
    "gpt-5.6-sol",
    "glm-5.3",
    "grok-4.6",
    "deepseek-v4-pro",
    "gemini-3.8-flash",
    "deepseek-v4.1-flash",
    "gpt-5.6-terra",
    "glm-5.3-flash",
)


def _endpoint_indices(model: str) -> tuple[int, ...]:
    groups = set(config.llm_route_groups_for_model(model))
    return tuple(idx for idx, endpoint in enumerate(llm_client.endpoints())
                 if endpoint.get("route_group") in groups)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe configured LLM reasoning-effort levels safely.")
    parser.add_argument(
        "--models", default=",".join(DEFAULT_MODELS),
        help="comma-separated model ids")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument(
        "--efforts", default="",
        help="optional comma-separated subset; still filtered by model support")
    parser.add_argument(
        "--force-efforts", action="store_true",
        help="diagnostic only: send requested efforts even if not registered")
    args = parser.parse_args()

    prompt = (
        "只回答最终数字：本金100，十进制赔率1.95，胜率56%，"
        "使用四分之一凯利时下注金额是多少？保留两位小数。")
    for model in [item.strip() for item in args.models.split(",")
                  if item.strip()]:
        indices = _endpoint_indices(model)
        if not indices:
            print(json.dumps({"model": model, "ok": False,
                              "error": "未配置对应密钥组"},
                             ensure_ascii=False))
            continue
        supported = config.llm_model_efforts(model)
        requested = tuple(item.strip() for item in args.efforts.split(",")
                          if item.strip())
        efforts = (requested if args.force_efforts and requested else
                   tuple(effort for effort in (requested or supported)
                         if effort in supported))
        print(json.dumps({"model": model, "planned_efforts": efforts},
                         ensure_ascii=False), flush=True)
        for effort in efforts:
            result = None
            endpoint = None
            for idx in indices:
                endpoint = llm_client.endpoints()[idx]
                try:
                    result = llm_client.probe_model(
                        idx, model, effort=effort, max_tokens=args.max_tokens,
                        timeout_seconds=args.timeout, prompt=prompt,
                        force_effort=args.force_efforts)
                except Exception as exc:  # 诊断工具必须继续测试其余档位
                    result = {"ok": False, "http_status": None,
                              "latency_ms": 0, "model": "",
                              "sent_effort": effort, "usage": None,
                              "finish_reason": "",
                              "error": f"探针异常：{exc!r}"}
                if result.get("ok"):
                    break
            assert result is not None and endpoint is not None
            usage = result.get("usage") or {}
            safe = {
                "model": model,
                "effort": effort,
                "endpoint": endpoint.get("label", f"#{idx + 1}"),
                "group": endpoint.get("route_group", ""),
                "ok": result.get("ok", False),
                "http_status": result.get("http_status"),
                "latency_ms": result.get("latency_ms"),
                "response_model": result.get("model", ""),
                "sent_effort": result.get("sent_effort", ""),
                "finish_reason": result.get("finish_reason", ""),
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "reasoning_tokens": usage.get("reasoning_tokens"),
                "error": result.get("error", ""),
            }
            print(json.dumps(safe, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
