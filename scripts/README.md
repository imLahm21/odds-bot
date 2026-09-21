# 工具脚本

所有命令都从项目根目录运行。Python 工具统一使用 `python -m scripts.<模块名>`，
避免脚本移动后出现导入路径或相对路径错误。

| 脚本 | 用途 | 常用命令 |
|---|---|---|
| `build_calibration.py` | 从 `竞彩.xlsx` 重算置信度、方向命中率和 ROI 校准表 | `python -m scripts.build_calibration --out scripts/output/calibration_out.md` |
| `dump_catalog.py` | 从缓存或 API-Football 导出联赛与博彩公司目录 | `python -m scripts.dump_catalog` |
| `probe.py` | API-Football 套餐、联赛、赛程和盘前赔率结构探针 | `python -m scripts.probe leagues` |
| `probe_live.py` | 滚球盘口和滚球 bet 类型探针 | `python -m scripts.probe_live` |
| `probe_ah_sign.py` | 复核亚盘 value 的主客队符号约定 | `python -m scripts.probe_ah_sign` |
| `probe_llm.py` | LLM 连通性、档位、完整报告和端点池诊断 | `python -m scripts.probe_llm pool heavy` |
| `probe_llm_efforts.py` | 逐模型、逐端点验证 reasoning effort | `python -m scripts.probe_llm_efforts --help` |
| `convert_wechat_copy_ready.ps1` | 把文章 HTML 转成微信公众号可复制编辑版本 | `pwsh -File scripts/convert_wechat_copy_ready.ps1 ...` |
| `setup_env_watch.sh` | 在服务器安装 systemd `.env` 变更监听 | `bash scripts/setup_env_watch.sh` |

## 输出目录

`scripts/output/` 保存脚本生成的本地结果：

- `calibration_out.md`：当前校准结果快照，已纳入 Git。
- `catalog_leagues.txt`、`catalog_bookmakers.txt`：可重新生成的本地目录，继续由 `.gitignore` 排除。

API 探针的原始 JSON 继续写入项目根目录的 `probe_samples/`，该目录不纳入 Git。

`build_calibration.py` 的完整参数和统计口径见 [build_calibration.md](build_calibration.md)。
