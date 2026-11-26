"""击杀连线：记录"谁在哪击毁了谁"，供地图上画「攻击者 → 被击毁」的红线。

用户 2026-10-17 看了 `thunder.nanachi.party` 的热力图后要求：
「他有会保存，击杀时攻击者和被击毁坦克的红线，这个你能做吗」。

数据全部来自本地（无外部依赖）：
  * **谁杀谁** ← `/hudmsg` 的击杀文案（poller → `main_window._on_hud_events`）
  * **两端坐标** ← 当时 `/map_obj` 里两个单位的最后位置（`map_view.unit_xy`）

坐标以**归一化**（0~1）保存，与底图分辨率无关；按场景分文件：

    data/killlines/<scene>.jsonl      每行 {"a":[x,y],"b":[x,y],"t":秒}

追加写 + 内存缓存（一次读入），关掉程序也不丢 —— 这就是用户要的「会保存」。

⚠ 精度说明：击杀文案到达与轮询之间有 0.2~2 秒延迟，位置是**那一刻的近似**；坦克基本准，
飞机可能偏几十米。帧内找不到某端（比如它已经在上一帧消失）就不记这条，宁缺勿脏。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from .mapimg import synchronized, safe_scene_name

log = logging.getLogger(__name__)

MAX_KEEP = 4000          # 每个场景最多保留多少条（超出丢最旧的），防止文件无限增长


class KillLines:
    """按场景保存的击杀连线集合。线程/调用都发生在 UI 线程，不做加锁。"""

    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self._cache: dict[str, list[dict]] = {}
        self._versions: dict[str, tuple | None] = {}

    # ------------------------------------------------------------------ 路径
    def path(self, scene: str | None) -> Path:
        return self.data_root / "killlines" / f"{safe_scene_name(scene)}.jsonl"

    # ------------------------------------------------------------------ 读
    @synchronized
    def lines(self, scene: str | None) -> list[dict]:
        """该场景的全部击杀连线（首次从磁盘读入并缓存）。"""
        key = str(scene or "_unknown")
        p = self.path(scene)
        try:
            stat = p.stat()
            version = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            version = None
        got = self._cache.get(key)
        if got is not None and self._versions.get(key) == version:
            return got
        out: list[dict] = []
        try:
            if p.is_file():
                for raw in p.read_text(encoding="utf-8").splitlines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        d = json.loads(raw)
                        a, b = d["a"], d["b"]
                        out.append({"a": [float(a[0]), float(a[1])],
                                    "b": [float(b[0]), float(b[1])],
                                    "t": float(d.get("t") or 0.0)})
                    except Exception:          # noqa: BLE001 - 单行坏了就跳过
                        continue
        except OSError:
            log.debug("读击杀连线失败: %s", p, exc_info=True)
        out = out[-MAX_KEEP:]
        self._cache[key] = out
        self._versions[key] = version
        return out

    # ------------------------------------------------------------------ 写
    @synchronized
    def add(self, scene: str | None, a, b, t: float = 0.0) -> bool:
        """记一条击杀连线；两端必须是合法归一化坐标，否则不记（宁缺勿脏）。"""
        try:
            ax, ay = float(a[0]), float(a[1])
            bx, by = float(b[0]), float(b[1])
        except (TypeError, ValueError, IndexError):
            return False
        if not all(0.0 <= v <= 1.0 for v in (ax, ay, bx, by)):
            return False
        rec = {"a": [round(ax, 4), round(ay, 4)],
               "b": [round(bx, 4), round(by, 4)],
               "t": round(float(t or 0.0), 1)}
        arr = self.lines(scene)
        arr.append(rec)
        drop = len(arr) - MAX_KEEP
        if drop > 0:
            del arr[:drop]
        self._append(scene, rec)
        return True

    def _append(self, scene: str | None, rec: dict) -> None:
        p = self.path(scene)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            stat = p.stat()
            self._versions[str(scene or "_unknown")] = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            log.debug("写击杀连线失败: %s", p, exc_info=True)

    @synchronized
    def clear(self, scene: str | None) -> None:
        """清空某个场景的连线（内存 + 磁盘）。"""
        self._cache[str(scene or "_unknown")] = []
        try:
            self.path(scene).unlink(missing_ok=True)
        except OSError:
            pass

    def count(self, scene: str | None) -> int:
        return len(self.lines(scene))
