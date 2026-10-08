# -*- coding: utf-8 -*-
"""执行速度链路审计（PC 侧）。

背景（用户反馈）："执行点位的实时不要按示教器走，按电脑的多少走"。
本工具回答两个问题：

  ① `--probe`（默认，**只读、不下发**）：PC 能看到的一切 —— 档位、就绪、速度寄存器现值、
     配置里的速度补偿，以及"PC 侧唯一写速度的地方在哪"。
  ② `--run`（**会动机器人**）：同一段位移在多个 PC 速度下各走一次并计时，
     算"每 1% 对应多少 °/s"是否恒定 —— 用来判定 PC 的百分比到底有没有被钳位/倍率压着。

判定法（不需要知道机器人 100% 有多快）：
  · 若各速度下 "°/s ÷ %" 基本一致 → PC 的百分比**线性生效**（只可能差一个固定倍率）
  · 若高速档明显偏小（比值不随百分比等比变化）→ 有**钳位**（档位限速 / 示教器倍率压死）
  · 若各速度耗时几乎相同 → 被压成了固定低速，PC 的数字实际没起作用

★ 纪律：AI 做真机验证只跑 5%。所以 `--run` 默认只跑 5%；要跑更快由操作员显式指定
   `--speeds 5,10,20`。运行前必须 AUTO 档 + 一键就绪。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
_OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _pw() -> str:
    v = os.environ.get("EFORT_ADMIN_PASSWORD", "").strip()
    if v:
        return v
    env = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    for ln in open(env, encoding="utf-8-sig"):
        s = ln.strip()
        if s.startswith("EFORT_ADMIN_PASSWORD="):
            return s.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("未找到管理员口令：设 EFORT_ADMIN_PASSWORD 或在 .env 里配置")


def call(method, path, body=None, tok=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"}
    if tok:
        h["X-Control-Token"] = tok
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with _OP.open(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else {}
        except Exception:
            return e.code, {"raw": raw.decode("utf-8", "replace")}
    except Exception as e:
        return 0, {"error": str(e)}


def _msg(d) -> str:
    m = d.get("message") if isinstance(d, dict) else None
    if isinstance(m, str):
        return m
    if isinstance(m, dict):
        return str(m.get("message") or m)[:120]
    return str((d or {}).get("error") or (d or {}).get("detail") or d)[:120]


def probe(tok):
    print("=" * 68)
    print(" ① PC 侧只读体检（不下发任何指令）")
    print("=" * 68)
    st, rm = call("GET", "/api/control/run-mode", tok=tok)
    print(" 档位        : %s  (joggable=%s)"
          % (rm.get("observed_mode") or rm.get("mode"), rm.get("joggable")))
    st, rc = call("GET", "/api/rc-status", tok=tok)
    print(" 就绪        : 状态字=%s 报警=%s/%s 程序号=%s"
          % (rc.get("status_word"), rc.get("alarm1"), rc.get("alarm2"), rc.get("prog")))
    print(" 40003 运行速度(控制器回读) : %s %%" % rc.get("speed_pct"))
    print(" 40103 速度设定            : 见下（PC 写什么这里就是什么，写后还会比对）")

    # 配置里的速度补偿：决定 40103 = UI% ÷ (vperc/100)
    # ★ 读配置要么读到真值，要么**明说读不到** —— 绝不能因为导入失败就印出
    #   "补偿不是 100" 这种假警报（诊断工具印错结论比没有更糟）。
    cfg, cfg_err = None, ""
    try:
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        sys.path.insert(0, os.path.join(root, "backend"))
        from app.core.config import get_config
        cfg = get_config().get("motion", "jog", "service_program_vperc", default=None)
    except Exception as e:  # noqa: BLE001
        cfg_err = str(e)[:80]
    if cfg is None:
        print(" 速度补偿    : ✗ 读不到配置（%s）" % (cfg_err or "值为空"))
        print("               请直接看 config/robot.yaml 的 motion.jog.service_program_vperc")
    else:
        try:
            v = float(cfg)
        except (TypeError, ValueError):
            v = -1.0
        print(" 速度补偿    : service_program_vperc=%s  → 40103 = UI速度 ÷ %s" % (cfg, v / 100.0))
        if v == 100.0:
            print("               ✅ 补偿为 100 ⇒ 40103 就等于右上角那个数（不再翻倍）")
        elif v > 0:
            print("               ⚠ 补偿=%s ≠ 100 ⇒ 40103 ≠ UI 速度，示教器会显示另一个数；"
                  "且必须与控制器 210 里的 v…perc 一致，否则实际速度会差 %s 倍"
                  % (cfg, round(100.0 / v, 2)))

    print()
    print(" ★ 结论：PC 侧**只有一处**写速度（motion.command → 40103），且写后立刻回读比对，")
    print("   不一致会当场中止下发 → 寄存器没有被示教器改写。")
    print("   所以若现场仍「不按电脑走」，只可能是下面三者之一（PC 都读不到）：")
    print("     a) 档位不是 AUTO              → PC 能看出来（上面那行）")
    print("     b) 控制器程序里不是 v100perc   → 只能到示教器看 210 程序里的 MJOINT 常量")
    print("     c) 控制器全局速度倍率(override) → 只能到示教器上看/关掉那个倍率")
    print()


def run_case(tok, joint, deg, speed, timeout):
    st, rc = call("GET", "/api/rc-status", tok=tok)
    cur = [float(x) for x in (rc.get("joints") or [])]
    if len(cur) != 6:
        return None, "拿不到当前位姿"
    tgt = cur[:]
    tgt[joint - 1] = cur[joint - 1] + deg
    t0 = time.time()
    st, r = call("POST", "/api/control/move",
                 {"joints": [round(v, 3) for v in tgt], "speed_pct": speed},
                 tok=tok, timeout=timeout)
    el = time.time() - t0
    if not r.get("ok"):
        return None, "下发失败 HTTP %s %s" % (st, _msg(r))
    # 走完立刻回原位（同一速度），让位置不漂
    back = cur
    call("POST", "/api/control/move",
         {"joints": [round(v, 3) for v in back], "speed_pct": speed}, tok=tok, timeout=timeout)
    return el, ""


def run_test(tok, joint, deg, speeds):
    print("=" * 68)
    print(" ② 实测：J%d 走 %.1f°，各 PC 速度下计时（会动机器人！）" % (joint, deg))
    print("=" * 68)
    rows = []
    for sp in speeds:
        el, err = run_case(tok, joint, deg, sp, timeout=180)
        if el is None:
            print("   %3d%%  ✗ %s" % (sp, err))
            continue
        dps = deg / el if el > 0 else 0.0
        rows.append((sp, el, dps, dps / sp if sp else 0))
        print("   %3d%%  耗时 %.2fs   实测 %.2f°/s   每 1%% 对应 %.3f°/s" % (sp, el, dps, dps / sp))
    if len(rows) >= 2:
        per = [r[3] for r in rows]
        spread = (max(per) - min(per)) / (sum(per) / len(per)) * 100 if sum(per) else 0
        print()
        print("   每 1%% 对应 °/s 的离散度：%.1f%%" % spread)
        if spread <= 15:
            print("   → ✅ PC 的百分比**线性生效**（各档一致）。若绝对值偏慢，是固定倍率差")
            print("      （多半是控制器程序里的 v…perc 不是 v100perc，或示教器有全局倍率）")
        else:
            print("   → ⚠ 各速度下不成比例 ⇒ 有**钳位/倍率**压着（档位限速或示教器 override）")
            print("      → 请到示教器确认：① 档位 AUTO ② 程序 210 的 MJOINT 是 v100perc ③ 全局倍率=100%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="实测（会动机器人）；默认只做只读体检")
    ap.add_argument("--joint", type=int, default=6, help="用哪个轴做计时（默认 J6，行程最短最安全）")
    ap.add_argument("--deg", type=float, default=6.0, help="每次走多少度（默认 6°）")
    ap.add_argument("--speeds", default="5", help="逗号分隔的 PC 速度%%，默认只跑 5（AI 纪律）")
    a = ap.parse_args()

    st, d = call("POST", "/api/auth/login", {"password": _pw()})
    tok = d.get("token")
    if not tok:
        print("登录失败"); return 1
    probe(tok)

    if not a.run:
        print("（只读体检完成。要实测加 --run；默认只跑 5%，更快由操作员显式指定 --speeds 5,10,20）")
        return 0

    st, rm = call("GET", "/api/control/run-mode", tok=tok)
    if "AUTO" not in str(rm.get("observed_mode") or rm.get("mode") or "").upper():
        print("✗ 当前不是 AUTO 档，下发了也不会动。请把控制器旋钮拨到 AUTO 后重试。")
        return 1
    call("POST", "/api/control/run-mode", {"mode": "AUTO", "note": "speed audit"}, tok=tok)
    speeds = [int(x) for x in str(a.speeds).split(",") if x.strip().isdigit()]
    if not speeds:
        print("✗ --speeds 解析为空"); return 1
    run_test(tok, a.joint, a.deg, speeds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
