"""
DG-Lab 客户端管理器

移植自 nonebot-plugin-dg-lab-play (https://github.com/Ljzd-PRO/nonebot-plugin-dg-lab-play)
原项目使用 BSD 3-Clause 许可证, Copyright © 2024 by Ljzd-PRO.
"""

import asyncio
import ssl
from functools import cached_property
from typing import Dict, Optional, Union, Callable, Any, List, Tuple
from pathlib import Path

from pydglab_ws import (
    DGLabClient, DGLabWSServer, StrengthData, FeedbackButton,
    DGLabWSConnect, RetCode, DGLabWSClient, PulseOperation,
    Channel, PulseDataTooLong
)

__all__ = ["DGLabPlayClient", "ClientManager"]

APP_PULSE_QUEUE_LEN = 50
"""DG-Lab App 波形队列最大持续时长"""


class DGLabPlayConfig:
    """配置容器，从 AstrBot 配置字典构建"""

    def __init__(self, config: dict):
        # WebSocket 服务端设置
        self.remote_server: bool = config.get("remote_server", False)
        self.remote_server_uri: Optional[str] = config.get("remote_server_uri", None)
        self.local_server_host: str = config.get("local_server_host", "0.0.0.0")
        self.local_server_port: int = config.get("local_server_port", 4567)
        self.local_server_publish_uri: str = config.get("local_server_publish_uri", "ws://127.0.0.1:4567")
        self.local_server_heartbeat_interval: Optional[float] = config.get("local_server_heartbeat_interval", None)

        # SSL 设置
        self.local_server_secure: bool = config.get("local_server_secure", False)
        self.local_server_ssl_cert: Optional[str] = config.get("local_server_ssl_cert", None)
        self.local_server_ssl_key: Optional[str] = config.get("local_server_ssl_key", None)
        self.local_server_ssl_password: Optional[str] = config.get("local_server_ssl_password", None)

        # 终端设置
        self.bind_timeout: float = config.get("bind_timeout", 90)
        self.register_timeout: float = config.get("register_timeout", 30)

        # 波形数据设置
        self.duration_per_post: float = config.get("duration_per_post", 5.0)
        self.post_interval: float = config.get("post_interval", 1.0)
        self.sleep_after_clear: float = config.get("sleep_after_clear", 0.5)

    @cached_property
    def server_ssl_context(self) -> Optional[ssl.SSLContext]:
        if self.local_server_secure and self.local_server_ssl_cert:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            try:
                context.load_cert_chain(
                    certfile=self.local_server_ssl_cert,
                    keyfile=self.local_server_ssl_key,
                    password=self.local_server_ssl_password
                )
                return context
            except ssl.SSLError:
                return None
        return None

    @property
    def ws_uri_for_qrcode(self) -> str:
        """QR 码中使用的 WS URI"""
        if self.remote_server:
            return self.remote_server_uri
        return self.local_server_publish_uri


