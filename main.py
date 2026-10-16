# -*- coding: utf-8 -*-
"""宿舍电费查询（重庆三峡科技大学 能源自助查询缴费系统）。

功能：
1. LLM 工具 `get_dorm_power_balance`：机器人可自主查询宿舍电费余额
2. `/电费`：查询已绑定房间的余额
3. `/电费 绑定`：校区 → 楼栋 → 房间号 三步绑定
4. `/电费 解绑`、`/电费 房间`

数据：
- 绑定关系保存在插件数据目录 bindings.json，插件配置里不含任何房间号
- 校方系统的后台接口本身不校验身份，插件只在本地约束"谁能查哪个房间"
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

PLUGIN_NAME = "astrbot_plugin_dorm_electricity"
DEFAULT_BASE_URL = "http://nyglxt.sanxiau.edu.cn/weixin/ashx/frmuser.ashx"
DEFAULT_SCHOOL = "重庆三峡科技大学百安校区"
PENDING_TTL = 300  # 绑定流程状态有效期（秒）
UA = (
    "Mozilla/5.0 (Linux; Android 14; PKV110 Build/UP1A) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/4.0 Chrome/120.0 Mobile Safari/537.36 "
    "MicroMessenger/8.0.78 WeChat/arm64 Weixin NetType/WIFI Language/zh_CN"
)


@register(PLUGIN_NAME, "Eta", "宿舍电费余额查询（重庆三峡科技大学）", "0.1.0")
class DormElectricityPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._pending: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------ 配置 / 存储
    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            if hasattr(self.config, "get"):
                val = self.config.get(key, default)
                if val is not None and val != "":
                    return val
        except Exception:
            pass
        return default

    def _data_file(self) -> Path:
        try:
            return Path(StarTools.get_data_dir(PLUGIN_NAME)) / "bindings.json"
        except Exception:
            return Path(__file__).parent / "bindings.json"

    def _load(self) -> dict:
        p = self._data_file()
        try:
            if p.exists():
                data = json.loads(p.read_text("utf-8"))
                if isinstance(data, dict):
                    data.setdefault("bindings", {})
                    return data
        except Exception as e:
            logger.warning(f"[DormPower] 读取绑定数据失败: {e}")
        return {"bindings": {}}

    def _save(self, data: dict) -> None:
        p = self._data_file()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
            tmp.replace(p)
        except Exception as e:
            logger.error(f"[DormPower] 保存绑定数据失败: {e}")

    @staticmethod
    def _user_key(event: AstrMessageEvent) -> str:
        return f"{event.get_platform_id()}:{event.get_sender_id()}"

    def _bound(self, event: AstrMessageEvent) -> dict:
        return self._load()["bindings"].get(self._user_key(event)) or {}

    # ---------------------------------------------------------------- 接口调用
    async def _request(self, params: dict, method: str = "GET") -> Any:
        base = str(self._cfg("base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL)
        timeout = float(self._cfg("request_timeout", 15) or 15)
        headers = {
            "User-Agent": UA,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "http://nyglxt.sanxiau.edu.cn/weixin/lastmoney.html",
        }
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            if method == "POST":
                # 该系统要求 POST 带上 Content-Length，空 body 用 content=b""
                resp = await client.post(base, params=params, content=b"", headers=headers)
            else:
                resp = await client.get(base, params=params, headers=headers)
            resp.raise_for_status()
            text = (resp.text or "").strip()
        return json.loads(text) if text else None

    async def _schools(self) -> list:
        return list(await self._request({"test": "buildlist"}, "POST") or [])

    async def _buildings(self, school: str) -> list:
        return list(await self._request({"test": "dylist", "pid": school}, "POST") or [])

    async def _rooms(self, school: str, building: str) -> list:
        data = await self._request({"test": "mphlistk", "proid": school, "pid": building}, "POST")
        return list(data or [])

    async def _balance_row(self, school: str, room: str) -> list | None:
        data = await self._request({"test": "lastlist", "proid": school, "pid": room})
        if isinstance(data, list) and data and isinstance(data[0], list) and len(data[0]) >= 2:
            return list(data[0])
        return None

    # ------------------------------------------------------------------ 输出
    def _fmt(self, school: str, building: str, room: str, row: list) -> str:
        r = list(row) + [""] * 5
        title = " ".join(x for x in (school, building, str(r[0] or room)) if x)
        out = [f"🔌 {title}", f"余额：{r[1]} 元"]
        if r[2]:
            out.append(f"扣费日期：{r[2]}")
        if r[3]:
            out.append(f"本月用量：{r[3]} 度")
        if r[4]:
            out.append(f"电费：{r[4]} 度")
        try:
            bal = float(r[1])
        except Exception:
            bal = None
        if bal is not None:
            thr = float(self._cfg("low_balance_threshold", 10) or 0)
            if bal < 0:
                out.append("⚠️ 余额已是负数，欠费了，赶紧充")
            elif thr > 0 and bal < thr:
                out.append(f"⚠️ 余额低于 {thr:g} 元，记得充一点")
        return "\n".join(out)

    def _help(self) -> str:
        return (
            "宿舍电费查询\n"
            "/电费 —— 查询已绑定房间的余额\n"
            "/电费 绑定 —— 选择校区 → 楼栋 → 房间号\n"
            "/电费 房间 —— 查看当前绑定\n"
            "/电费 解绑 —— 解除绑定"
        )

    # ------------------------------------------------------------------ 命令
    @filter.command("电费", alias={"查电费", "宿舍电费", "电费余额"})
    async def cmd_electricity(self, event: AstrMessageEvent, arg: str = ""):
        """查询宿舍电费余额。用法：/电费 | /电费 绑定 | /电费 解绑 | /电费 房间"""
        arg = (arg or "").strip()
        low = arg.lower()
        if low in ("绑定", "bind", "设置"):
            yield event.plain_result(await self._begin_bind(event))
            return
        if low in ("解绑", "unbind"):
            yield event.plain_result(self._unbind(event))
            return
        if low in ("房间", "状态", "room", "info"):
            yield event.plain_result(self._show_binding(event))
            return
        if low in ("帮助", "help", "?", "？"):
            yield event.plain_result(self._help())
            return
        try:
            yield event.plain_result(await self._query(event, arg))
        except Exception as e:
            logger.error(f"[DormPower] 查询失败: {e}")
            yield event.plain_result(f"查询失败：{e}")

    @filter.command("电费绑定", alias={"绑定电费", "绑定宿舍"})
    async def cmd_bind(self, event: AstrMessageEvent):
        """开始绑定宿舍房间。"""
        yield event.plain_result(await self._begin_bind(event))

    @filter.command("电费解绑", alias={"解绑电费"})
    async def cmd_unbind(self, event: AstrMessageEvent):
        """解除已绑定的宿舍房间。"""
        yield event.plain_result(self._unbind(event))

    # -------------------------------------------------------------- 绑定流程
    async def _begin_bind(self, event: AstrMessageEvent) -> str:
        if not self._cfg("allow_self_bind", True) and not event.is_admin():
            return "当前只允许管理员绑定房间。"
        try:
            schools = await self._schools()
        except Exception as e:
            return f"获取校区列表失败：{e}"
        if not schools:
            return "没有取到校区列表，系统可能暂时不可用。"

        key = self._user_key(event)
        self._pending[key] = {"stage": "school", "schools": schools, "ts": time.time()}
        lines = ["请选择校区（回复序号，「取消」可中止）："]
        lines += [f"{i + 1}. {s}" for i, s in enumerate(schools)]
        return "\n".join(lines)

    @staticmethod
    def _parse_index(text: str, n: int) -> int | None:
        t = (text or "").strip()
        if not t.isdigit():
            return None
        i = int(t)
        return i - 1 if 1 <= i <= n else None

    async def _advance(self, event: AstrMessageEvent, text: str) -> str:
        key = self._user_key(event)
        st = self._pending.get(key)
        if not st:
            return ""
        st["ts"] = time.time()
        stage = st.get("stage")

        if stage == "school":
            idx = self._parse_index(text, len(st.get("schools") or []))
            if idx is None:
                return "请输入有效的校区序号，例如 1"
            school = st["schools"][idx]
            buildings = await self._buildings(school)
            if not buildings:
                self._pending.pop(key, None)
                return f"{school} 下没有取到楼栋列表，请稍后再试。"
            st.update({"stage": "building", "school": school, "buildings": buildings})
            lines = [f"已选校区：{school}", "请选择楼栋（回复序号，「取消」可中止）："]
            lines += [f"{i + 1}. {b}" for i, b in enumerate(buildings)]
            return "\n".join(lines)

        if stage == "building":
            idx = self._parse_index(text, len(st.get("buildings") or []))
            if idx is None:
                return "请输入有效的楼栋序号，例如 17"
            building = st["buildings"][idx]
            rooms = await self._rooms(st["school"], building)
            if not rooms:
                self._pending.pop(key, None)
                return f"{building} 下没有取到房间列表，请重新 /电费 绑定。"
            st.update({"stage": "room", "building": building, "rooms": rooms})
            sample = []
            for it in rooms:
                t = str(it.get("text")).strip() if isinstance(it, dict) else str(it).strip()
                if t:
                    sample.append(t)
                if len(sample) >= 5:
                    break
            return (
                f"已选：{st['school']} {building}（共 {len(rooms)} 个房间）\n"
                f"请直接回复你的房间号，例如：{'、'.join(sample)}\n"
                "（「取消」可中止）"
            )

        if stage == "room":
            room = text.strip().upper().replace(" ", "")
            if not room:
                return "请回复房间号，例如 L1-0001"
            valid: list[str] = []
            for it in st.get("rooms") or []:
                t = str(it.get("text")).strip() if isinstance(it, dict) else str(it).strip()
                if t:
                    valid.append(t)
            if valid and room not in {v.upper() for v in valid}:
                return (
                    f"「{text}」不在 {st['building']} 的房间列表里，请重新输入，例如："
                    + "、".join(valid[:5])
                )
            row = await self._balance_row(st["school"], room)
            if not row:
                return f"没查到「{text}」的用电数据，请确认房间号后重新输入，或回复「取消」。"
            data = self._load()
            data["bindings"][key] = {
                "school": st["school"],
                "building": st["building"],
                "room": str(row[0] or room),
                "bind_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._save(data)
            self._pending.pop(key, None)
            return "绑定成功！\n" + self._fmt(st["school"], st["building"], str(row[0] or room), row) + "\n\n之后发「/电费」即可查询。"

        self._pending.pop(key, None)
        return "绑定流程已重置，请重新 /电费 绑定。"

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """绑定流程中接管用户的后续回复（校区 / 楼栋 / 房间号）。"""
        key = self._user_key(event)
        st = self._pending.get(key)
        if not st:
            return
        if time.time() - float(st.get("ts") or 0) > PENDING_TTL:
            self._pending.pop(key, None)
            return
        text = (event.get_message_str() or "").strip()
        if not text:
            return
        if text.startswith("/") and text.lstrip("/").strip() not in ("取消", "cancel"):
            # 让命令继续走各自的处理器
            self._pending.pop(key, None)
            return
        event.stop_event()
        if text.lstrip("/").strip() in ("取消", "cancel"):
            self._pending.pop(key, None)
            yield event.plain_result("已取消绑定。")
            return
        try:
            reply = await self._advance(event, text)
        except Exception as e:
            logger.error(f"[DormPower] 绑定流程出错: {e}")
            self._pending.pop(key, None)
            reply = f"绑定流程出错：{e}，请重新 /电费 绑定。"
        if reply:
            yield event.plain_result(reply)

    # ------------------------------------------------------------------ 查询
    async def _query(self, event: AstrMessageEvent, room_arg: str = "") -> str:
        b = self._bound(event)
        room_arg = (room_arg or "").strip().upper()
        if room_arg:
            if not self._cfg("allow_any_room", False):
                return (
                    "为了保护隐私，插件默认只查询你绑定的房间。\n"
                    "发「/电费 绑定」绑定自己的宿舍即可；\n"
                    "（管理员可在插件配置里开启 allow_any_room 来支持指定房间号查询）"
                )
            school = str(b.get("school") or self._cfg("default_school", DEFAULT_SCHOOL))
            building = str(b.get("building") or "")
            target = room_arg
        elif b.get("room"):
            school = str(b.get("school") or "")
            building = str(b.get("building") or "")
            target = str(b["room"])
        else:
            return "你还没有绑定宿舍房间。\n发「/电费 绑定」，按提示选择校区、楼栋，再回复房间号即可。\n" + self._help()

        row = await self._balance_row(school, target)
        if not row:
            return f"没查到 {school} {target} 的用电数据，房间号可能不对，或系统暂时不可用。"
        return self._fmt(school, building, target, row)

    def _show_binding(self, event: AstrMessageEvent) -> str:
        b = self._bound(event)
        if not b:
            return "你还没有绑定宿舍房间。发「/电费 绑定」开始绑定。"
        return (
            f"当前绑定：{b.get('school', '')} {b.get('building', '')} {b.get('room', '')}\n"
            f"绑定时间：{b.get('bind_at', '未知')}"
        )

    def _unbind(self, event: AstrMessageEvent) -> str:
        data = self._load()
        key = self._user_key(event)
        if key in data["bindings"]:
            data["bindings"].pop(key)
            self._save(data)
            return "已解除绑定。"
        return "你当前没有绑定房间。"

    # ---------------------------------------------------------------- LLM 工具
    @filter.llm_tool(name="get_dorm_power_balance")
    async def tool_dorm_power_balance(self, event: AstrMessageEvent, room: str = ""):
        """查询学生宿舍的电费余额（宿舍还剩多少电费、够不够用、本月用了多少度电）。

        何时使用：当用户问宿舍电费还剩多少钱、余额够不够、要不要去充电费、这个月用了多少度电的时候。

        Args:
            room (string): 可选。宿舍房间号，例如 "L1-0001"。留空则查询该用户已绑定的宿舍房间。
        """
        try:
            return await self._query(event, room)
        except Exception as e:
            logger.error(f"[DormPower] LLM 工具查询失败: {e}")
            return f"查询电费失败：{e}"
