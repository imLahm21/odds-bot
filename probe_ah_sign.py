"""一次性探针：验证 API-Football 亚盘 value 的符号约定（主热 / 客热两种情况）。

判据（不依赖任何既有假设）：
  用同场欧赔算去抽水的主胜概率 p_home，再对每条亚盘线算「主侧去抽水概率」
  p_side = (1/主水)/((1/主水)+(1/客水))。
  平衡线（p_side≈0.5）所在的 raw 值符号，与主队强弱的关系即为 API 约定：
    主队强（p_home 高） → 平衡线应在「主队让出」那一侧
    主队弱（p_home 低） → 平衡线应在「主队受让」那一侧
"""
import os, sys, json, time
from datetime import datetime, timezone, timedelta
import requests
from dotenv import load_dotenv

load_dotenv()
KEY = os.getenv("APIFOOTBALL_KEY", "").strip()
if not KEY:
    sys.exit("no APIFOOTBALL_KEY")
BASE = "https://v3.football.api-sports.io"
H = {"x-apisports-key": KEY}


def get(path, **params):
    r = requests.get(f"{BASE}/{path}", headers=H, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def devig2(a, b):
    ia, ib = 1 / a, 1 / b
    return ia / (ia + ib)


def analyze(fx_id, label):
    data = get("odds", fixture=fx_id)
    if not data.get("response"):
        print(f"  [{label}] fixture {fx_id}: 无赔率")
        return None
    r = data["response"][0]
    out = []
    for bm in r["bookmakers"]:
        h2h = ah = None
        for bet in bm["bets"]:
            if bet["id"] == 1:
                vs = {v["value"]: float(v["odd"]) for v in bet["values"]}
                if "Home" in vs and "Away" in vs:
                    h2h = vs
            if bet["id"] == 4:
                lines = {}
                for v in bet["values"]:
                    p = v["value"].split()
                    if len(p) != 2:
                        continue
                    side, num = p[0].lower(), float(p[1])
                    if side not in ("home", "away"):
                        continue
                    lines.setdefault(num, {})[side] = float(v["odd"])
                ah = {k: v for k, v in lines.items() if "home" in v and "away" in v}
        if h2h and ah:
            out.append((bm["name"], h2h, ah))
    if not out:
        print(f"  [{label}] fixture {fx_id}: 无亚盘+欧赔")
        return None

    # 全庄平均主胜概率
    ph = sum(devig2(h["Home"], h["Away"]) for _, h, _ in out) / len(out)
    print(f"\n=== [{label}] fixture {fx_id} ===")
    print(f"  欧赔去抽水 主胜(vs客胜) = {ph:.1%}  → 主队{'强' if ph > .5 else '弱'}")
    for name, h2h, ah in out[:4]:
        bal = [(raw, w) for raw, w in sorted(ah.items())
               if 0.42 <= devig2(w["home"], w["away"]) <= 0.58]
        s = "  ".join(f"raw={raw:+g}(主{w['home']}/客{w['away']})" for raw, w in bal)
        print(f"    {name:14s} 平衡线: {s or '无'}")
    # 汇总：平衡线 raw 的符号
    allbal = [raw for _, _, ah in out for raw, w in ah.items()
              if 0.42 <= devig2(w["home"], w["away"]) <= 0.58]
    if allbal:
        avg = sum(allbal) / len(allbal)
        print(f"  → 平衡线 raw 均值 = {avg:+.2f}")
        return ph, avg
    return None


now = datetime.now(timezone.utc)
res = []
for d in range(0, 4):
    day = (now + timedelta(days=d)).strftime("%Y-%m-%d")
    fx = get("fixtures", date=day, timezone="UTC")
    cands = [f for f in fx.get("response", [])
             if f["fixture"]["status"]["short"] == "NS"]
    for f in cands[:40]:
        if len(res) >= 6:
            break
        fid = f["fixture"]["id"]
        nm = f"{f['teams']['home']['name']} vs {f['teams']['away']['name']}"
        got = analyze(fid, nm)
        if got:
            res.append((nm, *got))
        time.sleep(0.4)
    if len(res) >= 6:
        break

print("\n\n########## 汇总 ##########")
print(f"{'比赛':46s} {'主胜%':>7s} {'平衡线raw':>10s}")
for nm, ph, avg in res:
    print(f"{nm[:44]:46s} {ph:6.1%} {avg:+10.2f}")
print("""
判读：
  若「主胜% 高（主强）」对应「平衡线 raw 为负」→ API: 负=主队让出，正=主队受让
     则内部 handicap 应 = raw（不取反，因 CLAUDE.md 也是 负=让出）
  若「主胜% 高（主强）」对应「平衡线 raw 为正」→ API: 正=主队让出
     则内部 handicap 应 = -raw（取反，即现有代码）
""")