class DGLabPlayClient:
    """
    单个终端的连接管理器

    :param user_id: 用户 ID
    :param config: 配置对象
    :param destroy_callback: 销毁时的回调函数
    :param client: pydglab-ws 的终端对象
    :param logger: 日志记录器
    """

    def __init__(
        self,
        user_id: str,
        config: DGLabPlayConfig,
        destroy_callback: Callable,
        logger,
        client: DGLabClient = None
    ):
        self.user_id = user_id
        self.config = config
        self.client: Optional[DGLabClient] = client
        self._destroy_callback = destroy_callback
        self._logger = logger
        self.last_strength: Optional[StrengthData] = None
        self.last_feedback: Optional[FeedbackButton] = None
        self.fetch_task: Optional[asyncio.Task] = None
        self._pulse_name_data: Tuple[List[str], List[PulseOperation]] = ([], [])
        self.pulse_task: Optional[asyncio.Task] = None
        self.is_destroyed: bool = False

        self.register_finished_lock = asyncio.Lock()
        self.bind_finished_lock = asyncio.Lock()

    async def __aenter__(self):
        for lock in self.register_finished_lock, self.bind_finished_lock:
            await lock.acquire()
        self.fetch_task = asyncio.create_task(self._serve())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return

    @cached_property
    def qrcode(self) -> Optional[str]:
        return self.client.get_qrcode(self.config.ws_uri_for_qrcode)

    @property
    def pulse_names(self) -> List[str]:
        return self._pulse_name_data[0]

    @property
    def pulse_data(self) -> List[PulseOperation]:
        return self._pulse_name_data[1]

    async def destroy(self):
        """断开终端的 WS 连接，调用回调函数，并解锁等待锁，以及取消消息获取的任务"""
        self.is_destroyed = True
        if self.client and isinstance(self.client, DGLabWSClient):
            await self.client.websocket.close()
        self._destroy_callback(self)
        for lock in self.register_finished_lock, self.bind_finished_lock:
            if lock.locked():
                lock.release()
        if self.fetch_task:
            self.fetch_task.cancel()
        if self.pulse_task and not self.pulse_task.cancelled() and not self.pulse_task.done():
            self.pulse_task.cancel()
        self._logger.info(f"已结束并摧毁 {self.user_id} 的终端")

    async def wait_for_bind(self, rebind: bool = False) -> bool:
        """等待绑定，返回是否绑定成功（非超时）"""
        try:
            await asyncio.wait_for(
                self.client.bind() if not rebind else self.client.rebind(),
                timeout=self.config.bind_timeout
            )
            return True
        except asyncio.TimeoutError:
            await self.destroy()
            return False
        finally:
            if self.bind_finished_lock.locked():
                self.bind_finished_lock.release()

    def setup_pulse_job(self, pulse_names: List[str], pulse_data: List[PulseOperation], *channels: Channel):
        """设置波形发送任务"""
        names, data = self._pulse_name_data
        for current, new in zip((names, data), (pulse_names, pulse_data)):
            current.clear()
            current.extend(new)
        if self.pulse_task and not self.pulse_task.cancelled() and not self.pulse_task.done():
            self.pulse_task.cancel()
        self.pulse_task = asyncio.create_task(
            self._pulse_job(pulse_data, *channels)
        )
        self._logger.info(f"已为用户 {self.user_id} 设置波形任务，波形长度 {len(pulse_data)}")

    async def _handle_data(self, data: Union[StrengthData, FeedbackButton, RetCode]):
        """处理消息"""
        if isinstance(data, StrengthData):
            self.last_strength = data
        elif isinstance(data, FeedbackButton):
            self.last_feedback = data
        elif data == RetCode.CLIENT_DISCONNECTED:
            self._logger.info(f"终端 {self.client.client_id} 绑定的 App 已断开")
            async with self.bind_finished_lock:
                await self.wait_for_bind(rebind=True)

    async def _serve(self):
        """建立终端连接，并不断获取和处理消息"""
        try:
            if self.config.remote_server:
                try:
                    async with DGLabWSConnect(
                        self.config.remote_server_uri,
                        self.config.register_timeout
                    ) as client:
                        self.client = client
                        self.register_finished_lock.release()
                        self._logger.info(f"终端 {client.client_id} 成功注册")
                        if not await self.wait_for_bind():
                            self._logger.warning(f"终端 {client.client_id} 等待绑定超时")
                            return
                        self._logger.info(f"终端 {client.client_id} 成功与 App {client.target_id} 绑定")
                        async for data in client.data_generator():
                            await self._handle_data(data)
                except asyncio.TimeoutError:
                    self._logger.error(f"终端从 {self.config.remote_server_uri} 获取 clientId 超时")
                    await self.destroy()
                    return
            else:
                self.register_finished_lock.release()
                if not await self.wait_for_bind():
                    self._logger.warning(f"终端 {self.client.client_id} 等待绑定超时")
                    return
                self._logger.info(f"终端 {self.client.client_id} 成功与 App {self.client.target_id} 绑定")
                async for data in self.client.data_generator():
                    await self._handle_data(data)
        except Exception:
            self._logger.error("终端连接出现异常，已退出")

    async def _pulse_job(self, pulse_data: List[PulseOperation], *channels: Channel):
        try:
            for channel in channels:
                await self.client.clear_pulses(channel)
            await asyncio.sleep(self.config.sleep_after_clear)

            pulse_data_duration = len(pulse_data) * 0.1
            replay_times = int(self.config.duration_per_post // pulse_data_duration)
            actual_duration = replay_times * pulse_data_duration
            max_pulse_num = int(APP_PULSE_QUEUE_LEN // actual_duration)
            pulse_data_for_post = pulse_data * replay_times

            try:
                for _ in range(max_pulse_num):
                    for channel in channels:
                        await self.client.add_pulses(channel, *pulse_data_for_post)
                    await asyncio.sleep(self.config.post_interval)

                # 减去上面多余的睡眠时间
                await asyncio.sleep(abs(pulse_data_duration - self.config.post_interval))
                while True:
                    for channel in channels:
                        await self.client.add_pulses(channel, *pulse_data_for_post)
                    await asyncio.sleep(pulse_data_duration)
            except PulseDataTooLong:
                self._logger.error(f"发送的波形数据过长 {self.config.duration_per_post}s，发送失败")
        except Exception:
            self._logger.error("波形发送任务出现异常，已退出")


class ClientManager:
    """客户端管理器"""

    def __init__(self, config: DGLabPlayConfig, logger):
        self.config = config
        self._logger = logger
        self.user_id_to_client: Dict[str, DGLabPlayClient] = {}
        self.ws_server: Optional[DGLabWSServer] = None
        self.ws_server_task: Optional[asyncio.Task] = None

    async def _setup_server(self):
        try:
            if not self.config.remote_server:
                async with DGLabWSServer(
                    self.config.local_server_host,
                    self.config.local_server_port,
                    self.config.local_server_heartbeat_interval,
                    ssl=self.config.server_ssl_context
                ) as server:
                    self.ws_server = server
                    self._logger.info(
                        f"已在 {self.config.local_server_host}:{self.config.local_server_port}"
                        f" 上启动 DG-Lab WebSocket 服务端"
                    )
                    self._logger.info(f"DG-Lab App 将通过 {self.config.local_server_publish_uri} 连接服务端")
                    await asyncio.Future()  # 永远挂起
            else:
                self._logger.info(f"DG-Lab App 将通过 {self.config.remote_server_uri} 连接服务端")
        except Exception as e:
            self._logger.error(f"运行 DG-Lab WebSocket 服务端时出现异常: {e}")

    def serve(self):
        self.ws_server_task = asyncio.create_task(self._setup_server())

    async def new_client(self, user_id: str) -> Optional[DGLabPlayClient]:
        if not self.config.remote_server:
            if self.ws_server:
                async with DGLabPlayClient(
                    user_id,
                    self.config,
                    lambda x: self.user_id_to_client.pop(x.user_id, None),
                    self._logger,
                    self.ws_server.new_local_client()
                ) as play_client:
                    pass
                self.user_id_to_client[user_id] = play_client
                self._logger.info(f"用户 {user_id} 创建了本地终端")
                return play_client
            else:
                return None
        else:
            async with DGLabPlayClient(
                user_id,
                self.config,
                lambda x: self.user_id_to_client.pop(x.user_id, None),
                self._logger
            ) as play_client:
                pass
            async with play_client.register_finished_lock:
                pass
            self.user_id_to_client[user_id] = play_client
            self._logger.info(f"用户 {user_id} 创建了远程终端")
            return play_client

    async def shutdown(self):
        """关闭所有客户端和服务器"""
        for play_client in list(self.user_id_to_client.values()):
            try:
                await play_client.destroy()
            except Exception:
                pass
        if self.ws_server_task and not self.ws_server_task.done():
            self.ws_server_task.cancel()
