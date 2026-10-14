# -*- coding: utf-8 -*-
"""
A5 事件取证包：每次 danger/hit **上升沿**自动落一盘"可复盘的证据"。

兑现项目主张里的**"每一步都能自证"**：事发那一刻到底多近、谁下的令、速度多少、
程序号是什么、控制器读回来的是什么档位 —— 事后靠回忆和日志拼图是拼不出来的。

三件事
  ① **环形缓冲**（`note()`）：采集循环每 0.2s 推一条实时样本（关节 / TCP / 后端围栏结论 /
     是否陈旧 / 是否模拟）。默认留 10s，够覆盖"撞上前的一段"。
  ② **上升沿落盘**（`request_capture()`）：由 `safety_guard` 在"变严重进 danger/hit"时调用。
     ★★ **绝不阻塞采集线程**：这里只把一个请求塞进队列，真正写盘由**单条后台线程**做
        （写盘要读库、拉相机图，最长可能几百毫秒 —— 放在采集循环里就会丢帧，
         而丢帧正是 §5.5 铁律里"画面停在最后一帧"的那类事故）。
  ③ **取证目录**：`{EFORT_LOGS_DIR}/evidence/<UTC时间>-<seq>/`
     `manifest.json`（结论与上下文）· `samples.jsonl`（缓冲里每条实时样本）·
     `events.jsonl`（相关事件）· `frame.jpg`（相机在跑才有，**没有就 manifest 里如实写 null**）。

★ seq **只增不减、绝不复用**（铁律①）：计数落在 `evidence/.seq`，前端列表的水位用 `seq`
  而不是 `list.length`（列表被 `keep_days` 裁过之后长度会变小，水位一失效就整块不刷新）。
★ **不做什么**：不上传任何外部服务；不做永久保留（`evidence.keep_days` 默认 7 天）；
  不承诺存下画面；3D 画布截图没有前端上传通道 → **不假装采到**（记进 `honest_gaps`）。
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import get_config, logs_dir
from app.core.logger import get_logger

log = get_logger("safety_evidence")

SCHEMA = 1
PACK_DIR_TAG = "evidence"
_SAMPLES_HARD_CAP = 4000          # 环形缓冲的绝对上限（窗口配得再大也不撑爆内存）


# ---------------------------------------------------------------- 配置
def _cfg(key: str, default: Any) -> Any:
    try:
        v = get_config().get("evidence", key, default=None)
        return default if v is None else v
    except Exception:
        return default


def buffer_seconds() -> float:
    try:
        return max(1.0, min(120.0, float(_cfg("buffer_seconds", 10.0))))
    except Exception:
        return 10.0


def keep_days() -> float:
    try:
        return max(1.0, min(90.0, float(_cfg("keep_days", 7))))
    except Exception:
        return 7.0


def max_packs() -> int:
    try:
        return max(10, min(5000, int(_cfg("max_packs", 200))))
    except Exception:
        return 200


def enabled() -> bool:
    try:
        return bool(_cfg("enabled", True))
    except Exception:
        return True


def base_dir() -> str:
    """取证根目录 —— ★ 必须落在 `logs_dir()` 下（AGENTS.md §5.5：测试隔离三样之一）。"""
    d = os.path.join(logs_dir(), PACK_DIR_TAG)
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------- 环形缓冲
_lock = threading.Lock()
_samples: deque = deque()
_last_trim_at = 0.0


def note(sample: Dict[str, Any]) -> None:
    """采集循环每拍推一条实时样本（成本 = 一次 append；★ 绝不抛异常回调用方）。"""
    try:
        if not enabled():
            return
        s = dict(sample or {})
        s.setdefault("at", time.time())
        with _lock:
            _samples.append(s)
            if len(_samples) > _SAMPLES_HARD_CAP:
                _samples.popleft()
        _trim_if_due()
    except Exception as e:                       # 取证坏了不能带崩采集
        log.debug("取证样本记录失败（忽略）: %s", e)


def _trim_if_due() -> None:
    """按**时间窗口**裁剪（不是按条数 —— 采集节拍会变，条数会骗人）。"""
    global _last_trim_at
    now = time.time()
    if now - _last_trim_at < 1.0:
        return
    _last_trim_at = now
    floor = now - buffer_seconds()
    with _lock:
        while len(_samples) > 2 and _samples[0].get("at", 0) < floor:
            _samples.popleft()


def window_samples() -> List[Dict[str, Any]]:
    floor = time.time() - buffer_seconds()
    with _lock:
        return [dict(s) for s in _samples if float(s.get("at", 0)) >= floor]


def reset_buffer() -> None:
    """清空缓冲与队列（**只给测试用**，避免用例之间串味）。"""
    with _lock:
        _samples.clear()


# ---------------------------------------------------------------- seq（只增不减）
_seq_lock = threading.Lock()


def _seq_file() -> str:
    return os.path.join(base_dir(), ".seq")


def _next_seq() -> int:
    """持久单调计数。落盘失败也要继续（退回内存计数），**绝不因为取证书不写而拒绝**。"""
    path = _seq_file()
    cur = 0
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                cur = int((f.read() or "0").strip() or 0)
    except Exception:
        cur = 0
    nxt = cur + 1
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(nxt))
    except Exception as e:
        log.warning("取证序号写入失败（本次仍用 %d）: %s", nxt, e)
    return nxt


# ---------------------------------------------------------------- 脱敏
_SECRET_KEY_RE = re.compile(r"(token|password|passwd|secret|cookie|authoriz|api[_-]?key|credential)",
                            re.I)
# "token":"abc" / password=abc / api_key: abc —— 键名连同值一起打掉
_SECRET_PAIR_RE = re.compile(
    r"([\"']?(?:token|password|passwd|secret|api[_-]?key|credential)[\"']?\s*[:=]\s*)[^\s,;\"'}]+",
    re.I)


def _secret_values() -> List[str]:
    """已知口令/令牌明文（用于把事件 detail 里可能带出的口令打码）。

    ★ **不缓存**：改口令后缓存里还是旧口令，新口令就会原样进取证包（缓存省一点开销、
      换来一个"泄漏面"，不值）。
    """
    vals: List[str] = []
    for env in ("EFORT_ADMIN_PASSWORD", "EFORT_OPERATOR_PASSWORD"):
        v = os.environ.get(env) or ""
        if len(v) >= 3:
            vals.append(v)
    for k in ("admin_password", "operator_password"):
        try:
            v = get_config().get("auth", k, default=None)
        except Exception:
            v = None
        v = str(v or "")
        if len(v) >= 3 and v not in vals:
            vals.append(v)
    return vals


def _redact_text(t: str) -> str:
    out = str(t)
    # 形如 "token":"abc" / password=abc / api_key: abc 的**键值对**整对打码
    out = _SECRET_PAIR_RE.sub(lambda m: f"{m.group(1)}***", out)
    for v in _secret_values():
        if v in out:
            out = out.replace(v, "***")
    return out


def _scrub_detail(raw: Any) -> str:
    """事件 detail 多半是 JSON 字符串 —— 能解析就**按结构**脱敏，解不了就按文本脱敏。"""
    s = str(raw or "")
    if not s:
        return ""
    try:
        obj = json.loads(s)
    except Exception:
        return _redact_text(s)
    if isinstance(obj, (dict, list)):
        try:
            return json.dumps(_scrub(obj), ensure_ascii=False)
        except Exception:
            return _redact_text(s)
    return _redact_text(str(obj))


def _scrub(obj: Any) -> Any:
    """按键名剔除凭据类字段 + 对字符串做口令打码（写盘前的最后一道闸）。"""
    if isinstance(obj, dict):
        cleaned: Dict[str, Any] = {}
        for k, v in obj.items():
            if _SECRET_KEY_RE.search(str(k)):
                continue
            cleaned[str(k)] = _scrub(v)
        return cleaned
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    if isinstance(obj, str):
        return _redact_text(obj)
    if isinstance(obj, float):
        # NaN/Inf 会写出非法 JSON（前端 JSON.parse 直接抛）—— 统一成 null
        return obj if obj == obj and obj not in (float("inf"), float("-inf")) else None
    return obj


# ---------------------------------------------------------------- 上下文采集
def _guard_snapshot() -> Dict[str, Any]:
    try:
        from app.services.safety_guard import snapshot
        return snapshot() or {}
    except Exception as e:
        return {"unavailable": f"围栏快照不可用：{str(e)[:80]}"}


def _collector_context() -> Tuple[Dict[str, Any], List[str]]:
    gaps: List[str] = []
    out: Dict[str, Any] = {}
    try:
        from app.services.collector import collector
        latest = collector.get_latest() or {}
        out["pose_frame"] = {
            "joints": [latest.get(f"j{i}") for i in range(1, 7)] if latest else None,
            "tcp": latest.get("tcp"),
            "simulated": latest.get("simulated"),
            "stale": latest.get("stale"),
            "frame_at": latest.get("at"),
            "cmd": latest.get("cmd"),
            "cmd_tcp": latest.get("cmd_tcp"),
        }
        if not latest:
            gaps.append("采集器还没有任何姿态帧（取证里的关节/TCP 为空）")
        elif latest.get("stale"):
            gaps.append("事发那一帧是**陈旧帧**（读控制器失败后冻结的上一帧），"
                        "关节/TCP 不代表控制器当前真实位置")
        rc = getattr(collector, "latest_rc_status", None) or {}
        out["rc_status"] = _rc_summary(rc)
        if not rc.get("ok"):
            gaps.append(f"控制器寄存器快照不可用：{rc.get('error') or '没读到（模拟/离线）'}")
        out["collector"] = {"simulated": bool(getattr(collector, "simulated", False)),
                            "connected": bool(getattr(collector, "connected", False))}
    except Exception as e:
        gaps.append(f"采集上下文不可用：{str(e)[:80]}")
    return out, gaps


def _rc_summary(rc: Dict[str, Any]) -> Dict[str, Any]:
    bits = (rc or {}).get("bits") or {}
    return {
        "ok": bool((rc or {}).get("ok")),
        "error": (rc or {}).get("error") or "",
        "at": (rc or {}).get("t") or "",
        "mode": (rc or {}).get("mode"),
        "status_word": (rc or {}).get("status_word"),
        "servo_on": bits.get("servo"),
        "alarm_bit": bits.get("alarm"),
        "estop_bit": bits.get("estop"),
        "run_bit": bits.get("run"),
        "prog_loaded_bit": bits.get("prog_loaded"),
        "prog": (rc or {}).get("prog"),
        "service_program": (rc or {}).get("service_program"),
        # 40003 = 控制器**读回来**的运行速度；40103 是我们写进去的设定值
        "controller_speed_pct_40003": (rc or {}).get("speed_pct"),
        "alarm1": (rc or {}).get("alarm1"),
        "alarm2": (rc or {}).get("alarm2"),
        "ready": (rc or {}).get("ready"),
        "real_enabled": (rc or {}).get("real_enabled"),
    }


def _speed_context() -> Tuple[Dict[str, Any], List[str]]:
    gaps: List[str] = []
    out: Dict[str, Any] = {"ui_slider_pct": None, "note": ""}
    try:
        from app.services import ui_speed
        p = ui_speed.peek() or {}
        out["ui_slider_pct"] = p.get("speed_pct")
        out["note"] = str(p.get("note") or "")[:80]
        if out["ui_slider_pct"] is None:
            gaps.append("界面滑块速度从未被推送过（ui_speed 无登记值）")
    except Exception as e:
        gaps.append(f"界面速度读数不可用：{str(e)[:80]}")
    try:
        from app.services.motion import real_write_enabled
        out["real_write_enabled"] = bool(real_write_enabled())
    except Exception as e:
        gaps.append(f"真实下发闸门状态不可用：{str(e)[:80]}")
    return out, gaps


def _motion_context() -> Dict[str, Any]:
    try:
        from app.services.motion import motion
        st = motion.state() or {}
        return {"mode": st.get("mode"), "moving": st.get("moving"),
                "stopped": st.get("stopped"), "last_target": st.get("last_target"),
                "last_executed_at": st.get("last_executed_at"),
                "last_error": st.get("last_error"),
                "joint_lock": st.get("joint_lock")}
    except Exception as e:
        return {"unavailable": str(e)[:80]}


def _flow_context() -> Tuple[Dict[str, Any], List[str]]:
    """在跑的流程摘要（分拣 / 文件 / AI 共用同一条"在跑"世界观）。"""
    gaps: List[str] = []
    out: Dict[str, Any] = {}
    try:
        from app.api.sorting import _scene_state        # 同进程内部函数，只为拿摘要
        st = _scene_state() or {}
        out = {"running": bool(st.get("running")),
               "dry_run": st.get("dry_run"),
               "stage": st.get("stage"),
               "piece": st.get("piece_index", st.get("piece")),
               "total": st.get("target_pieces", st.get("max_pieces")),
               "color": st.get("last_color") or st.get("color"),
               "slot": st.get("slot") or st.get("target_slot"),
               "speed_pct": st.get("speed_pct"),
               "error": st.get("error") or ""}
    except Exception as e:
        gaps.append(f"执行流程状态不可用：{str(e)[:80]}")
    return out, gaps


def _recent_events(limit: int = 50) -> Tuple[List[Dict[str, Any]], List[str]]:
    """事发前后的 safety / control 事件（写进 events.jsonl 供复盘）。"""
    gaps: List[str] = []
    rows: List[Dict[str, Any]] = []
    try:
        from app.db.crud import list_system_events
        from app.db.database import SessionLocal
        s = SessionLocal()
        try:
            for cat in ("safety", "control"):
                for r in list_system_events(s, category=cat, limit=limit):
                    rows.append({"id": r.id, "timestamp": _iso(r.timestamp),
                                 "category": r.category, "level": r.level,
                                 "action": r.action, "actor": r.actor,
                                 "message": _redact_text(r.message or ""),
                                 "detail": _scrub_detail(r.detail or "")})
        finally:
            s.close()
        rows.sort(key=lambda x: (x.get("timestamp") or "", x.get("id") or 0), reverse=True)
    except Exception as e:
        gaps.append(f"事件表读取失败（events.jsonl 只有本次触发）：{str(e)[:80]}")
    return rows[:limit * 2], gaps


def _recent_commands(limit: int = 20) -> List[Dict[str, Any]]:
    """最近若干条**指令与回执**（control 类事件里带 target/speed 的那些）。"""
    rows, _ = _recent_events(limit)
    cmds: List[Dict[str, Any]] = []
    for r in rows:
        if r.get("category") != "control":
            continue
        d: Dict[str, Any] = {}
        try:
            d = json.loads(r.get("detail") or "{}")
        except Exception:
            d = {"raw": (r.get("detail") or "")[:200]}
        if not isinstance(d, dict):
            d = {"raw": str(d)[:200]}
        cmds.append({"id": r.get("id"), "timestamp": r.get("timestamp"),
                     "action": r.get("action"), "actor": r.get("actor"),
                     "message": r.get("message"),
                     "target": d.get("target") or d.get("joints") or d.get("last_target"),
                     "speed_pct": d.get("speed_pct"),
                     "point": d.get("point") or d.get("point_name") or d.get("name")})
        if len(cmds) >= limit:
            break
    return cmds


def _iso(dt: Optional[datetime]) -> str:
    try:
        if not dt:
            return ""
        return dt.isoformat() + "Z" if dt.tzinfo is None else dt.isoformat()
    except Exception:
        return ""


def _camera_frame(dir_path: str) -> Tuple[Optional[str], Dict[str, Any]]:
    """抓一张相机单帧（`/snapshot`）。★ 相机默认就是关的 —— 抓不到要在 manifest 里
    **写成 null + 原因**，不是"文件缺失当没这回事"（铁律③/⑤）。
    """
    info: Dict[str, Any] = {"captured": False, "source": "camera_service:/snapshot",
                            "error": ""}
    try:
        from app.services import camera_client
        st, data, err = camera_client.get_binary("/snapshot", timeout=1.5)
        if st == 200 and isinstance(data, (bytes, bytearray)) and len(data) > 100:
            fp = os.path.join(dir_path, "frame.jpg")
            with open(fp, "wb") as f:
                f.write(bytes(data))
            info.update({"captured": True, "bytes": len(data)})
            return "frame.jpg", info
        info["error"] = err or f"相机服务返回 {st}（无画面）"
    except Exception as e:
        info["error"] = f"抓帧异常：{str(e)[:80]}"
    return None, info


# ---------------------------------------------------------------- 落盘
_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
_worker: Optional[threading.Thread] = None
_worker_lock = threading.Lock()
_inflight: set = set()
_inflight_lock = threading.Lock()


def request_capture(trigger: Dict[str, Any], blocking: bool = False) -> Dict[str, Any]:
    """登记一次取证落盘。默认**排队由后台线程执行**（调用方 = 采集线程，绝不能等）。"""
    if not enabled():
        return {"ok": False, "queued": False, "reason": "evidence.enabled=false（取证已关闭）"}
    key = str((trigger or {}).get("dedupe_key") or "")
    if key:
        with _inflight_lock:
            if key in _inflight:
                return {"ok": True, "queued": False, "reason": "同一触发的取证包正在写，不重复出包"}
            _inflight.add(key)
    if blocking:
        try:
            return _write_pack(trigger or {})
        finally:
            if key:
                with _inflight_lock:
                    _inflight.discard(key)
    _ensure_worker()
    _queue.put(trigger or {})
    return {"ok": True, "queued": True}


def _ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker and _worker.is_alive():
            return
        _worker = threading.Thread(target=_drain, name="evidence-writer", daemon=True)
        _worker.start()


def _drain() -> None:
    while True:
        try:
            trig = _queue.get(timeout=30.0)
        except queue.Empty:
            return                      # 空闲 30s 就退出，需要时再懒启动
        key = str(trig.get("dedupe_key") or "")
        try:
            _write_pack(trig)
        except Exception as e:
            log.warning("取证包写入失败（%s）: %s", key or "?", e)
            try:
                from app.services.events import emit as emit_event
                emit_event("safety", "warn", "safety.evidence_failed",
                           f"取证包写入失败：{str(e)[:120]}", {"trigger": key})
            except Exception:
                pass
        finally:
            if key:
                with _inflight_lock:
                    _inflight.discard(key)


def _write_pack(trigger: Dict[str, Any]) -> Dict[str, Any]:
    now = time.time()
    seq = _next_seq()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pack_id = f"{stamp}-{seq}"
    root = base_dir()
    final = os.path.join(root, pack_id)
    # seq 唯一 ⇒ 目录不会撞；真撞了就再取一个号（绝不覆盖旧包 = 绝不复用序号的含义）
    while os.path.exists(final):
        seq = _next_seq()
        pack_id = f"{stamp}-{seq}"
        final = os.path.join(root, pack_id)
    # ★★ 先写**暂存目录**、写完再改名入场：列目录的人永远不会看到一个"缺 manifest 的半成品"
    #    （那会被读成"取证包损坏"，而它其实只是还在写）。
    dir_path = os.path.join(root, f".pending-{pack_id}")
    os.makedirs(dir_path)

    guard = _guard_snapshot()
    ctx, gaps = _collector_context()
    speed, gaps2 = _speed_context()
    flow, gaps3 = _flow_context()
    events, gaps4 = _recent_events()
    gaps += gaps2 + gaps3 + gaps4

    merged = guard.get("merged") or {}
    srv = guard.get("server") or {}
    cli = guard.get("client") or {}
    samples = window_samples()

    manifest: Dict[str, Any] = {
        "schema": SCHEMA,
        "seq": seq,
        "id": pack_id,
        "captured_at_utc": datetime.now(timezone.utc).isoformat() + "Z",
        "captured_at_epoch": round(now, 3),
        "trigger": {
            "source": str(trigger.get("source") or "server"),
            "from_state": trigger.get("from_state"),
            "to_state": trigger.get("to_state"),
            "zone_id": trigger.get("zone_id") or "",
            "zone_name": trigger.get("zone_name") or "",
            "clearance_m": trigger.get("clearance"),
            "clearance_mm": trigger.get("clearance_mm"),
            "ratio": trigger.get("ratio"),
            "word": str(trigger.get("word") or ""),
        },
        "fence": {
            "mode": guard.get("mode"),
            "mode_reason": guard.get("mode_reason") or "",
            "interlocked": guard.get("interlocked"),
            "merged_state": merged.get("state"),
            "merged_source": merged.get("source"),
            "client": cli,
            "server": srv,
        },
        "robot": ctx,
        "speed": speed,
        "motion": _motion_context(),
        "flow": flow,
        "commands_recent": _recent_commands(),
        "samples": {"count": len(samples), "window_seconds": buffer_seconds(),
                    "file": "samples.jsonl" if samples else None},
        "events": {"count": len(events), "file": "events.jsonl" if events else None},
        "frame": None,
        "frame_info": None,
        # ★ 前端 3D 画布截图：没有上传通道 ⇒ 如实写"没采到"，不编一个不存在的文件
        "honest_gaps": gaps + [
            "3D 画面截图（snapshot.png）未采集：前端没有画布上传通道，不假装存在",
        ],
        "files": ["manifest.json"],
        "disclaimer": ("取证包只记录本机采到的数与控制器回读的寄存器，用于事后复盘；"
                       "它不是安全认证材料，也不替代控制器安全板。"),
    }

    with open(os.path.join(dir_path, "samples.jsonl"), "w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(_scrub(s), ensure_ascii=False) + "\n")
    if not samples:
        manifest["honest_gaps"].append("环形缓冲里没有样本（取证时采集循环还没跑过）")

    trig_line = {"id": None, "timestamp": manifest["captured_at_utc"],
                 "category": "safety", "level": "critical"
                 if manifest["trigger"]["to_state"] == "hit" else "warn",
                 "action": f"safety.evidence_{manifest['trigger']['to_state']}",
                 "actor": "system",
                 "message": f"取证包 #{seq} 落盘（{manifest['trigger']['word']}）",
                 "detail": json.dumps(_scrub(manifest["trigger"]), ensure_ascii=False)}
    with open(os.path.join(dir_path, "events.jsonl"), "w", encoding="utf-8") as f:
        for ev in [trig_line] + events:
            f.write(json.dumps(_scrub(ev), ensure_ascii=False) + "\n")

    frame_file, frame_info = _camera_frame(dir_path)
    manifest["frame"] = frame_file            # ★ null 而不是"键不存在"
    manifest["frame_info"] = frame_info
    if frame_file:
        manifest["files"].append(frame_file)
    else:
        manifest["honest_gaps"].append(f"相机单帧未采到：{frame_info.get('error') or '未知原因'}")

    manifest["files"] += ["samples.jsonl", "events.jsonl"]
    manifest = _scrub(manifest)
    with open(os.path.join(dir_path, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # ★ 半成品写完了才**改名入场**：从这一刻起这个包在列表里是完整的（含 manifest）。
    try:
        os.rename(dir_path, final)
    except OSError as e:
        log.warning("取证包改名失败（保留在暂存目录 %s）: %s", dir_path, e)
        return {"ok": False, "queued": False, "error": f"取证包落盘改名失败：{str(e)[:80]}"}
    dir_path = final

    _prune()
    try:
        from app.services.events import emit as emit_event
        emit_event("safety", "info", "safety.evidence_written",
                   f"取证包 #{seq} 已落盘（{manifest['trigger']['zone_name'] or '未命名区域'}）",
                   {"seq": seq, "id": pack_id,
                    "state": manifest["trigger"]["to_state"],
                    "frame": manifest["frame"],
                    "samples": manifest["samples"]["count"]})
    except Exception:
        pass
    return {"ok": True, "queued": False, "pack": _summary(manifest, dir_path)}


def _summary(m: Dict[str, Any], dir_path: str) -> Dict[str, Any]:
    trig = m.get("trigger") or {}
    return {
        "seq": m.get("seq"),
        "id": m.get("id"),
        "captured_at_utc": m.get("captured_at_utc"),
        "to_state": trig.get("to_state"),
        "from_state": trig.get("from_state"),
        "source": trig.get("source"),
        "word": trig.get("word"),
        "zone_id": trig.get("zone_id"),
        "zone_name": trig.get("zone_name"),
        "clearance_mm": trig.get("clearance_mm"),
        "fence_mode": (m.get("fence") or {}).get("mode"),
        "frame": m.get("frame"),
        "samples": (m.get("samples") or {}).get("count"),
        "events": (m.get("events") or {}).get("count"),
        "honest_gaps": m.get("honest_gaps") or [],
        "bytes": _dir_bytes(dir_path),
    }


def _dir_bytes(dir_path: str) -> int:
    total = 0
    for name in os.listdir(dir_path):
        try:
            total += os.path.getsize(os.path.join(dir_path, name))
        except Exception:
            pass
    return total


# ---------------------------------------------------------------- 读侧
def list_packs(limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """按 **seq 倒序**（最新在前）列取证包。★ 前端水位用 `latest_seq`，不用数组长度。"""
    root = base_dir()
    rows: List[Dict[str, Any]] = []
    try:
        names = [n for n in os.listdir(root)
                 if os.path.isdir(os.path.join(root, n))
                 and not n.startswith(".")          # ★ .pending-* 是半成品，不参与列表
                 and re.match(r".*-\d+$", n)]
    except Exception:
        names = []
    for n in names:
        p = os.path.join(root, n)
        m = _read_manifest(p)
        if m is None:
            rows.append({"id": n, "seq": _seq_of(n), "corrupt": True,
                         "captured_at_utc": "", "to_state": "unknown",
                         "honest_gaps": ["manifest.json 缺失或损坏（如实标出，不静默跳过）"]})
            continue
        rows.append(_summary(m, p))
    rows.sort(key=lambda r: int(r.get("seq") or 0), reverse=True)
    latest = int(rows[0]["seq"]) if rows else 0
    return {"total": len(rows), "latest_seq": latest,
            "limit": limit, "offset": offset,
            "packs": rows[offset:offset + limit]}


def _seq_of(name: str) -> int:
    try:
        return int(str(name).rsplit("-", 1)[1])
    except Exception:
        return 0


def _read_manifest(dir_path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(os.path.join(dir_path, "manifest.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _safe_dir(pack_id: str) -> Tuple[Optional[str], Optional[str]]:
    """把外部传入的 id 约束到 base_dir 里的真实子目录（★ 防目录穿越）。"""
    root = os.path.abspath(base_dir())
    name = str(pack_id or "").strip()
    if not re.match(r"^[A-Za-z0-9._-]{1,64}$", name) or ".." in name:
        return None, f"非法的取证包 id：{pack_id!r}"
    p = os.path.abspath(os.path.join(root, name))
    if os.path.commonpath([root, p]) != root or not os.path.isdir(p):
        return None, f"取证包不存在：{pack_id!r}"
    return p, None


def get_pack(pack_id: str) -> Dict[str, Any]:
    d, err = _safe_dir(pack_id)
    if err:
        return {"ok": False, "error": err}
    m = _read_manifest(d)
    if m is None:
        return {"ok": False, "error": f"取证包 {pack_id} 的 manifest.json 读不出来（损坏或没有）"}
    files: List[Dict[str, Any]] = []
    for n in sorted(os.listdir(d)):
        fp = os.path.join(d, n)
        if not os.path.isfile(fp):
            continue
        files.append({"name": n, "bytes": os.path.getsize(fp)})
    return {"ok": True, "manifest": m, "files": files,
            "samples_preview": _tail_lines(os.path.join(d, "samples.jsonl"), 50),
            "events_preview": _tail_lines(os.path.join(d, "events.jsonl"), 50)}


def _tail_lines(path: str, n: int) -> List[Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-n:]
    except Exception:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except Exception:
            out.append(line.strip()[:400])
    return out


def pack_path(pack_id: str) -> Tuple[Optional[str], Optional[str]]:
    return _safe_dir(pack_id)


def zip_pack(pack_id: str) -> Tuple[Optional[str], Optional[str]]:
    """打成一个 zip（返回临时文件路径由调用方流式发送后删除）。"""
    d, err = _safe_dir(pack_id)
    if err:
        return None, err
    tmp = os.path.join(base_dir(), f".zipping-{pack_id}")
    try:
        return shutil.make_archive(tmp, "zip", d), None
    except Exception as e:
        return None, f"打包失败：{str(e)[:120]}"


# ---------------------------------------------------------------- 保留期清理
def _prune() -> int:
    """按 `keep_days` + `max_packs` 裁旧包（★ 不做永久保留是方案里写明的边界）。"""
    root = base_dir()
    try:
        names = [n for n in os.listdir(root)
                 if os.path.isdir(os.path.join(root, n)) and not n.startswith(".")]
    except Exception:
        return 0
    deadline = time.time() - keep_days() * 86400.0
    removed = 0
    entries = []
    for n in names:
        p = os.path.join(root, n)
        try:
            mtime = os.path.getmtime(p)
        except Exception:
            mtime = 0.0
        entries.append((mtime, n, p))
    entries.sort()
    stale = [e for e in entries if e[0] < deadline]
    for _, n, p in stale:
        try:
            shutil.rmtree(p)
            removed += 1
        except Exception as e:
            log.warning("取证包清理失败 %s: %s", n, e)
    left = [e for e in entries if e[0] >= deadline]
    cap = max_packs()
    if len(left) > cap:
        for _, n, p in left[:len(left) - cap]:
            try:
                shutil.rmtree(p)
                removed += 1
            except Exception as e:
                log.warning("取证包超量清理失败 %s: %s", n, e)
    # 崩溃/断电留下的半成品与 zip 残留：超过 1 小时的一律收掉
    try:
        for n in os.listdir(root):
            if not (n.startswith(".zipping-") or n.startswith(".pending-")):
                continue
            fp = os.path.join(root, n)
            try:
                if time.time() - os.path.getmtime(fp) <= 3600:
                    continue
                if os.path.isdir(fp):
                    shutil.rmtree(fp)
                else:
                    os.remove(fp)
            except OSError:
                pass
    except Exception:
        pass
    return removed


def prune() -> int:
    return _prune()
