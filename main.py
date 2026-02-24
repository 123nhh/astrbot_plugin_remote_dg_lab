"""
AstrBot 插件 - DG-Lab-Play (郊狼玩法)

移植自 nonebot-plugin-dg-lab-play (https://github.com/Ljzd-PRO/nonebot-plugin-dg-lab-play)
原项目使用 BSD 3-Clause 许可证, Copyright © 2024 by Ljzd-PRO.

在群里和大家一起玩郊狼吧！支持多个郊狼玩家同时连接，通过 DG-Lab App Socket 协议控制设备。
"""

import asyncio
import random
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional, Dict, List, Tuple

import qrcode
from pydglab_ws import Channel, StrengthOperationType

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.message_components import Plain, Image, At
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

from .client_manager import ClientManager, DGLabPlayConfig
from .model import custom_pulse_data, load_custom_pulse_data, parse_dungeonlab_pulse, load_pulse_files_from_dir


MAX_STRENGTH_PCT = 90.0
"""除一键制裁外，所有玩法的最大强度上限（百分比）"""

SHIELD_TRIGGER_PROB = 0.45
"""护盾随机触发概率"""

SHIELD_RECHARGE_SECS = 2700
"""护盾充能间隔（秒），45 分钟"""

# ==================== 回复文本常量 ====================
class ReplyText:
    bind_timeout = "绑定超时"
    current_players = "当前玩家："
    current_pulse = "当前波形循环为：【{}】"
    current_strength = "A通道：{0}/{1} B通道：{2}/{3}"
    failed_to_create_client = "创建 DG-Lab 控制终端失败"
    failed_to_fetch_strength_info = "获取通道强度状态失败"
    failed_to_fetch_strength_limit = "获取通道强度上限失败，控制失败"
    game_exited = "已退出游戏"
    invalid_pulse_param = "波形参数错误，控制失败"
    invalid_strength_param = "强度参数错误，控制失败"
    invalid_target = "目标玩家不存在或郊狼 App 未绑定"
    no_available_pulse = "无可用波形"
    no_player = "当前没有已连接的玩家，你可以绑定试试~"
    not_bind_yet = "你目前没有绑定 DG-Lab App"
    please_at_target = "使用命令的同时请 @ 想要控制的玩家"
    please_scan_qrcode = "请用 DG-Lab App 扫描二维码以连接"
    please_set_pulse_first = "请先设置郊狼波形：/随机波形 @用户"
    pulses_empty = "当前波形循环为空"
    successfully_bind = "绑定成功，可以开始色色了！"
    successfully_decreased = "郊狼强度减小了 {}%"
    successfully_increased = "郊狼强度加强了 {}%！"
    successfully_set_pulse = "郊狼波形成功设置为【{}】！"
    successfully_set_to_strength = "郊狼强度成功设置为 {}%！"


# ==================== 帮助文本 ====================
USAGE_TEXT = """\
⚡ DG-Lab-Play 郊狼玩法说明 ⚡

📲连接 DG-Lab App：/绑定郊狼
🕹️查看当前玩家：/当前玩家
🚪退出游戏：/退出游戏

🔺增加玩家通道强度：/加大强度 @用户 <百分比>
🔻减小玩家通道强度：/减小强度 @用户 <百分比>
🎚️查看当前通道强度：/当前强度 @用户
🎲随机通道强度：/随机强度 @用户

� 波形由系统自动随机切换（10s 一次），无需手动设置
🏷️查看可用波形：/可用波形
📤上传自定义波形：将 *.pulse 文件发送到群文件，自动解析添加
   或发送 /导入波形 并粘贴 Dungeonlab+pulse:... 文本

🎯 进阶玩法（最高强度 90%）：
🎲掷骰子惩罚：/郊狼骰子 @用户
🌪️随机风暴：/随机风暴 @用户 [时长秒] [次数]
📈渐强惩罚：/渐强惩罚 @用户 [时长秒] [最终百分比]
💀一键制裁：/一键制裁 @用户 [时长秒]（满功率，不受 90% 限制）
  └ 每人每小时最多发起 3 次，被控者免审 1 次/h，超出需批准
✅批准制裁请求：/批准制裁
🎰郊狼轮盘：/郊狼轮盘
⚔️郊狼决斗（双方掷骰子，输家受罚）：/郊狼决斗 @用户
🔢猜数字（5次机会猜 1-100，猜错受罚）：/猜数字 @用户
🔗接力惩罚（依次传递递增惩罚）：/接力惩罚 [起始强度%]
⛔停止玩法任务：/停止任务 @用户

🛡️ 反弹护盾机制：
每位玩家绑定后自动获得反弹护盾（每 45 分钟自动补充 1 层）
加强度 ≥30% 或受其他惩罚时，护盾有概率随机触发，反弹给攻击者


🔗项目链接：https://github.com/123nhh/astrbot_plugin_remote_dg_lab
"""


# ==================== 骰子结果表 ====================
DICE_OUTCOMES = [
    {
        "face": "⚀", "value": 1, "name": "平安无事",
        "desc": "幸运！什么都没有发生~",
        "strength": None, "duration": 0,
    },
    {
        "face": "⚁", "value": 2, "name": "微风拂过",
        "desc": "一阵微弱的电流掠过...",
        "strength": (10, 30), "duration": 10,
    },
    {
        "face": "⚂", "value": 3, "name": "电流穿行",
        "desc": "感觉来了！电流在体内穿行！",
        "strength": (30, 60), "duration": 20,
    },
    {
        "face": "⚃", "value": 4, "name": "雷霆洗礼",
        "desc": "轰！！！雷霆万钧！",
        "strength": (60, 85), "duration": 30,
    },
    {
        "face": "⚄", "value": 5, "name": "极限风暴",
        "desc": "暴风雨来临！！无处可逃！！！",
        "strength": (85, 100), "duration": 45,
    },
    {
        "face": "⚅", "value": 6, "name": "命运逆转",
        "desc": "命运的齿轮开始转动...惩罚反弹给施法者！",
        "strength": (50, 100), "duration": 30, "reverse": True,
    },
]


def _get_at_target(event: AstrMessageEvent) -> Optional[str]:
    """从消息链中提取 @ 的目标用户 ID"""
    for comp in event.get_messages():
        if isinstance(comp, At):
            target = str(comp.qq) if hasattr(comp, 'qq') and comp.qq else None
            if target and target != "all":
                return target
    return None


def _parse_percentage_from_text(event: AstrMessageEvent) -> Optional[float]:
    """从消息文本中解析百分比数值"""
    text = event.message_str.strip() if hasattr(event, 'message_str') else ""
    for part in text.split():
        try:
            val = float(part)
            if 0 < val <= 100:
                return val
        except (ValueError, TypeError):
            continue
    return None


