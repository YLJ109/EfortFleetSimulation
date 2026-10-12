# -*- coding: utf-8 -*-
"""★ 原位禁转 + 原位安全路径 回归（2026-10-12）。

用户口径（逐字，三条）：
  1. 「**原位也不能进行 J6 旋转**」「**原位禁止旋转拒绝指令！**」
     —— 机器人停在**原料位**（A1…A9 / B1…B9）时，`J6正面`/`J6反面`（虚拟切面命令）
        与任何"带翻转的下发"一律**拒绝**；要切先把车开到**正上位**再切。
  2. 「**不能原位到原位进行移动**……你直接原位，正上位，目标的正上位，原位就行了」
     —— 原位 → **不同**原位：自动拆三段 `X → X上 → Y上 → Y`（不经低位直冲）；
        原位 → **同一**原位：**拒绝**（无意义运动）。
  3. 「**任何程序也是**」—— `_move` 里再加一道**兜底**：流程若直接下发"原料位→另一原料位"→ 拒绝。

判据（唯一）：`services/sorting.py::current_raw_slot` —— **实时 6 关节 vs 18 个原料位示教姿态**
（容差 3°，J6 按环形差）。**无状态**：点动/手动下发后自动正确，不靠"上一次到了哪"。

全部纯逻辑/接口级：conftest 已强制 `EFORT_SIMULATE=always` + `EFORT_REAL_MOTION=0`，
用例内 monkeypatch 实时姿态 + 记录 `motion.command`，**绝不连真机**。
"""
from __future__ import annotations

import types


from app.services import sorting as S

DIM_J6_FACE_POINT = "J6反面"


def _h(client):
    r = client.post("/api/auth/login", json={"password": "test1234"})
    assert r.status_code == 200, r.text
    return {"X-Control-Token": r.json()["token"]}


def _teach(client, h, name, joints):
    r = client.post("/api/sorting/teach", json={"name": name, "joints": joints}, headers=h)
    assert r.status_code == 200, r.text


# =====================================================================
# 一、纯函数：`current_raw_slot`（"现在停在哪"的唯一判据）
# =====================================================================
def test_current_raw_slot_matches_within_tolerance():
    poses = {"A1": [0, 0, 0, 0, 0, 0], "A2": [10, 20, 30, 40, 50, 60]}
    assert S.current_raw_slot([0.5, -0.5, 1.0, 0.0, 0.0, 2.0], poses) == "A1"
    assert S.current_raw_slot([10, 20, 30, 40, 50, 60], poses) == "A2"


def test_current_raw_slot_miss_returns_none():
    poses = {"A1": [0, 0, 0, 0, 0, 0]}
    # 超出容差（3°）→ 判不出
    assert S.current_raw_slot([5.0, 0, 0, 0, 0, 0], poses) is None
    # 拿不到实时姿态 / 形状不对 → None（调用方按 fail-safe 处理）
    assert S.current_raw_slot(None, poses) is None
    assert S.current_raw_slot([0, 0, 0, 0, 0], poses) is None
    assert S.current_raw_slot([0] * 6, None) is None


def test_current_raw_slot_only_considers_raw_slots():
    """正上位（A1上）**不是**原料位 → 即使姿态完全一致也不算"停在原位"。"""
    poses = {"A1上": [0, 0, 0, 0, 0, 0], "J6反面": [0, 0, 0, 0, 0, 0]}
    assert S.current_raw_slot([0.0] * 6, poses) is None


def test_current_raw_slot_j6_is_ring_equivalent():
    """★ J6 按**环形差**：175° 与 −185° 是同一姿态，不得因 ±360 表示差异判成"不在原位"。"""
    poses = {"A3": [0, 0, 0, 0, 0, -185.0]}
    assert S.current_raw_slot([0, 0, 0, 0, 0, 175.0], poses) == "A3"


# =====================================================================
# 二、`raw_slot_poses`：只取原料位，坏行跳过
# =====================================================================
def test_raw_slot_poses_only_raw_slots(client):
    h = _h(client)
    _teach(client, h, "A1", [1, 2, 3, 4, 5, 6])
    _teach(client, h, "C检测", [9, 9, 9, 9, 9, 9])     # 不是原料位 → 不应出现
    poses = S.raw_slot_poses()
    assert poses.get("A1") == [1, 2, 3, 4, 5, 6]
    assert "C检测" not in poses


