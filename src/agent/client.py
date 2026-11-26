"""与 War Thunder 本地战术接口(默认 http://localhost:8111)交互的异步客户端。

端点全集与用法以游戏自带前端 http://127.0.0.1:8111/ 的 JS 为准(2026-09-18 真机核对):
  /state                   玩家载具遥测(键名带单位后缀, 如 "H, m" "AoA, deg")
  /indicators              操纵面/踏板/仪表针
  /hudmsg?lastEvt=&lastDmg= HUD 消息双游标增量(裸 GET 返回 400)
  /mission.json            只有 {objectives, status}; **不含地图名**
  /map_info.json           地图几何 + map_generation(底图是否要重取的依据)
  /map.img?gen=N           512x512 战术地图, **JPEG**(约 347KB/次, 单次 ~375ms)
  /gamechat?lastId=N       对局聊天(本项目未用)
服务端串行处理请求: 拉大图要按 gen 变化才做, 否则会把 /state 等拖到超时。
"""
from __future__ import annotations

import asyncio
import re
import logging
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

# config.blk 中可能的安装路径(按常见 Steam/独立安装器位置)
_BLK_CANDIDATES = [
    r"C:\Program Files (x86)\Steam\steamapps\common\War Thunder\config.blk",
    r"C:\Program Files\Steam\steamapps\common\War Thunder\config.blk",
    r"D:\Steam\steamapps\common\War Thunder\config.blk",
    r"D:\SteamLibrary\steamapps\common\War Thunder\config.blk",
    r"C:\Games\War Thunder\config.blk",
]

# 形如  hud{ internetServer{ port:int = 8111 } }
_PORT_RE = re.compile(r"internetServer\s*\{[^}]*?port\s*:\s*int\s*=\s*(\d+)", re.S)


def detect_port(cfg_blk_path: str = "", fallback: int = 8111) -> int:
    """从 config.blk 尝试探测 internetServer 端口; 失败回退 fallback。"""
    candidates: list[str] = []
    if cfg_blk_path:
        candidates.append(cfg_blk_path)
    candidates.extend(_BLK_CANDIDATES)
    for p in candidates:
        try:
            text = Path(p).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m = _PORT_RE.search(text)
        if m:
            port = int(m.group(1))
            log.info("从 %s 探测到端口 %d", p, port)
            return port
    log.info("未在 config.blk 探测到端口, 使用回退 %d", fallback)
    return fallback


class WTClient:
    """轻量异步客户端; 一个 httpx.AsyncClient 跨端点复用连接。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 8111, timeout: float = 2.0,
                 transport: httpx.BaseTransport | None = None):
        self.base = f"http://{host}:{port}"
        self._client: httpx.AsyncClient | None = None
        self._timeout = timeout
        self._transport = transport   # 测试可注入 httpx.MockTransport

    async def __aenter__(self) -> "WTClient":
        # trust_env=False: **不读系统/环境代理**(ALL_PROXY / HTTP_PROXY)。
        # 我们只连本机 127.0.0.1, 走代理毫无意义; 而机器上只要配了一个非法的代理变量,
        # httpx 就会在构造时抛 InvalidURL(实测 "Invalid port: ':1]'"), 整个轮询线程直接崩。
        self._client = httpx.AsyncClient(base_url=self.base, timeout=self._timeout,
                                         transport=self._transport, trust_env=False)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _c(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base, timeout=self._timeout,
                                             trust_env=False,
                                             transport=self._transport)
        return self._client

    async def _get(self, path: str, timeout: float | None = None,
                   params: dict[str, Any] | None = None) -> httpx.Response:
        kw: dict[str, Any] = {}
        if timeout is not None:
            kw["timeout"] = timeout
        if params is not None:
            kw["params"] = params
        return await self._c().get(path, **kw)

    async def ping(self, timeout: float | None = None) -> bool:
        """探活; 传 timeout 即可用短超时快速判定"端口没人监听"。"""
        try:
            r = await self._get("/state", timeout=timeout)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def fetch_state(self, timeout: float | None = None) -> dict[str, Any]:
        r = await self._get("/state", timeout=timeout)
        r.raise_for_status()
        return r.json()

    async def fetch_indicators(self) -> dict[str, Any]:
        r = await self._get("/indicators")
        r.raise_for_status()
        return r.json()

    async def fetch_hudmsg(self, last_evt: int = 0, last_dmg: int = 0) -> dict[str, Any]:
        """GET /hudmsg?lastEvt=&lastDmg= —— 双游标增量。裸 GET 实测返回 400。

        响应: {"events":[{id,msg,sender,enemy,mode,time}], "damage":[同结构]}
        """
        r = await self._get("/hudmsg", params={"lastEvt": int(last_evt), "lastDmg": int(last_dmg)})
        r.raise_for_status()
        return r.json()

    async def fetch_mission(self) -> dict[str, Any]:
        r = await self._get("/mission.json")
        r.raise_for_status()
        return r.json()

    async def fetch_map_objects(self) -> list:
        r = await self._get("/map_obj.json")
        r.raise_for_status()
        return r.json() if r.content.strip() else []

    async def fetch_map_info(self) -> dict[str, Any]:
        r = await self._get("/map_info.json")
        r.raise_for_status()
        return r.json()

    async def fetch_map_img_bytes(self, gen: int | None = None) -> bytes:
        """官方做法是只在 map_generation 变化时请求 /map.img?gen=N(内容是 JPEG)。"""
        params = None if gen is None else {"gen": int(gen)}
        r = await self._get("/map.img", params=params)
        r.raise_for_status()
        return r.content
