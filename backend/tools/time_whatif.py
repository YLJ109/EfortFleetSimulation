# -*- coding: utf-8 -*-
"""★★ E3 反事实试跑：**离线计时**（决定界面显示的样本数 n 的唯一依据）。

为什么必须先跑这个脚本
--------------------
方案 §E3 里写的 `n=200` 是一个**未经计时**的数字 —— 一趟几段 × 每段一次保守包络扫掠
（`safety_eval.sweep` 里每拍都要做一次数值 FK）的真实耗时**坐在键盘前是算不出来的**。
而本项目有一条铁律（AGENTS.md §5.5 铁律②）：**界面上写的样本数必须等于真跑出来的那个**。
所以定 n 之前先量：

    cd backend && .venv/Scripts/python.exe tools/time_whatif.py

**纯离线**：不连机器人、不写任何寄存器、不碰气路、不改速度口径；
点位姿态用下面这份**合成表**（不读生产库），这样量到的就是"计算本身的耗时"，
不会因为"这台机器库里有多少点"而漂移。

输出怎么读
--------
· `sample_ms` = 单个蒙特卡洛样本的均值耗时（= 全部段各扫掠一次的开销）；
· 脚本按 `RUN_BUDGET_MS` 预算反推"整轮不超过 ~1.5s"的最大 n，并给出建议值；
· 建议值抄进 `app/services/whatif.py` 的 `SAMPLE_N_DEFAULT`，并把实测数字写进那里的注释
  （本项目的习惯：**现场数字留在代码注释里**，别让下一个人再猜）。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("EFORT_SIMULATE", "always")      # 本脚本本来就不碰硬件，这里只是兜底

from app.services import whatif as W                   # noqa: E402

#: 合成点位（够跑出一条真实形状的段序列：原位 → 正上位 → 检测 → 目标正上位 → 目标 → 起点）
POSES = {
    "A1": [0.0, -60.0, 60.0, 0.0, 30.0, -14.1],
    "A1上": [0.0, -60.0, 60.0, 0.0, 30.0, -14.1],
    "C检测": [60.0, -50.0, 55.0, 0.0, 25.0, 0.0],
    "B1上": [120.0, -60.0, 60.0, 0.0, 30.0, 20.0],
    "B1": [120.0, -60.0, 60.0, 0.0, 30.0, 20.0],
    "开始/结束点": [90.0, -40.0, 40.0, 0.0, 20.0, 0.0],
}
SCENARIO = {"tray": {"dx": 10.0, "dy": 0.0, "dz": 0.0}, "face": "keep",
            "order": "as_listed", "speed_gear_pct": 10}


def _one(n: int, seed: int) -> dict:
    return W.run(SCENARIO, n=n, seed=seed, poses=POSES)


def main() -> int:
    try:                                    # Windows 中文系统默认 GBK 控制台：⇒ / ≈ 会炸
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("== E3 what-if 离线计时（不连机器人 / 不写任何东西）==")
    warm = _one(2, seed=1)
    print("预热：段数=%s 样本=%s 整轮=%sms（sample_ms=%s）"
          % (len(warm.get("segments") or []), warm.get("n"), warm.get("run_ms"),
             warm.get("sample_ms")))

    probe_n = 40
    res = _one(probe_n, seed=7)
    segs = len(res.get("segments") or [])
    sample_ms = float(res.get("sample_ms") or 0.0)
    if sample_ms <= 0:
        print("!! 没跑出样本（status=%s，failures=%s）" % (res.get("status"), res.get("failures")))
        return 1
    print("实测：段数=%d  每段扫掠=%d 拍  单样本=%.3f ms  整轮(%d 样本)=%.1f ms"
          % (segs, W.SWEEP_SAMPLES_PER_SEGMENT, sample_ms, probe_n, res.get("run_ms") or 0.0))

    # 手工再量一次纯蒙特卡洛循环（去掉 run() 里的簿记开销，看真实吞吐）
    t0 = time.perf_counter()
    for i in range(probe_n):
        _one(1, seed=100 + i)
    loop_ms = (time.perf_counter() - t0) * 1000.0 / probe_n
    print("含每次 run() 的固定开销（点位/名义扫掠/统计）：平均 %.1f ms/次 run(n=1)" % loop_ms)

    budget = W.RUN_BUDGET_MS
    per_run_fixed_ms = max(0.0, (res.get("run_ms") or 0.0) - sample_ms * probe_n)
    fit = int((budget - per_run_fixed_ms) / max(sample_ms, 1e-6))
    print("单轮固定开销 约 %.1f ms，预算 %.0f ms 内最多跑得下 n 约 %d 个样本"
          % (per_run_fixed_ms, budget, fit))
    for n_try in (50, 100, 140, 200, 300):
        r = _one(n_try, seed=11)
        got = int(r.get("n") or 0)
        flag = "OK" if (got == n_try and float(r.get("run_ms") or 0) <= budget * 1.35) else "偏慢/被截断"
        print("  n=%3d -> 实际跑完 %3d，整轮 %7.1f ms   %s"
              % (n_try, got, r.get("run_ms") or 0.0, flag))

    print("\n建议 SAMPLE_N_DEFAULT = %d（单样本 %.3f ms；整轮应 <= 预算 %.0f ms）"
          % (W.SAMPLE_N_DEFAULT, sample_ms, budget))
    print("把上面这行数字抄进 app/services/whatif.py 顶部注释里，别只改常量不改说明。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