def _parse_pulse_name_from_text(event: AstrMessageEvent) -> Optional[str]:
    """从消息文本中解析波形名称"""
    text = event.message_str.strip() if hasattr(event, 'message_str') else ""
    # 去除可能的数字部分（百分比值），逐词匹配
    parts = text.split()
    for part in parts:
        if part and custom_pulse_data.get(part) is not None:
            return part
    # 尝试用整个剩余文本
    clean = text.strip()
    if clean and custom_pulse_data.get(clean) is not None:
        return clean
    return None


def _parse_numbers_from_text(event: AstrMessageEvent) -> list:
    """从消息文本中提取所有数字"""
    text = event.message_str.strip() if hasattr(event, 'message_str') else ""
    numbers = []
    for part in text.split():
        try:
            numbers.append(float(part))
        except (ValueError, TypeError):
            continue
    return numbers


@register("astrbot_plugin_remote_dg_lab", "Ljzd-PRO & AstrBot Porter",
          "DG-Lab-Play 郊狼玩法 - 在群里和大家一起玩郊狼吧！", "1.2.0",
          "https://github.com/123nhh/astrbot_plugin_remote_dg_lab")
class DGLabPlayPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.dg_config: Optional[DGLabPlayConfig] = None
        self.client_manager: Optional[ClientManager] = None
        self._data_dir: Optional[Path] = None
        self._active_tasks: dict = {}  # user_id -> asyncio.Task
        # 一键制裁限制：记录时间戳
        self._sanction_attacker_log: Dict[str, List[float]] = defaultdict(list)  # 施法者 -> [时间戳]
        self._sanction_victim_log: Dict[str, List[float]] = defaultdict(list)    # 被控者 -> [时间戳]
        # 待审批的制裁请求: victim_id -> (attacker_id, play_client, umo, duration)
        self._pending_sanctions: Dict[str, Tuple[str, object, str, float]] = {}
        # 反弹护盾: user_id -> {"charges": int, "last_recharge": float}
        self._shields: Dict[str, dict] = {}
        # 猜数字游戏状态: victim_id -> {"answer": int, "attacker": str, "remaining": int, "umo": str}
        self._guess_games: Dict[str, dict] = {}
        # 波形背景轮播任务: user_id -> asyncio.Task
        self._wave_rotation_tasks: Dict[str, asyncio.Task] = {}
        # 存放 .pulse 文件的目录
        self._pulses_dir: Optional[Path] = None

    async def initialize(self):
        """插件初始化：加载配置、波形数据、启动 WS 服务端"""
        try:
            self.dg_config = DGLabPlayConfig(self.config)

            self._data_dir = Path("data/plugin_data/astrbot_plugin_remote_dg_lab")
            self._data_dir.mkdir(parents=True, exist_ok=True)

            self._pulses_dir = self._data_dir / "pulses"
            self._pulses_dir.mkdir(parents=True, exist_ok=True)

            load_custom_pulse_data(self._data_dir, logger)
            n = load_pulse_files_from_dir(self._pulses_dir, logger)
            if n:
                logger.info(f"共额外加载 {n} 个 .pulse 波形文件")

            self.client_manager = ClientManager(self.dg_config, logger)
            self.client_manager.serve()

            if not self.dg_config.remote_server:
                if self.dg_config.local_server_publish_uri == "ws://127.0.0.1:4567":
                    logger.warning(
                        "⚠️ 未修改默认本地服务端的 local_server_publish_uri，"
                        "DG-Lab App 将可能无法通过生成的二维码进行连接。"
                        "请在插件配置中修改为公网可访问的地址。"
                    )

            logger.info("✅ DG-Lab-Play 插件初始化完成")
        except Exception as e:
            logger.error(f"❌ DG-Lab-Play 插件初始化失败: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def _generate_qr_image(self, qrcode_data: str, user_id: str) -> str:
        """生成 QR 码图片并返回文件路径"""
        qr_path = str(self._data_dir / f"qr_{user_id}.png")
        qr_img = qrcode.make(qrcode_data)
        qr_img.save(qr_path, "PNG")
        return qr_path

    # ==================== 绑定 / 玩家管理指令 ====================

    @filter.command("绑定郊狼")
    async def bind_dg_lab(self, event: AstrMessageEvent):
        """连接 DG-Lab App，扫描二维码绑定设备"""
        user_id = event.get_sender_id()
        umo = event.unified_msg_origin

        play_client = self.client_manager.user_id_to_client.get(user_id)
        if not play_client:
            play_client = await self.client_manager.new_client(user_id)

        if not play_client:
            yield event.plain_result(ReplyText.failed_to_create_client)
            return

        qrcode_data = play_client.qrcode
        qr_path = self._generate_qr_image(qrcode_data, user_id)

        yield event.chain_result([
            Image.fromFileSystem(qr_path),
            Plain(ReplyText.please_scan_qrcode)
        ])

        # 后台等待绑定完成并通知
        asyncio.create_task(self._wait_bind_result(play_client, umo))

    async def _wait_bind_result(self, play_client, umo: str):
        """等待绑定结果并发送通知"""
        async with play_client.bind_finished_lock:
            pass

        chain = MessageChain()
        if not play_client.is_destroyed:
            chain.message(
                ReplyText.successfully_bind + "\n\n"
                "🛡️ 你已获得一层『反弹护盾』！\n"
                "• 当你被 ≥30% 强度攻击或受到惩罚时，护盾有概率随机触发，将惩罚反弹给攻击者\n"
                "• 每 45 分钟自动补充 1 层，最多拥有 1 层\n"
                "• 私聊发送 /护盾状态 可查看护盾情况"
            )
            self._grant_shield(play_client.user_id)
            self._start_wave_rotation(play_client.user_id, play_client)
        else:
            chain.message(ReplyText.bind_timeout)

        try:
            await self.context.send_message(umo, chain)
        except Exception as e:
            logger.error(f"发送绑定结果消息失败: {e}")

    @filter.command("当前玩家")
    async def show_players(self, event: AstrMessageEvent):
        """查看当前已连接的玩家列表"""
        if self.client_manager.user_id_to_client:
            player_ids = list(self.client_manager.user_id_to_client.keys())
            components = [Plain(ReplyText.current_players)]
            for uid in player_ids:
                components.append(At(qq=uid))
                components.append(Plain(" "))
            yield event.chain_result(components)
        else:
            yield event.plain_result(ReplyText.no_player)

    @filter.command("退出游戏")
    async def exit_game(self, event: AstrMessageEvent):
        """退出郊狼游戏，断开连接"""
        user_id = event.get_sender_id()
        play_client = self.client_manager.user_id_to_client.get(user_id)
        if play_client:
            self._stop_wave_rotation(user_id)
            await play_client.destroy()
            yield event.plain_result(ReplyText.game_exited)
        else:
            yield event.plain_result(ReplyText.not_bind_yet)

    # ==================== 强度控制指令 ====================

    async def _strength_control(
        self, event: AstrMessageEvent,
        mode: StrengthOperationType,
        target_user_id: str,
        percentage_value: Optional[float]
    ):
        """通用强度控制逻辑（上限 MAX_STRENGTH_PCT）"""
        if percentage_value is None or not (0 < percentage_value <= 100):
            yield event.plain_result(ReplyText.invalid_strength_param)
            return

        # 非制裁类强度控制限制上限
        percentage_value = min(percentage_value, MAX_STRENGTH_PCT)

        play_client = self.client_manager.user_id_to_client.get(target_user_id)
        if not play_client:
            yield event.plain_result(ReplyText.invalid_target)
            return

        if not play_client.last_strength:
            yield event.plain_result(ReplyText.failed_to_fetch_strength_limit)
            return

        a_value = round(play_client.last_strength.a_limit * (percentage_value / 100))
        b_value = round(play_client.last_strength.b_limit * (percentage_value / 100))

        await play_client.client.set_strength(Channel.A, mode, a_value)
        await play_client.client.set_strength(Channel.B, mode, b_value)

        if mode == StrengthOperationType.INCREASE:
            success_text = ReplyText.successfully_increased.format(round(percentage_value))
        elif mode == StrengthOperationType.DECREASE:
            success_text = ReplyText.successfully_decreased.format(round(percentage_value))
        elif mode == StrengthOperationType.SET_TO:
            success_text = ReplyText.successfully_set_to_strength.format(round(percentage_value))
        else:
            return

        yield event.plain_result(success_text)

    @filter.command("加大强度")
    async def increase_strength(self, event: AstrMessageEvent):
        """增加玩家通道强度：/加大强度 @用户 <百分比>"""
        target = _get_at_target(event)
        if not target:
            yield event.plain_result(ReplyText.please_at_target)
            return
        percentage = _parse_percentage_from_text(event)

        # 护盾检查：加大强度 ≥30% 时可触发反弹
        if percentage is not None and percentage >= 30:
            sender_id = event.get_sender_id()
            play_client = self.client_manager.user_id_to_client.get(target)
            sender_client = self.client_manager.user_id_to_client.get(sender_id)
            reflected = await self._try_shield_reflect(
                sender_id, target, event.unified_msg_origin,
                play_client, sender_client,
                lambda sc, sid: self._dice_punishment_task(
                    sc, sid, event.unified_msg_origin,
                    (int(percentage), int(min(percentage + 20, MAX_STRENGTH_PCT))), 15
                )
            )
            if reflected:
                return

        async for result in self._strength_control(event, StrengthOperationType.INCREASE, target, percentage):
            yield result

    @filter.command("减小强度")
    async def decrease_strength(self, event: AstrMessageEvent):
        """减小玩家通道强度：/减小强度 @用户 <百分比>"""
        target = _get_at_target(event)
        if not target:
            yield event.plain_result(ReplyText.please_at_target)
            return
        percentage = _parse_percentage_from_text(event)
        async for result in self._strength_control(event, StrengthOperationType.DECREASE, target, percentage):
            yield result

    @filter.command("随机强度")
    async def random_strength(self, event: AstrMessageEvent):
        """将强度随机设置：/随机强度 @用户"""
        target = _get_at_target(event)
        if not target:
            yield event.plain_result(ReplyText.please_at_target)
            return
        random_value = float(random.randint(0, 100))
        async for result in self._strength_control(event, StrengthOperationType.SET_TO, target, random_value):
            yield result

    # ==================== 强度查询指令 ====================

    @filter.command("当前强度")
    async def current_strength(self, event: AstrMessageEvent):
        """查看当前通道强度：/当前强度 @用户"""
        target = _get_at_target(event)
        if not target:
            yield event.plain_result(ReplyText.please_at_target)
            return

        play_client = self.client_manager.user_id_to_client.get(target)
        if not play_client:
            yield event.plain_result(ReplyText.invalid_target)
            return

        if play_client.last_strength:
            yield event.plain_result(
                ReplyText.current_strength.format(
                    play_client.last_strength.a,
                    play_client.last_strength.a_limit,
                    play_client.last_strength.b,
                    play_client.last_strength.b_limit,
                )
            )
        else:
            yield event.plain_result(ReplyText.failed_to_fetch_strength_info)

    # ==================== 波形信息指令 ====================

    @filter.command("可用波形")
    async def show_pulses(self, event: AstrMessageEvent):
        """列出所有可用波形名称"""
        if custom_pulse_data:
            yield event.plain_result("🎵 当前可用波形（系统自动轮播，无需手动切换）：\n" + "、".join(custom_pulse_data.keys))
        else:
            yield event.plain_result(ReplyText.no_available_pulse)

    # ==================== 帮助指令 ====================

    @filter.command("郊狼玩法")
    async def show_usage(self, event: AstrMessageEvent):
        """显示 DG-Lab-Play 完整帮助信息"""
        yield event.plain_result(USAGE_TEXT)

    # ==================== 内部辅助方法 ====================

    async def _set_strength_pct(self, play_client, percentage: float, bypass_cap: bool = False) -> bool:
        """直接设置玩家强度为指定百分比（默认上限 MAX_STRENGTH_PCT）"""
        if not play_client.last_strength or play_client.is_destroyed:
            return False
        cap = 100.0 if bypass_cap else MAX_STRENGTH_PCT
        pct = min(percentage, cap)
        a_val = round(play_client.last_strength.a_limit * (pct / 100))
        b_val = round(play_client.last_strength.b_limit * (pct / 100))
        await play_client.client.set_strength(Channel.A, StrengthOperationType.SET_TO, a_val)
        await play_client.client.set_strength(Channel.B, StrengthOperationType.SET_TO, b_val)
        return True

    # ==================== 波形背景轮播 ====================

    def _start_wave_rotation(self, user_id: str, play_client):
        """启动用户的背景波形轮播任务（绑定时调用）"""
        self._stop_wave_rotation(user_id)
        task = asyncio.create_task(self._wave_rotation_task(play_client, user_id))
        self._wave_rotation_tasks[user_id] = task

    def _stop_wave_rotation(self, user_id: str):
        """停止用户的背景波形轮播任务"""
        task = self._wave_rotation_tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()

    async def _wave_rotation_task(self, play_client, user_id: str):
        """
        背景波形轮播：将所有可用波形随机打乱后依次切换，每 10s 一次。
        走完整个列表后重新随机，不向群里发送任何通知。
        惩罚类指令不会修改波形，只控制强度。
        """
        try:
            while not play_client.is_destroyed:
                available = list(custom_pulse_data.keys)
                if not available:
                    await asyncio.sleep(10)
                    continue
                random.shuffle(available)
                for name in available:
                    if play_client.is_destroyed:
                        return
                    data = custom_pulse_data.get(name)
                    if data:
                        play_client.setup_pulse_job([name], data, Channel.A, Channel.B)
                    await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"波形背景轮播任务异常 [{user_id}]: {e}")

    def _start_gameplay_task(self, user_id: str, coro) -> asyncio.Task:
        """启动玩法后台任务，自动取消该用户之前的活跃任务"""
        old = self._active_tasks.get(user_id)
        if old and not old.done():
            old.cancel()
        task = asyncio.create_task(coro)
        self._active_tasks[user_id] = task
        return task

    def _get_shield(self, user_id: str) -> int:
        """获取玩家当前护盾层数（自动补充，每 45 分钟补充 1 层）"""
        now = time.time()
        shield = self._shields.get(user_id)
        if not shield:
            return 0
        elapsed = now - shield["last_recharge"]
        if shield["charges"] < 1 and elapsed >= SHIELD_RECHARGE_SECS:
            shield["charges"] = 1
            shield["last_recharge"] = now
        return shield["charges"]

    def _consume_shield(self, user_id: str) -> bool:
        """随机概率触发护盾（非必然），返回是否触发并扣除一层"""
        charges = self._get_shield(user_id)
        if charges > 0 and random.random() < SHIELD_TRIGGER_PROB:
            self._shields[user_id]["charges"] = charges - 1
            return True
        return False

    def _grant_shield(self, user_id: str):
        """授予玩家护盾（绑定时调用）"""
        self._shields[user_id] = {"charges": 1, "last_recharge": time.time()}

    async def _try_shield_reflect(self, attacker_id: str, victim_id: str,
                                   event_or_umo, play_client, attacker_client,
                                   punishment_coro_factory) -> bool:
        """
        检查被攻击者是否有护盾，如果有则反弹惩罚给攻击者。
        返回 True 表示护盾触发了（已反弹），调用方应终止原始惩罚。
        """
        if not self._consume_shield(victim_id):
            return False
        # 护盾触发！
        umo = event_or_umo if isinstance(event_or_umo, str) else event_or_umo.unified_msg_origin
        if attacker_client and not attacker_client.is_destroyed:
            chain = MessageChain()
            chain.message(
                f"🛡️ 反弹护盾触发！惩罚被反弹给了攻击者！"
            )
            try:
                await self.context.send_message(umo, chain)
            except Exception:
                pass
            self._start_gameplay_task(attacker_id, punishment_coro_factory(attacker_client, attacker_id))
        else:
            chain = MessageChain()
            chain.message("🛡️ 反弹护盾触发！但攻击者未绑定设备，惩罚已抵消！")
            try:
                await self.context.send_message(umo, chain)
            except Exception:
                pass
        return True

    # ==================== 玩法后台任务 ====================

    async def _dice_punishment_task(self, play_client, target_uid: str, umo: str,
                                     strength_range: tuple, duration: int):
        """骰子惩罚后台任务（静默执行，不再发中间消息）"""
        try:
            pct = min(random.randint(strength_range[0], strength_range[1]), MAX_STRENGTH_PCT)
            await self._set_strength_pct(play_client, pct)
            await asyncio.sleep(duration)
            if not play_client.is_destroyed:
                await self._set_strength_pct(play_client, 0)
        except asyncio.CancelledError:
            if not play_client.is_destroyed:
                try:
                    await self._set_strength_pct(play_client, 0)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"骰子惩罚任务异常: {e}")
        finally:
            self._active_tasks.pop(target_uid, None)

    async def _storm_task(self, play_client, target_uid: str, umo: str,
                           duration: float, times: int):
        """
        随机风暴后台任务。
        - 若玩家当前强度为 0，则从小到大递增，逐步趋向 ±30% 的稳定区间。
        - 最终以一条合并消息播报所有步骤。
        - 强度上限 MAX_STRENGTH_PCT。
        """
        interval = duration / max(times, 1)
        step_logs = []
        try:
            # 检测当前强度是否为 0，决定是否使用渐进模式
            current_is_zero = (
                play_client.last_strength is not None and
                play_client.last_strength.a == 0 and
                play_client.last_strength.b == 0
            )

            for i in range(times):
                if play_client.is_destroyed:
                    break

                if current_is_zero:
                    # 渐进模式：从 5% 开始，每步递增，收敛到 [15, 55] 区间
                    progress = (i + 1) / times  # 0~1
                    base = 5 + 50 * progress  # 5% → 55%
                    noise = random.uniform(-15, 15)
                    pct = min(max(base + noise, 5), MAX_STRENGTH_PCT)
                else:
                    pct = min(random.randint(5, 100), MAX_STRENGTH_PCT)

                await self._set_strength_pct(play_client, pct)
                step_logs.append(f"  [{i + 1}/{times}] 强度 {int(pct)}%")
                await asyncio.sleep(interval)

            if not play_client.is_destroyed:
                await self._set_strength_pct(play_client, 0)

            summary = "🌪️ 随机风暴结束！\n" + "\n".join(step_logs) + "\n✅ 强度已归零"
            chain = MessageChain()
            chain.message(summary)
            await self.context.send_message(umo, chain)
        except asyncio.CancelledError:
            if not play_client.is_destroyed:
                try:
                    await self._set_strength_pct(play_client, 0)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"随机风暴任务异常: {e}")
        finally:
            self._active_tasks.pop(target_uid, None)

    async def _gradual_task(self, play_client, target_uid: str, umo: str,
                             duration: float, final_pct: float):
        """渐强惩罚后台任务：强度从 0 线性升至目标百分比（上限 MAX_STRENGTH_PCT）"""
        final_pct = min(final_pct, MAX_STRENGTH_PCT)
        steps = min(int(duration), 20)
        interval = duration / max(steps, 1)
        try:
            for i in range(1, steps + 1):
                if play_client.is_destroyed:
                    break
                current_pct = final_pct * i / steps
                await self._set_strength_pct(play_client, current_pct)
                await asyncio.sleep(interval)

            if not play_client.is_destroyed:
                await asyncio.sleep(3)
                await self._set_strength_pct(play_client, 0)
                chain = MessageChain()
                chain.message(f"📉 渐强惩罚结束！已从 0% 升至 {int(final_pct)}%，强度已归零")
                await self.context.send_message(umo, chain)
        except asyncio.CancelledError:
            if not play_client.is_destroyed:
                try:
                    await self._set_strength_pct(play_client, 0)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"渐强惩罚任务异常: {e}")
        finally:
            self._active_tasks.pop(target_uid, None)

    async def _sanction_task(self, play_client, target_uid: str, umo: str,
                              duration: float):
        """一键制裁后台任务：满功率输出指定时长（绕过 90% 上限）"""
        try:
            await self._set_strength_pct(play_client, 100, bypass_cap=True)

            chain = MessageChain()
            chain.message(f"💀 一键制裁执行中！满功率，持续 {int(duration)} 秒！")
            await self.context.send_message(umo, chain)

            await asyncio.sleep(duration)

            if not play_client.is_destroyed:
                await self._set_strength_pct(play_client, 0, bypass_cap=True)
                chain = MessageChain()
                chain.message("✅ 制裁结束，强度已归零")
                await self.context.send_message(umo, chain)
        except asyncio.CancelledError:
            if not play_client.is_destroyed:
                try:
                    await self._set_strength_pct(play_client, 0, bypass_cap=True)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"一键制裁任务异常: {e}")
    # ==================== 进阶玩法指令 ====================

    @filter.command("郊狼骰子")
    async def dice_play(self, event: AstrMessageEvent):
        """掷骰子决定惩罚等级：/郊狼骰子 @用户"""
        target = _get_at_target(event)
        if not target:
            yield event.plain_result(ReplyText.please_at_target)
            return

        play_client = self.client_manager.user_id_to_client.get(target)
        if not play_client:
            yield event.plain_result(ReplyText.invalid_target)
            return

        roll = random.randint(1, 6)
        outcome = DICE_OUTCOMES[roll - 1]

        result_text = f"🎲 骰子转动中... {outcome['face']} = {roll}！\n"
        result_text += f"【{outcome['name']}】{outcome['desc']}"
        if outcome["strength"]:
            s_lo, s_hi = outcome["strength"]
            s_lo = min(s_lo, int(MAX_STRENGTH_PCT))
            s_hi = min(s_hi, int(MAX_STRENGTH_PCT))
            result_text += f"\n⚡ 强度 {s_lo}~{s_hi}%，持续 {outcome['duration']} 秒"

        # 处理命运逆转：惩罚反弹给施法者
        actual_target = target
        actual_client = play_client
        if outcome.get("reverse"):
            sender_id = event.get_sender_id()
            sender_client = self.client_manager.user_id_to_client.get(sender_id)
            if sender_client and sender_id != target:
                actual_target = sender_id
                actual_client = sender_client
                result_text += "\n⚠️ 惩罚反弹！施法者将承受惩罚！"
            else:
                result_text += "\n⚠️ 施法者未绑定设备，惩罚加倍施加于目标！"
                outcome = dict(outcome)
                outcome["strength"] = (80, 100)
                outcome["duration"] = 45

        yield event.plain_result(result_text)

        if outcome["strength"]:
            # 护盾检查
            sender_id = event.get_sender_id()
            sender_client = self.client_manager.user_id_to_client.get(sender_id)
            reflected = await self._try_shield_reflect(
                sender_id, actual_target, event.unified_msg_origin,
                actual_client, sender_client,
                lambda sc, sid: self._dice_punishment_task(
                    sc, sid, event.unified_msg_origin,
                    outcome["strength"], outcome["duration"]
                )
            )
            if not reflected:
                self._start_gameplay_task(
                    actual_target,
                    self._dice_punishment_task(
                        actual_client, actual_target, event.unified_msg_origin,
                        outcome["strength"], outcome["duration"]
                    )
                )

    @filter.command("随机风暴")
    async def random_storm(self, event: AstrMessageEvent):
        """在指定时间内随机变化多次强度和波形：/随机风暴 @用户 [时长秒] [次数]"""
        target = _get_at_target(event)
        if not target:
            yield event.plain_result(ReplyText.please_at_target)
            return

        play_client = self.client_manager.user_id_to_client.get(target)
        if not play_client:
            yield event.plain_result(ReplyText.invalid_target)
            return

        nums = _parse_numbers_from_text(event)
        duration = nums[0] if len(nums) > 0 else 30.0
        times = int(nums[1]) if len(nums) > 1 else 5

        duration = max(5, min(duration, 300))
        times = max(1, min(times, 50))

        # 护盾检查
        sender_id = event.get_sender_id()
        sender_client = self.client_manager.user_id_to_client.get(sender_id)
        reflected = await self._try_shield_reflect(
            sender_id, target, event.unified_msg_origin,
            play_client, sender_client,
            lambda sc, sid: self._storm_task(sc, sid, event.unified_msg_origin, duration, times)
        )
        if not reflected:
            self._start_gameplay_task(
                target,
                self._storm_task(play_client, target, event.unified_msg_origin, duration, times)
            )
            yield event.plain_result(f"🌪️ 已对目标发起随机风暴：{int(duration)} 秒内变化 {times} 次")

    @filter.command("渐强惩罚")
    async def gradual_punishment(self, event: AstrMessageEvent):
        """强度从零逐步攀升到指定百分比：/渐强惩罚 @用户 [时长秒] [最终百分比]"""
        target = _get_at_target(event)
        if not target:
            yield event.plain_result(ReplyText.please_at_target)
            return

        play_client = self.client_manager.user_id_to_client.get(target)
        if not play_client:
            yield event.plain_result(ReplyText.invalid_target)
            return

        nums = _parse_numbers_from_text(event)
        duration = nums[0] if len(nums) > 0 else 30.0
        final_pct = nums[1] if len(nums) > 1 else 80.0

        duration = max(5, min(duration, 300))
        final_pct = max(1, min(final_pct, 100))

        # 护盾检查
        sender_id = event.get_sender_id()
        sender_client = self.client_manager.user_id_to_client.get(sender_id)
        reflected = await self._try_shield_reflect(
            sender_id, target, event.unified_msg_origin,
            play_client, sender_client,
            lambda sc, sid: self._gradual_task(sc, sid, event.unified_msg_origin, duration, final_pct)
        )
        if not reflected:
            self._start_gameplay_task(
                target,
                self._gradual_task(play_client, target, event.unified_msg_origin, duration, final_pct)
            )
            yield event.plain_result(
                f"📈 已对目标发起渐强惩罚：{int(duration)} 秒内升至 {int(final_pct)}%"
            )

    @filter.command("一键制裁")
    async def instant_sanction(self, event: AstrMessageEvent):
        """满功率制裁目标玩家（每人每小时发起 3 次，被控者每小时免审 1 次）：/一键制裁 @用户 [时长秒]"""
        target = _get_at_target(event)
        if not target:
            yield event.plain_result(ReplyText.please_at_target)
            return

        play_client = self.client_manager.user_id_to_client.get(target)
        if not play_client:
            yield event.plain_result(ReplyText.invalid_target)
            return

        sender_id = event.get_sender_id()
        now = time.time()

        # 清理超过 1 小时的记录
        one_hour_ago = now - 3600
        self._sanction_attacker_log[sender_id] = [
            t for t in self._sanction_attacker_log[sender_id] if t > one_hour_ago
        ]
        self._sanction_victim_log[target] = [
            t for t in self._sanction_victim_log[target] if t > one_hour_ago
        ]

        # 检查施法者限制：每小时最多 3 次
        if len(self._sanction_attacker_log[sender_id]) >= 3:
            remaining = int(self._sanction_attacker_log[sender_id][0] + 3600 - now)
            remaining_min = remaining // 60
            remaining_sec = remaining % 60
            yield event.plain_result(
                f"⚠️ 你在 1 小时内已发起 3 次制裁，已达上限\n"
                f"下次可用时间：{remaining_min} 分 {remaining_sec} 秒后"
            )
            return

        nums = _parse_numbers_from_text(event)
        duration = nums[0] if len(nums) > 0 else 15.0
        duration = max(3, min(duration, 120))

        # 检查被控者限制：每小时免审 1 次，超过需要审批
        if len(self._sanction_victim_log[target]) >= 1:
            # 需要被控者批准
            self._pending_sanctions[target] = (sender_id, play_client, event.unified_msg_origin, duration)
            yield event.chain_result([
                At(qq=target),
                Plain(
                    f" 你在 1 小时内已被制裁过，本次需要你的批准\n"
                    f"发起人：{event.get_sender_name()}\n"
                    f"制裁时长：{int(duration)} 秒\n"
                    f"请发送 /批准制裁 同意，或忽略拒绝（30 秒后自动过期）"
                )
            ])
            # 30 秒后自动过期
            asyncio.create_task(self._sanction_approval_timeout(target, event.unified_msg_origin))
            return

        # 免审执行
        self._sanction_attacker_log[sender_id].append(now)
        self._sanction_victim_log[target].append(now)

        # 护盾检查
        sender_client = self.client_manager.user_id_to_client.get(sender_id)
        reflected = await self._try_shield_reflect(
            sender_id, target, event.unified_msg_origin,
            play_client, sender_client,
            lambda sc, sid: self._sanction_task(sc, sid, event.unified_msg_origin, duration)
        )
        if not reflected:
            self._start_gameplay_task(
                target,
                self._sanction_task(play_client, target, event.unified_msg_origin, duration)
            )
            yield event.plain_result(f"💀 已对目标发起一键制裁：满功率持续 {int(duration)} 秒！")

    async def _sanction_approval_timeout(self, target: str, umo: str):
        """制裁审批超时自动过期"""
        await asyncio.sleep(30)
        if target in self._pending_sanctions:
            self._pending_sanctions.pop(target, None)
            try:
                chain = MessageChain()
                chain.message("⏰ 制裁请求已过期，未获得批准")
                await self.context.send_message(umo, chain)
            except Exception:
                pass

    @filter.command("批准制裁")
    async def approve_sanction(self, event: AstrMessageEvent):
        """被控者批准制裁请求：/批准制裁"""
        user_id = event.get_sender_id()
        pending = self._pending_sanctions.pop(user_id, None)
        if not pending:
            yield event.plain_result("当前没有待审批的制裁请求")
            return

        attacker_id, play_client, umo, duration = pending
        now = time.time()

        # 记录操作日志
        self._sanction_attacker_log[attacker_id].append(now)
        self._sanction_victim_log[user_id].append(now)

        self._start_gameplay_task(
            user_id,
            self._sanction_task(play_client, user_id, umo, duration)
        )
        yield event.plain_result(f"✅ 已批准制裁！满功率持续 {int(duration)} 秒！")

    @filter.command("郊狼轮盘")
    async def roulette(self, event: AstrMessageEvent):
        """随机惩罚一名在线玩家：/郊狼轮盘"""
        players = list(self.client_manager.user_id_to_client.items())
        if not players:
            yield event.plain_result(ReplyText.no_player)
            return

        target_uid, play_client = random.choice(players)
        pct = min(random.randint(20, 100), MAX_STRENGTH_PCT)
        duration = random.randint(10, 60)

        # 护盾检查
        sender_id = event.get_sender_id()
        sender_client = self.client_manager.user_id_to_client.get(sender_id)
        reflected = await self._try_shield_reflect(
            sender_id, target_uid, event.unified_msg_origin,
            play_client, sender_client,
            lambda sc, sid: self._dice_punishment_task(sc, sid, event.unified_msg_origin, (pct, pct), duration)
        )
        if reflected:
            return

        yield event.chain_result([
            Plain("🎰 郊狼轮盘转动中...\n🎯 命运选中了 "),
            At(qq=target_uid),
            Plain(f" ！\n⚡ 随机强度 {pct}%，持续 {duration} 秒！")
        ])

        self._start_gameplay_task(
            target_uid,
            self._dice_punishment_task(play_client, target_uid, event.unified_msg_origin, (pct, pct), duration)
        )

    @filter.command("停止任务")
    async def stop_gameplay_task(self, event: AstrMessageEvent):
        """停止某玩家进行中的玩法任务：/停止任务 @用户"""
        target = _get_at_target(event)
        if not target:
            target = event.get_sender_id()

        task = self._active_tasks.get(target)
        if task and not task.done():
            task.cancel()
            self._active_tasks.pop(target, None)
            yield event.plain_result("⛔ 已停止该玩家的玩法任务，强度已归零")
        else:
            yield event.plain_result("当前没有正在进行的玩法任务")

    # ==================== .pulse 波形文件导入 ====================

    @filter.command("导入波形")
    async def import_pulse(self, event: AstrMessageEvent):
        """
        导入 DungeonLab .pulse 波形文本：/导入波形 <波形名称>
        然后在下一行（或同一条消息内）粘贴 Dungeonlab+pulse:... 内容。
        也可以直接将整段内容作为命令参数。
        """
        text = event.message_str.strip() if hasattr(event, 'message_str') else ""
        # 去除命令前缀
        for prefix in ["/导入波形", "导入波形"]:
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break

        # 尝试从文本中找到 pulse 内容
        pulse_start = text.find("Dungeonlab+pulse")
        if pulse_start == -1:
            yield event.plain_result(
                "❌ 未找到有效的 .pulse 内容。\n"
                "用法：/导入波形 <名称>\n"
                "Dungeonlab+pulse:...\n\n"
                "或发送以名称作为第一行、pulse 内容作为第二行的消息。"
            )
            return

        lines = text[:pulse_start].strip().splitlines()
        wave_name = lines[-1].strip() if lines else ""
        pulse_content = text[pulse_start:].strip()

        if not wave_name:
            yield event.plain_result("❌ 请在 pulse 内容前一行提供波形名称")
            return

        pulses = parse_dungeonlab_pulse(pulse_content)
        if not pulses:
            yield event.plain_result(f"❌ 波形解析结果为空，请确认 .pulse 格式正确（以 Dungeonlab+pulse: 开头）")
            return

        # 保存 .pulse 文件
        if self._pulses_dir:
            safe_name = "".join(c for c in wave_name if c.isalnum() or c in "-_()[]（）【】 ").strip()
            if not safe_name:
                safe_name = f"custom_{int(time.time())}"
            file_path = self._pulses_dir / f"{safe_name}.pulse"
            try:
                file_path.write_text(pulse_content, encoding="utf-8")
            except Exception as e:
                logger.warning(f"保存 .pulse 文件失败: {e}")

        custom_pulse_data.data[wave_name] = list(pulses)
        yield event.plain_result(
            f"✅ 已导入波形【{wave_name}】，共 {len(pulses)} 组，已加入轮播列表"
        )

    @filter.on_message()
    async def handle_group_pulse_file(self, event: AstrMessageEvent):
        """监听群消息中的文件组件，若是 .pulse 文件则自动解析"""
        try:
            from astrbot.api.message_components import File  # type: ignore
        except ImportError:
            return

        for comp in event.get_messages():
            if not isinstance(comp, File):  # type: ignore
                continue
            name = getattr(comp, 'name', '') or getattr(comp, 'filename', '') or ''
            if not name.lower().endswith('.pulse'):
                continue

            url = getattr(comp, 'url', '') or getattr(comp, 'file', '')
            if not url:
                continue

            # 尝试下载并解析
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status != 200:
                            continue
                        content = await resp.text(encoding='utf-8', errors='replace')
            except Exception as e:
                logger.warning(f"下载 .pulse 文件失败: {name} - {e}")
                continue

            pulses = parse_dungeonlab_pulse(content)
            if not pulses:
                await self.context.send_message(
                    event.unified_msg_origin,
                    MessageChain().message(f"⚠️ 文件 {name} 解析结果为空，已跳过")
                )
                continue

            wave_name = name[:-6]  # 去掉 .pulse 后缀
            custom_pulse_data.data[wave_name] = list(pulses)

            if self._pulses_dir:
                try:
                    (self._pulses_dir / name).write_text(content, encoding="utf-8")
                except Exception as e:
                    logger.warning(f"保存群文件 .pulse 失败: {e}")

            chain = MessageChain()
            chain.message(f"✅ 已自动解析群文件波形【{wave_name}】，共 {len(pulses)} 组，已加入轮播列表")
            await self.context.send_message(event.unified_msg_origin, chain)

    # ==================== 护盾查询 ====================

    @filter.command("护盾状态")
    async def shield_status(self, event: AstrMessageEvent):
        """查看自己的反弹护盾状态：/护盾状态"""
        user_id = event.get_sender_id()
        charges = self._get_shield(user_id)
        if charges > 0:
            yield event.plain_result(
                f"🛡️ 你当前拥有 {charges} 层反弹护盾\n"
                f"（触发概率 {int(SHIELD_TRIGGER_PROB * 100)}%，每 45 分钟补充 1 层）"
            )
        else:
            shield = self._shields.get(user_id)
            if shield:
                elapsed = time.time() - shield["last_recharge"]
                remaining = max(0, int(SHIELD_RECHARGE_SECS - elapsed))
                yield event.plain_result(
                    f"🛡️ 护盾已耗尽，{remaining // 60} 分 {remaining % 60} 秒后自动补充"
                )
            else:
                yield event.plain_result("🛡️ 你还没有绑定郊狼，绑定后自动获得护盾")

    # ==================== 郊狼决斗 ====================

    @filter.command("郊狼决斗")
    async def duel(self, event: AstrMessageEvent):
        """双方掷骰子，点数小的受罚：/郊狼决斗 @用户"""
        target = _get_at_target(event)
        if not target:
            yield event.plain_result(ReplyText.please_at_target)
            return

        sender_id = event.get_sender_id()
        if sender_id == target:
            yield event.plain_result("不能和自己决斗哦~")
            return

        target_client = self.client_manager.user_id_to_client.get(target)
        sender_client = self.client_manager.user_id_to_client.get(sender_id)

        if not target_client:
            yield event.plain_result(ReplyText.invalid_target)
            return

        roll_a = random.randint(1, 6)
        roll_b = random.randint(1, 6)
        diff = abs(roll_a - roll_b)

        result_text = (
            f"⚔️ 郊狼决斗开始！\n"
            f"🎲 {event.get_sender_name()} 掷出了 {roll_a} 点\n"
            f"🎲 对手 掷出了 {roll_b} 点\n"
        )

        if roll_a == roll_b:
            # 平局：双方都受轻微惩罚
            pct = 15
            duration = 8
            result_text += f"\n⚔️ 平局！双方各受 {pct}% 强度惩罚 {duration} 秒！"
            yield event.plain_result(result_text)
            if sender_client and not sender_client.is_destroyed:
                self._start_gameplay_task(
                    sender_id,
                    self._dice_punishment_task(sender_client, sender_id, event.unified_msg_origin, (pct, pct), duration)
                )
            if not target_client.is_destroyed:
                self._start_gameplay_task(
                    target,
                    self._dice_punishment_task(target_client, target, event.unified_msg_origin, (pct, pct), duration)
                )
        else:
            pct = min(diff * 20, 100)
            duration = 10 + diff * 5
            if roll_a > roll_b:
                loser_id = target
                loser_client = target_client
                result_text += f"\n💥 对手败北！受到 {pct}% 惩罚，持续 {duration} 秒！"
            else:
                loser_id = sender_id
                loser_client = sender_client
                result_text += f"\n💥 {event.get_sender_name()} 败北！受到 {pct}% 惩罚，持续 {duration} 秒！"

            yield event.plain_result(result_text)

            if loser_client and not loser_client.is_destroyed:
                self._start_gameplay_task(
                    loser_id,
                    self._dice_punishment_task(loser_client, loser_id, event.unified_msg_origin, (pct, pct), duration)
                )
            elif not loser_client:
                yield event.plain_result("败方未绑定设备，惩罚无法执行")

    # ==================== 猜数字 ====================

    @filter.command("猜数字")
    async def guess_number(self, event: AstrMessageEvent):
        """猜数字玩法，5 次机会猜 1-100：/猜数字 @用户"""
        target = _get_at_target(event)
        if not target:
            yield event.plain_result(ReplyText.please_at_target)
            return

        target_client = self.client_manager.user_id_to_client.get(target)
        if not target_client:
            yield event.plain_result(ReplyText.invalid_target)
            return

        if target in self._guess_games:
            yield event.plain_result("该玩家已有一场猜数字游戏进行中")
            return

        answer = random.randint(1, 100)
        self._guess_games[target] = {
            "answer": answer,
            "attacker": event.get_sender_id(),
            "remaining": 5,
            "low": 1,
            "high": 100,
            "umo": event.unified_msg_origin
        }

        yield event.chain_result([
            At(qq=target),
            Plain(
                f" 🔢 猜数字游戏开始！\n"
                f"系统已生成 1-100 的随机数，你有 5 次机会\n"
                f"每次猜错只提示大了还是小了，并受到惩罚\n"
                f"偏差 ±5 以内算猜对，猜对则发起者受罚！\n"
                f"请发送 /猜 <数字> 进行猜测"
            )
        ])

    @filter.command("猜")
    async def guess_answer(self, event: AstrMessageEvent):
        """猜数字回答：/猜 <数字>"""
        user_id = event.get_sender_id()
        game = self._guess_games.get(user_id)
        if not game:
            yield event.plain_result("你当前没有进行中的猜数字游戏")
            return

        nums = _parse_numbers_from_text(event)
        if not nums:
            yield event.plain_result("请输入一个数字，例如：/猜 50")
            return

        guess = int(nums[0])
        answer = game["answer"]
        game["remaining"] -= 1
        remaining = game["remaining"]

        target_client = self.client_manager.user_id_to_client.get(user_id)

        diff = abs(guess - answer)

        if diff <= 5:
            # ±5 以内算猜对！惩罚发起者
            self._guess_games.pop(user_id, None)
            attacker_id = game["attacker"]
            attacker_client = self.client_manager.user_id_to_client.get(attacker_id)

            yield event.plain_result(
                f"🎉 恭喜！答案是 {answer}，你猜了 {guess}，偏差仅 {diff}，算你猜对！\n"
                f"发起者将受到惩罚！"
            )
            if attacker_client and not attacker_client.is_destroyed:
                self._start_gameplay_task(
                    attacker_id,
                    self._dice_punishment_task(attacker_client, attacker_id, game["umo"], (50, 80), 20)
                )
            return

        # 只提示方向，不暴露偏差
        if guess < answer:
            hint = "⬆️ 小了"
        else:
            hint = "⬇️ 大了"

        # 固定惩罚，避免通过体感推断偏差
        pct = 40
        duration = 8

        if remaining > 0:
            result_text = f"{hint}！剩余 {remaining} 次机会"
        else:
            result_text = f"{hint}！机会已用完，答案是 {answer}，最终惩罚！"
            self._guess_games.pop(user_id, None)
            pct = 80
            duration = 15

        yield event.plain_result(result_text)

        # 执行惩罚
        if target_client and not target_client.is_destroyed:
            self._start_gameplay_task(
                user_id,
                self._dice_punishment_task(target_client, user_id, game.get("umo", event.unified_msg_origin), (pct, pct), duration)
            )

    # ==================== 接力惩罚 ====================

    @filter.command("接力惩罚")
    async def relay_punishment(self, event: AstrMessageEvent):
        """依次传递递增惩罚：/接力惩罚 [起始强度%]"""
        players = list(self.client_manager.user_id_to_client.items())
        if len(players) < 2:
            yield event.plain_result("至少需要 2 名在线玩家才能进行接力惩罚")
            return

        nums = _parse_numbers_from_text(event)
        base_pct = nums[0] if len(nums) > 0 else 10.0
        base_pct = max(5, min(base_pct, 50))
        increment = 10

        random.shuffle(players)
        umo = event.unified_msg_origin

        order_text = "🔗 接力惩罚开始！顺序：\n"
        for i, (uid, _) in enumerate(players):
            pct = min(base_pct + i * increment, 100)
            order_text += f"  {i + 1}. 强度 {int(pct)}%\n"
        yield event.plain_result(order_text)

        self._start_gameplay_task(
            "relay_" + str(id(players)),
            self._relay_task(players, base_pct, increment, umo)
        )

    async def _relay_task(self, players: list, base_pct: float, increment: float, umo: str):
        """接力惩罚后台任务（不改变波形，只控制强度）"""
        try:
            for i, (uid, play_client) in enumerate(players):
                if play_client.is_destroyed:
                    continue
                pct = min(base_pct + i * increment, MAX_STRENGTH_PCT)
                duration = 8 + i * 3

                await self._set_strength_pct(play_client, pct)

                chain = MessageChain()
                chain.message(
                    f"🔗 接力 [{i + 1}/{len(players)}]：强度 {int(pct)}%，持续 {duration} 秒"
                )
                await self.context.send_message(umo, chain)

                await asyncio.sleep(duration)

                if not play_client.is_destroyed:
                    await self._set_strength_pct(play_client, 0)

                await asyncio.sleep(2)

            chain = MessageChain()
            chain.message("✅ 接力惩罚全部结束！")
            await self.context.send_message(umo, chain)
        except asyncio.CancelledError:
            for _, play_client in players:
                if not play_client.is_destroyed:
                    try:
                        await self._set_strength_pct(play_client, 0)
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"接力惩罚任务异常: {e}")

    # ==================== 自动通过好友 ====================

    @filter.on_astrbot_loaded()
    async def setup_auto_friend_accept(self):
        """在 AstrBot 加载完成后设置自动通过好友请求（仅 aiocqhttp 平台）"""
        if not self.config.get("auto_accept_friend", True):
            logger.info("自动通过好友功能已关闭")
            return
        try:
            from astrbot.api.platform import AiocqhttpAdapter
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            if platform and isinstance(platform, AiocqhttpAdapter):
                client = platform.get_client()
                if client:
                    @client.on('request')
                    async def _handle_request(payload):
                        if payload.get('request_type') == 'friend':
                            try:
                                await client.api.call_action(
                                    'set_friend_add_request',
                                    flag=payload.get('flag', ''),
                                    approve=True
                                )
                                logger.info(f"已自动通过好友请求: user_id={payload.get('user_id')}")
                            except Exception as e:
                                logger.error(f"自动通过好友请求失败: {e}")
                    logger.info("自动通过好友功能已启用")
                else:
                    logger.warning("自动通过好友功能：无法获取 aiocqhttp 客户端")
            else:
                logger.info("自动通过好友功能：当前平台非 aiocqhttp，跳过")
        except ImportError:
            logger.info("自动通过好友功能：aiocqhttp 适配器未安装，跳过")
        except Exception as e:
            logger.warning(f"自动通过好友功能初始化失败（不影响其他功能）: {e}")

    # ==================== 生命周期 ====================

    async def terminate(self):
        """插件销毁，关闭所有连接"""
        for task in self._active_tasks.values():
            if not task.done():
                task.cancel()
        self._active_tasks.clear()
        for task in self._wave_rotation_tasks.values():
            if not task.done():
                task.cancel()
        self._wave_rotation_tasks.clear()
        if self.client_manager:
            await self.client_manager.shutdown()
        logger.info("DG-Lab-Play 插件已关闭")