# =====================================================================
# 三、`/sorting/goto`：原位禁转（切面命令）
# =====================================================================
def test_goto_face_point_at_raw_slot_auto_lifts_and_returns(client, monkeypatch):
    """★ 口径3：停在原料位时按「J6反面」→ **自动补段**：抬起(A1上) → 切面 → 回原位(A1)。"""
    import app.api.sorting as API
    import app.services.motion as M
    h = _h(client)
    _teach(client, h, "A1", [1, 2, 3, 4, 5, 6])
    _teach(client, h, "A1上", [1, 2, -30, 4, 5, 90])
    monkeypatch.setattr(API, "_live_joints", lambda: [1.0, 2, 3, 4, 5, 6])   # ≈ A1
    cmds = []
    monkeypatch.setattr(M.motion, "command",
                        lambda *a, **k: (cmds.append(list(a[0])), {"ok": True})[1])
    r = client.post("/api/sorting/goto", json={"name": DIM_J6_FACE_POINT}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json().get("path") == ["A1上", DIM_J6_FACE_POINT, "A1"], r.text
    assert len(cmds) == 3, "原位切面必须自动补段（抬起→切→回原位）：%r" % cmds
    assert cmds[0][5] == 6.0, "抬起段 J6 必须冻结（原位到正上位不转）：%r" % cmds[0]
    assert cmds[2][5] == cmds[1][5], "回原位段必须沿用刚切好的面：%r" % cmds


def test_goto_face_point_allowed_when_not_at_raw_slot(client, monkeypatch):
    """★ 在**正上位**（不是原料位）切面 → 放行（这才是正确用法）。"""
    import app.api.sorting as API
    h = _h(client)
    _teach(client, h, "A1", [0, 0, 0, 0, 0, 0])
    # 实时姿态离 A1 很远（≠ 任何原料位）→ 允许切面
    monkeypatch.setattr(API, "_live_joints", lambda: [45.0, 30.0, -20.0, 5.0, 60.0, 15.0])
    r = client.post("/api/sorting/goto", json={"name": DIM_J6_FACE_POINT}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json().get("j6_only") is True


# =====================================================================
# 四、`/sorting/goto`：原位到原位
# =====================================================================
def test_goto_same_raw_slot_rejected(client, monkeypatch):
    """★ 口径 2：原位 → **同一**原位 → 拒绝（无意义运动）。"""
    import app.api.sorting as API
    h = _h(client)
    _teach(client, h, "A1", [0, 0, 0, 0, 0, 0])
    monkeypatch.setattr(API, "_live_joints", lambda: [0.0] * 6)
    r = client.post("/api/sorting/goto", json={"name": "A1"}, headers=h)
    assert r.status_code == 409, r.text
    assert "无需移动" in r.json()["message"], r.text


def test_goto_other_raw_slot_takes_three_segment_safe_path(client, monkeypatch):
    """★ 口径 2：原位 A1 → 另一原位 A2 → **自动拆三段** A1上 → A2上 → A2（3 次下发）。"""
    import app.api.sorting as API
    import app.services.motion as M
    h = _h(client)
    _teach(client, h, "A1", [0, 0, 0, 0, 0, 0])
    _teach(client, h, "A2", [10, 20, 30, 40, 50, 60])
    _teach(client, h, "A1上", [0, 0, -30, 0, 0, 90])
    _teach(client, h, "A2上", [10, 20, 0, 40, 50, 120])
    monkeypatch.setattr(API, "_live_joints", lambda: [0.0] * 6)
    cmds = []
    monkeypatch.setattr(M.motion, "command",
                        lambda *a, **k: (cmds.append(list(a[0])), {"ok": True})[1])
    r = client.post("/api/sorting/goto", json={"name": "A2", "speed_pct": 30}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json().get("path") == ["A1上", "A2上", "A2"], r.text
    assert len(cmds) == 3, "原位安全路径必须是三段下发：%r" % cmds


# =====================================================================
# 五、`_move` 兜底：流程直接"原料位 → 另一原料位" → 拒绝（口径 3「任何程序也是」）
# =====================================================================
class _Ctx:
    def wait_pause(self):
        return False

    def cancelled(self):
        return False


def _pmap(*names):
    return {n: types.SimpleNamespace(joints="[0,0,0,0,0,0]") for n in names}


def test_move_auto_inserts_safe_path_via_pmap(monkeypatch):
    """★ 长流程兜底：`_move` 在原料位出发时会**自动补段**（抬起 → 目标正上位 → 目标）。"""
    import app.api.sorting as API
    import app.services.motion as M
    pmap = _pmap("A1", "A2", "A1上", "A2上")
    monkeypatch.setattr(API, "_live_joints_or_none", lambda: [0.0] * 6)
    monkeypatch.setattr(API, "guard_check", lambda: (True, "", None))
    cmds = []
    monkeypatch.setattr(M.motion, "command",
                        lambda *a, **k: (cmds.append(list(a[0])), {"ok": True})[1])
    API._move(_Ctx(), pmap, "A2", 10, False, "跨原位")
    assert len(cmds) == 3, "应自动补段（A1上 → A2上 → A2）：%r" % cmds


def test_move_fallback_not_applied_in_dry_run(monkeypatch):
    """空跑（dry）不下发任何指令 → 这条护栏不生效（否则会误伤预演/离线流程）。"""
    import app.api.sorting as API
    pmap = _pmap("A1", "A2")
    monkeypatch.setattr(API, "_live_joints_or_none", lambda: [0.0] * 6)
    API._move(_Ctx(), pmap, "A2", 10, True, "跨原位·空跑")   # 不应抛


def test_move_fallback_not_applied_when_target_is_not_raw_slot(monkeypatch):
    """目标不是原料位（如正上位）→ 不拦（正常流程大量走这条）。"""
    import app.api.sorting as API
    pmap = _pmap("A1", "A2")
    monkeypatch.setattr(API, "_live_joints_or_none", lambda: [0.0] * 6)
    # 目标 A2上 不在 pmap 里会被 `_joints_of` 判"点位缺失" → 这里补进去
    pmap["A2上"] = types.SimpleNamespace(joints="[0,0,0,0,0,90]")
    API._move(_Ctx(), pmap, "A2上", 10, True, "去正上位·空跑")


# =====================================================================
# 六、★「任何程序也是」：`/control/run-file` 必须有**同一份**护栏
#    （它**不走** `api/sorting._move` —— 自己那条下发链，见 `_run_file_job`）
# =====================================================================
def test_run_file_job_carries_origin_guards():
    """静态核对：**所有"不走 `_move` 的下发链"**都接了「原位安全路径」自动补段。

    · `api/control.py::api_move` / `_run_file_job` —— 点位执行 / 程序·序列·文件执行；
    · `services/vision_rules.py::execute_rule` —— 后台无人值守规则自动执行。
    """
    import inspect

    import app.api.control as C
    import app.services.vision_rules as VR
    for fn in (C.api_move, C._run_file_job, VR.execute_rule):
        src = inspect.getsource(fn)
        assert "raw_slot_step_targets(" in src, "%s 未接「原位安全路径」自动补段" % fn.__qualname__


def _wait_result(client, h, run_id, timeout=10.0):
    """`/control/run-file` 已异步化：结果从 `/control/run-state` 取。"""
    import time as _t
    t0 = _t.time()
    while _t.time() - t0 < timeout:
        r = client.get("/api/control/run-state", params={"run_id": run_id}, headers=h)
        st = (r.json() or {}).get("state") or {}
        if st.get("result"):
            return st["result"]
        _t.sleep(0.05)
    raise AssertionError("等待执行结果超时：%s" % run_id)


def test_run_file_auto_inserts_safe_path(client, monkeypatch):
    """★ 程序步骤也**自动补段**：步骤点名 A2、当前停在 A1 → 实际下发 A1上 → A2上 → A2。"""
    import app.api.control as C
    import app.services.motion as M
    from app.db.crud import list_points
    from app.db.database import SessionLocal
    h = _h(client)
    _teach(client, h, "A1", [0, 0, 0, 0, 0, 0])
    _teach(client, h, "A2", [10, 20, 30, 40, 50, 60])
    _teach(client, h, "A1上", [0, 0, -30, 0, 0, 90])
    _teach(client, h, "A2上", [10, 20, 0, 40, 50, 120])
    monkeypatch.setattr(C, "_live_joints_or_none", lambda: [0.0] * 6)
    cmds = []
    monkeypatch.setattr(M.motion, "command",
                        lambda *a, **k: (cmds.append(list(a[0])), {"ok": True})[1])
    db = SessionLocal()
    try:
        pid = next(p.id for p in list_points(db) if p.name == "A2")
    finally:
        db.close()
    r = client.post("/api/control/run-file",
                    json={"items": [{"type": "point", "point_id": pid}],
                          "name": "ed", "dry_run": False, "speed_pct": 5},
                    headers=h)
    assert r.status_code == 200, r.text
    d = _wait_result(client, h, r.json()["run_id"])
    assert d["ok"] is True, d
    assert len(cmds) == 3, "程序步骤应自动补段：%r" % cmds


def test_run_file_face_cut_at_raw_slot_lifts_and_returns(client, monkeypatch):
    """★ 程序里的切面步骤、机器人停在原料位 → **自动补段**：抬起 → 切 → 回原位（3 次下发）。

    ★ 为什么**直接写库**造 `J6反面` 行：它现在是**虚拟切面命令**（示教接口直接 400「无需示教」），
      但库里**历史遗留行**仍可能被序列步骤按 `point_id` 引用 —— 这条正是给那种情形兜底的。
    """
    import app.api.control as C
    import app.services.motion as M
    from app.db.crud import create_point, list_points
    from app.db.database import SessionLocal
    h = _h(client)
    # ★ 用**特征明显**的实时姿态（[1,2,3,4,5,6]）：DB 里其它用例可能也教过全 0 的点，
    #   若用 [0]*6 会与它们撞车（"在哪一格"由 J1~J5 判，撞车就判不准）。
    _teach(client, h, "A1", [1, 2, 3, 4, 5, 6])              # 让"当前 ≈ A1"成立
    _teach(client, h, "A1上", [1, 2, -30, 4, 5, 90])
    db = SessionLocal()
    try:
        row = next((p for p in list_points(db) if p.name == DIM_J6_FACE_POINT), None)
        if row is None:
            row = create_point(db, DIM_J6_FACE_POINT, joints_json="[0,0,0,0,0,180]")
        pid = row.id
    finally:
        db.close()
    monkeypatch.setattr(C, "_live_joints_or_none", lambda: [1.0, 2, 3, 4, 5, 6])
    cmds = []
    monkeypatch.setattr(M.motion, "command",
                        lambda *a, **k: (cmds.append(list(a[0])), {"ok": True})[1])
    r = client.post("/api/control/run-file",
                    json={"items": [{"type": "point", "point_id": pid}],
                          "name": "ed", "dry_run": False, "speed_pct": 5},
                    headers=h)
    assert r.status_code == 200, r.text
    d = _wait_result(client, h, r.json()["run_id"])
    assert d["ok"] is True, d
    assert len(cmds) == 3, "原位切面必须自动补段（抬起→切→回原位）：%r" % cmds
    assert cmds[0][5] == 6.0, "抬起段 J6 冻结（原位到正上位不转）：%r" % cmds[0]
    assert cmds[2][5] == cmds[1][5], "回原位段沿用刚切好的面：%r" % cmds


# =====================================================================
# 七、★★ 2026-10-12「原位安全路径」（用户口径：「**禁止原位到原位执行**」「**禁止原位去起点和
#   摄像头点**」「**禁止摄像头点位或者起点直接去原点**」「**如果原位旋转，先回到正上位，
#   旋转，然后来到原位**」）—— **一切入口都自动补段**（不再拒绝）。
# =====================================================================
def _dep_poses():
    """★ 各点的 **J1~J5 必须互不相同**（正上位比原位"高"，J3 必然不同）——
    因为"在哪一格"由 **J1~J5** 判定（忽略 J6），若 J1~J5 相同就分不出原位/正上位。"""
    return {
        "A1": [0, 0, 0, 0, 0, 0],
        "A1上": [0, 0, -30, 0, 0, 90],                 # ★ J1~J5 与 A1 不同（高)
        "A2": [10, 20, 30, 40, 50, 60],
        "A2上": [10, 20, 0, 40, 50, 120],              # ★ J1~J5 与 A2 不同（高）
        S.START_POINT: [30, 20, 10, 0, 0, 45],
    }


def test_safe_path_pure_function():
    """纯函数：**原位安全路径**（垂直只在"某格原位 ↔ 该格正上位"，横向在高位）。"""
    poses = _dep_poses()
    live = [0.0] * 6                                   # ≈ A1

    def P(tgt, lv=None):
        return S.raw_slot_safe_path(lv if lv else live, poses, tgt)[0]

    assert P("A1上") == ["A1上"]                        # 抬到自己的正上位 = 单段
    assert P("A1") == []                                # 已在目标点（交互入口报「无需移动」）
    assert P("A2") == ["A1上", "A2上", "A2"]            # 原位 → 另一原位
    assert P(S.START_POINT) == ["A1上", S.START_POINT]  # 原位 → 起点（先抬起）
    # ★ 规则3：从**正上位**（A2上）去 A1 原位 → 必须经 A1上
    assert P("A1", poses["A2上"]) == ["A1上", "A1"]
    # ★ 规则3：从**起点**去 A1 原位 → 同样经 A1上
    assert P("A1", poses[S.START_POINT]) == ["A1上", "A1"]
    # ★★ 判不出在哪一格 → **不拒绝、也不补段**（"判不出"⇒ 不在任何示教点附近 ⇒ 也不在原位附近）
    assert S.raw_slot_safe_path([45, 30, -20, 5, 60, 15], poses, "A1") == (None, None)
    # 正上位 → 另一正上位 = 纯横向，不补
    assert P("A2上", poses["A1上"]) == ["A2上"]


def test_safe_path_never_refuses_when_position_unknown():
    """★★ **判不出在哪一格 → 不拒绝、也不补段**（现场 Bug 固化，2026-10-12）。

    用户口径：「**不要禁止，你直接自动补段**」
    （现场原话：正在移到「A1上」（正上位）… 无法确认机器人当前位置… —— 连"去正上位"都被拦了）。
    ★ 为什么"不补段"是安全的：判不出 ⇒ 实时 **J1~J5** 与**所有**示教点都差 > 容差
      ⇒ **它不在任何示教点附近** ⇒ 也就不在任何原位附近 ⇒ 不会构成"从低位横移"。
    ★ 注意：**"在哪一格"由 J1~J5 判（忽略 J6）** —— 因为"升降段 J6 冻结"会故意让到达姿态的
      J6 ≠ 该点示教 J6，若把 J6 算进去就会"抬手后认不出自己"。
    """
    poses = _dep_poses()
    _unknown = [45, 30, -20, 5, 60, 15]                # J1~J5 与所有示教点都不符
    for tgt in ("A2", S.START_POINT, "A1上"):
        assert S.raw_slot_safe_path(_unknown, poses, tgt) == (None, None), tgt
    # 拿不到实时姿态 → 同样不拒绝（交回调用方；真实下发的"未就绪闸门"会兜底）
    assert S.raw_slot_safe_path(None, poses, "A2") == (None, None)
    # 脚本/无人值守入口（`raw_slot_step_segments`）同样不拒绝
    assert S.raw_slot_step_segments(_unknown, poses, target_name="A2") == ([], [], None)


def test_position_same_slot_with_different_j6_is_still_recognized():
    """★★ **J6 冻结后仍认得出"在哪一格"**：同一格、只 J6 不同 → 仍判为同一点（现场 Bug 固化）。

    现场症状：抬到 A1上 后（J6 保持原位的值，≠ A1上 示教 J6），下一步"认不出自己"
    → 漏掉抬起段 / 报"偏差超过容差 3°"。
    """
    poses = _dep_poses()
    live = list(poses["A1上"])
    live[5] = 12.0                                     # ★ 只把 J6 改成另一个值（模拟"冻结"结果）
    assert S.point_at_pose(live, poses) == "A1上"
    assert S.current_raw_slot(list(poses["A1"]) [:5] + [77.0], poses) == "A1"


def test_goto_from_raw_slot_to_start_point_takes_two_segments(client, monkeypatch):
    """★ 交互入口：原位 A1 → 起点 = **两段**（先抬到 A1上，再过去），绝不平移。"""
    import app.api.sorting as API
    import app.services.motion as M
    h = _h(client)
    _teach(client, h, "A1", [0, 0, 0, 0, 0, 0])
    _teach(client, h, "A1上", [0, 0, -30, 0, 0, 90])
    _teach(client, h, S.START_POINT, [30, 20, 10, 0, 0, 45])
    monkeypatch.setattr(API, "_live_joints", lambda: [0.0] * 6)
    cmds = []
    monkeypatch.setattr(M.motion, "command",
                        lambda *a, **k: (cmds.append(list(a[0])), {"ok": True})[1])
    r = client.post("/api/sorting/goto", json={"name": S.START_POINT, "speed_pct": 30}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json().get("path") == ["A1上", S.START_POINT], r.text
    assert len(cmds) == 2, "原位→起点必须是两段下发：%r" % cmds


def test_goto_from_raw_slot_to_own_above_stays_single_segment(client, monkeypatch):
    """★ 目标就是**自己的正上位**（= 抬起本身）→ 不拆段，走原单段路径。"""
    import app.api.sorting as API
    import app.services.motion as M
    h = _h(client)
    _teach(client, h, "A1", [0, 0, 0, 0, 0, 0])
    _teach(client, h, "A1上", [0, 0, -30, 0, 0, 90])
    monkeypatch.setattr(API, "_live_joints", lambda: [0.0] * 6)
    cmds = []
    monkeypatch.setattr(M.motion, "command",
                        lambda *a, **k: (cmds.append(list(a[0])), {"ok": True})[1])
    r = client.post("/api/sorting/goto", json={"name": "A1上", "speed_pct": 30}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json().get("path") is None, r.text
    assert len(cmds) == 1


def test_control_move_explicit_joints_auto_inserts(client, monkeypatch):
    """★ c 项：**显式 6 轴 joints**（不经 point_id）也**自动补段** → 先抬到 A1上，再下发原目标。"""
    import app.api.control as C
    import app.services.motion as M
    h = _h(client)
    _teach(client, h, "A1", [0, 0, 0, 0, 0, 0])
    _teach(client, h, "A1上", [0, 0, -30, 0, 0, 90])
    # 伪装成"真机链路"（档位检查单独放行；模拟链路仍不写控制器）
    monkeypatch.setattr(M, "real_write_enabled", lambda: True)
    monkeypatch.setattr(C, "real_write_enabled", lambda: True)
    monkeypatch.setattr(C.runmode, "check_jog", lambda: (True, "", None))
    monkeypatch.setattr(C, "_live_joints_or_none", lambda: [0.0] * 6)   # 当前 ≈ A1
    cmds = []
    monkeypatch.setattr(M.motion, "command",
                        lambda *a, **k: (cmds.append(list(a[0])), {"ok": True})[1])
    # ★ 用一个**匹配不上库里任何点位**的目标，避免被"目标≈某已示教点"的判定串味
    _tgt = [11, 22, 33, 44, 55, 66]
    r = client.post("/api/control/move", json={"joints": _tgt}, headers=h)
    assert r.status_code == 200, r.text
    assert len(cmds) == 2, "应为：A1上（抬起，J6 冻结）→ 原目标：%r" % cmds
    assert cmds[0][5] == 0.0, "抬起段 J6 冻结：%r" % cmds[0]
    assert cmds[1] == _tgt, "原目标必须原样透传：%r" % cmds[1]

