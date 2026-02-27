import asyncio
import aiohttp
import re
import os
import json
import datetime
import struct
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.event.filter import EventMessageType, event_message_type
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Node, Plain
from astrbot.api.star import StarTools

# --- 常量定义 ---
RCON_TIMEOUT = 5.0
HEALTH_CHECK_RETRY_INTERVAL = 180  
SAVE_DELAY = 5                     
API_TIMEOUT_TOTAL = 45             
API_TIMEOUT_CONN = 15              
RETRY_DELAY = 5                    

class AsyncRCON:
    """轻量级纯异步 Source RCON 客户端"""
    def __init__(self, host: str, port: int, password: str):
        self.host = host
        self.port = port
        self.password = password
        self.reader = None
        self.writer = None
        self._packet_id_counter = 0

    def _next_packet_id(self) -> int:
        self._packet_id_counter += 1
        return self._packet_id_counter

    async def connect(self):
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=RCON_TIMEOUT
        )
        auth_id = self._next_packet_id()
        await self._send(3, self.password, auth_id) 
        
        while True:
            resp = await self._read()
            if resp.get('id') == -1:
                raise ValueError("RCON 认证失败：密码错误或被拒绝")
            if resp.get('type') == 2 and resp.get('id') == auth_id: 
                break

    async def execute(self, command: str) -> str:
        req_id = self._next_packet_id()
        await self._send(2, command, req_id) 
        
        response_text = ""
        try:
            while True:
                resp = await asyncio.wait_for(self._read(), timeout=RCON_TIMEOUT)
                if resp.get('id') == req_id:
                    response_text += resp.get('body', '')
                    break  
        except asyncio.TimeoutError:
            pass 
            
        return response_text

    async def _send(self, packet_type: int, body: str, packet_id: int):
        body_encoded = body.encode('utf-8')
        packet_size = 10 + len(body_encoded)
        packet = struct.pack('<iii', packet_size, packet_id, packet_type) + body_encoded + b'\x00\x00'
        self.writer.write(packet)
        await self.writer.drain()

    async def _read(self):
        size_data = await asyncio.wait_for(self.reader.readexactly(4), timeout=RCON_TIMEOUT)
        size = struct.unpack('<i', size_data)[0]
        packet_data = await asyncio.wait_for(self.reader.readexactly(size), timeout=RCON_TIMEOUT)
        packet_id, packet_type = struct.unpack('<ii', packet_data[:8])
        body = packet_data[8:-2].decode('utf-8', errors='ignore')
        return {'id': packet_id, 'type': packet_type, 'body': body}

    async def close(self):
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass


@register("steam_mod_monitor", "YourName", "Steam 创意工坊管家与游戏服 RCON 控制核心", "5.6.0")
class SteamModMonitor(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.state_lock = asyncio.Lock()  
        
        self.global_bot = None
        self.SEND_TIMEOUT = 15.0
        
        # --- 参数严谨解析 ---
        self.push_group_id = str(self.config.get("push_group_id", "")).strip()
        self.mod_ids_raw = str(self.config.get("mod_ids", ""))
        self.poll_interval = max(1, int(self.config.get("poll_interval_minutes", 30)))
        self.max_retries = max(1, int(self.config.get("max_retries", 5)))
        
        self.steam_api_base = str(self.config.get("steam_api_base", "https://api.steampowered.com")).rstrip('/')
        self.steam_api_key = str(self.config.get("steam_api_key", ""))
        
        self.auto_reset_enable = bool(self.config.get("auto_reset_enable", False))
        self.auto_reset_time = str(self.config.get("auto_reset_time", "04:20")).strip()
        
        self.server_ip = str(self.config.get("server_ip", "")).strip()
        self.server_port = int(self.config.get("server_port", 27015))
        self.server_rcon_password = str(self.config.get("server_rcon_password", "")).strip()
        
        self.server_rcon_broadcast = str(self.config.get("server_rcon_broadcast", "发现模组更新，请稍候..."))
        self.server_rcon_countdown = max(0, int(self.config.get("server_rcon_countdown", 5)))
        self.server_rcon_manual_broadcast = str(self.config.get("server_rcon_manual_broadcast", "服务器即将强制重启..."))
        self.server_rcon_manual_countdown = max(0, int(self.config.get("server_rcon_manual_countdown", 60)))
        
        self.server_restart_wait_minutes = max(1, int(self.config.get("server_restart_wait_minutes", 6)))
        
        self.mod_ids = [m.strip() for m in re.split(r'[,;]', self.mod_ids_raw) if m.strip()]
        
        # --- 内存状态与持久化配置 ---
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_steammod_monitor")
        self.data_file = Path(self.data_dir) / "data.json"
        
        self.last_update_times = {} 
        self.pending_updates = {} 
        self.last_reset_date = None
        self._load_data_sync()  
        
        self.is_running = bool(self.push_group_id and self.mod_ids)
        self.is_restarting = False 
        self.broadcast_sent_for_current_updates = False 
        
        # 任务声明
        self.monitor_task = None
        self.reset_task = None
        if self.is_running:
            logger.info(f"[Steam模组监控] 插件启动！配置目标群号: {self.push_group_id}")
            self.monitor_task = asyncio.create_task(self.monitor_loop(), name="steammod_monitor")
            self.reset_task = asyncio.create_task(self.auto_reset_loop(), name="steammod_reset")

    def _load_data_sync(self):
        if not self.data_file.parent.exists():
            self.data_file.parent.mkdir(parents=True, exist_ok=True)
            return
        if self.data_file.exists():
            try:
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.last_update_times = data.get("last_update_times", {})
                    self.pending_updates = data.get("pending_updates", {})
                    self.last_reset_date = data.get("last_reset_date", None)
            except Exception as e:
                logger.error(f"[Steam模组监控] 状态加载失败: {e}")

    async def _save_data(self):
        data = {
            "last_update_times": self.last_update_times,
            "pending_updates": self.pending_updates,
            "last_reset_date": self.last_reset_date
        }
        try:
            def _write():
                with open(self.data_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False)
            await asyncio.to_thread(_write)
        except Exception as e:
            pass

    async def terminate(self):
        if self.monitor_task and not self.monitor_task.done():
            self.monitor_task.cancel()
        if self.reset_task and not self.reset_task.done():
            self.reset_task.cancel()
        await self._save_data()
        logger.info("[Steam模组监控] 任务已终止并落盘。")

    @event_message_type(EventMessageType.ALL)
    async def silent_capture(self, event: AstrMessageEvent):
        """核心魔法：后台静默抓取 Bot 实例。只要有人发消息，瞬间拿到发送权限！"""
        if not self.global_bot:
            self.global_bot = event.bot
            logger.info("[Steam模组监控] 🚀 嗅探到活动，底层通讯通道已打通，支持纯QQ群号直推！")

    async def send_alert(self, msg: str):
        """智能路由引擎：纯数字走原生 API，复杂字符串走官方路由"""
        if not self.push_group_id:
            return
            
        # 1. 纯数字QQ号 -> 走 Native API (最爽的体验，直接成功)
        if self.push_group_id.isdigit() and self.global_bot:
            try:
                group_id_int = int(self.push_group_id)
                await asyncio.wait_for(
                    self.global_bot.api.call_action(
                        "send_group_msg", 
                        group_id=group_id_int, 
                        message=str(msg)
                    ),
                    timeout=self.SEND_TIMEOUT
                )
                logger.info(f"[Steam模组监控] ✅ 消息原生推送到群: {group_id_int}")
                return
            except Exception as e:
                logger.error(f"[Steam模组监控] ❌ 原生 API 推送失败: {repr(e)}")

        # 2. 如果不是纯数字（比如有人填了完整的 UMO），或者 global_bot 还没嗅探到，走官方兜底
        try:
            chain = MessageChain().message(msg)
            await self.context.send_message(self.push_group_id, chain)
            logger.info(f"[Steam模组监控] ✅ 消息通过官方路由推送到: {self.push_group_id}")
        except Exception as ex:
            logger.error(f"[Steam模组监控] ❌ 官方路由也失败了: {repr(ex)}")

    async def tcp_ping(self, host: str, port: int, timeout: int = 3) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False

    async def get_online_players(self) -> int:
        if not self.server_ip or not self.server_port or not self.server_rcon_password:
            return -1
            
        rcon = AsyncRCON(self.server_ip, self.server_port, self.server_rcon_password)
        try:
            await rcon.connect()
            resp = await rcon.execute("players")
            await rcon.close()
            match = re.search(r'Players connected \((\d+)\)', resp)
            if match:
                return int(match.group(1))
            return 0 
        except Exception as e:
            return -1

    async def verify_server_health(self, silent_success=False):
        wait_seconds = self.server_restart_wait_minutes * 60
        logger.info(f"[Steam模组监控] 进入健康等待 ({self.server_restart_wait_minutes}分钟)...")
        await asyncio.sleep(wait_seconds) 
        
        is_online = await self.tcp_ping(self.server_ip, self.server_port)
        if not is_online:
            logger.warning(f"[Steam模组监控] ⚠️ 初次 Ping 失败！等待 {HEALTH_CHECK_RETRY_INTERVAL} 秒重试...")
            await asyncio.sleep(HEALTH_CHECK_RETRY_INTERVAL) 
            is_online = await self.tcp_ping(self.server_ip, self.server_port)
            
        async with self.state_lock:
            self.is_restarting = False
            if is_online:
                self.pending_updates.clear() 
                self.broadcast_sent_for_current_updates = False
                await self._save_data()

        if is_online:
            logger.info("[Steam模组监控] ✅ 探针测试通过！")
            if not silent_success:
                success_msg = (
                    f"✅ 【服务器已重启成功】\n"
                    f"服务器连通性检测正常！最新模组已加载完毕，MOD模组状态信息已经更新。\n"
                    f"🎮 大家可以进入游戏游玩啦！"
                )
                await self.send_alert(success_msg)
        else:
            logger.error("[Steam模组监控] ❌ 重试后服务器依然离线！")
            fail_msg = (
                f"❌ 【服务器宕机严重告警】\n"
                f"重启后未能正常恢复开服！TCP 端口被拒绝。\n"
                f"🛠️ 请立刻检查 Docker 容器或服务端崩溃日志！"
            )
            await self.send_alert(fail_msg)

    @filter.command("steammod")
    async def handle_steammod(self, event: AstrMessageEvent, action: str = ""):
        # 手动发送指令，也可以起到主动激活通讯通道的作用
        if not self.global_bot:
            self.global_bot = event.bot

        action = action.strip().lower()
        
        if action == "on":
            self.is_running = True
            yield event.plain_result(f"✅ 监控已开启！巡视数量: {len(self.mod_ids)}")
            await self.check_steam_updates_with_retry(is_manual=True)
            
        elif action == "off":
            self.is_running = False
            yield event.plain_result("🛑 监控已挂起！")
            
        elif action == "reset":
            async with self.state_lock:
                self.pending_updates.clear()
                self.broadcast_sent_for_current_updates = False
                await self._save_data()
            yield event.plain_result("✅ 状态已人工重置并落盘！")
            
        elif action == "ping":
            if not self.server_ip:
                yield event.plain_result("❌ 未配置服务器 IP。")
                return
            yield event.plain_result(f"📡 探测游戏服 ({self.server_ip}:{self.server_port}) ...")
            if await self.tcp_ping(self.server_ip, self.server_port):
                yield event.plain_result("✅ 状态：[在线]\n端口通信正常。")
            else:
                yield event.plain_result("❌ 状态：[离线 / 无响应]")
                
        elif action == "restart":
            if not self.server_ip or not self.server_rcon_password:
                yield event.plain_result("❌ RCON 配置不全。")
                return
            
            async with self.state_lock:
                if self.is_restarting:
                    yield event.plain_result("⚠️ 警告：当前已在重启流程中，请勿重复触发！")
                    return
                self.is_restarting = True
                
            yield event.plain_result(f"⚡ 收到手动指令！已下发【{self.server_rcon_manual_countdown}秒】广播...")
            asyncio.create_task(self.execute_rcon_restart(is_auto=False))
            
        else:
            yield event.plain_result(f"🔍 核对 {len(self.mod_ids)} 个模组最后更新时间，请稍候...")
            async for msg in self.manual_status_check(event):
                yield msg

    async def execute_rcon_restart(self, is_auto=True):
        rcon = AsyncRCON(self.server_ip, self.server_port, self.server_rcon_password)
        try:
            await rcon.connect()
            if is_auto:
                await asyncio.sleep(self.server_rcon_countdown)
            else:
                clean_msg = self.server_rcon_manual_broadcast.replace('"', "'")
                await rcon.execute(f'servermsg "{clean_msg}"')
                await asyncio.sleep(self.server_rcon_manual_countdown)
                
            await rcon.execute("save")
            await asyncio.sleep(SAVE_DELAY) 
            await rcon.execute("quit")
            await rcon.close()
            
            logger.info("[Steam模组监控] Quit 关机已下达，移交探针。")
            asyncio.create_task(self.verify_server_health(silent_success=False))
            
        except Exception as e:
            logger.error(f"[Steam模组监控] RCON 重启失败: {type(e).__name__} - {str(e)}")
            async with self.state_lock:
                self.is_restarting = False
            await self.send_alert(f"❌ RCON 触发失败，请人工检查。报错: {type(e).__name__}")
            if rcon: await rcon.close()

    async def auto_reset_loop(self):
        await asyncio.sleep(10)
        while True:
            if self.is_running and self.auto_reset_enable and self.auto_reset_time:
                try:
                    now = datetime.datetime.now()
                    now_date = now.strftime("%Y-%m-%d")
                    if now.strftime("%H:%M") == self.auto_reset_time and self.last_reset_date != now_date:
                        self.last_reset_date = now_date
                        
                        if self.server_ip and self.server_port:
                            asyncio.create_task(self.verify_server_health(silent_success=True))
                        else:
                            async with self.state_lock:
                                self.pending_updates.clear()
                                await self._save_data()
                except Exception as e:
                    logger.error(f"[Steam模组监控] 定时安检异常: {str(e)}")
            await asyncio.sleep(20)

    async def manual_status_check(self, event: AstrMessageEvent):
        url = f"{self.steam_api_base}/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
        data = {"itemcount": len(self.mod_ids)}
        for i, mod_id in enumerate(self.mod_ids):
            data[f"publishedfileids[{i}]"] = mod_id
        if self.steam_api_key:
            data["key"] = self.steam_api_key

        try:
            timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_TOTAL, connect=API_TIMEOUT_CONN)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                async with session.post(url, data=data) as response:
                    if response.status != 200:
                        yield event.plain_result(f"❌ 查询失败，状态码: {response.status}")
                        return
                        
                    result = await response.json()
                    items = result.get('response', {}).get('publishedfiledetails', [])
                    
                    lines = []
                    needs_update_count = 0
                    
                    for item in items:
                        mod_id = str(item.get('publishedfileid', ''))
                        mod_name = item.get('title', '未知模组')
                        current_time = item.get('time_updated', 0)
                        if not mod_id: continue
                            
                        time_str = datetime.datetime.fromtimestamp(current_time).strftime('%Y-%m-%d %H:%M') if current_time > 0 else "未知时间"
                        
                        if mod_id in self.pending_updates:
                            lines.append(f"🔴 {mod_name} ({mod_id}) | 需更新 (最新: {time_str})")
                            needs_update_count += 1
                        else:
                            lines.append(f"🟢 {mod_name} ({mod_id}) | {time_str}")
                    
                    long_text = "【当前模组健康状态明细】\n\n" + "\n".join(lines)
                    bot_id = str(event.get_self_id()) if hasattr(event, 'get_self_id') else "10000"
                    
                    yield event.chain_result([Node(uin=bot_id, custom_name="Steam模组管家", content=[Plain(long_text)])])
                    await asyncio.sleep(0.5)
                    
                    summary = f"📊 状态总结：共 {len(self.mod_ids)} 个，需更新 {needs_update_count} 个。"
                    yield event.plain_result(summary)
        except Exception as e:
            yield event.plain_result(f"❌ 网络异常: {type(e).__name__}")

    async def monitor_loop(self):
        await asyncio.sleep(5) 
        while True:
            if self.is_running:
                await self.check_steam_updates_with_retry(is_manual=False)
            await asyncio.sleep(self.poll_interval * 60)

    async def check_steam_updates_with_retry(self, is_manual=False):
        url = f"{self.steam_api_base}/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
        data = {"itemcount": len(self.mod_ids)}
        for i, mod_id in enumerate(self.mod_ids):
            data[f"publishedfileids[{i}]"] = mod_id
        if self.steam_api_key:
            data["key"] = self.steam_api_key

        timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_TOTAL, connect=API_TIMEOUT_CONN)
        for attempt in range(1, self.max_retries + 1):
            try:
                async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                    async with session.post(url, data=data) as response:
                        if response.status != 200:
                            await asyncio.sleep(RETRY_DELAY)
                            continue
                        result = await response.json()
                        await self.process_result(result, is_manual)
                        return 
            except Exception as e:
                if attempt < self.max_retries:
                    await asyncio.sleep(RETRY_DELAY) 
                else:
                    logger.error(f"[Steam模组监控] 自动轮询最终失败: {type(e).__name__}")

    async def process_result(self, result, is_manual):
        items = result.get('response', {}).get('publishedfiledetails', [])
        new_updates_found = False
        state_changed = False
        
        async with self.state_lock:
            for item in items:
                mod_id = str(item.get('publishedfileid', ''))
                mod_name = item.get('title', '未知模组')
                current_time = item.get('time_updated', 0)
                
                if not mod_id or current_time == 0:
                    continue
                    
                if mod_id not in self.last_update_times:
                    self.last_update_times[mod_id] = current_time
                    state_changed = True
                    continue
                    
                if current_time > self.last_update_times[mod_id]:
                    logger.info(f"[Steam模组监控] 🚨 发现新版本！模组: {mod_name}")
                    self.last_update_times[mod_id] = current_time
                    time_str = datetime.datetime.fromtimestamp(current_time).strftime('%Y-%m-%d %H:%M')
                    self.pending_updates[mod_id] = {"name": mod_name, "time": time_str}
                    new_updates_found = True
                    state_changed = True

            if state_changed:
                await self._save_data()
                
            needs_restart = bool(self.pending_updates) and not self.is_restarting

        if not is_manual and needs_restart:
            await self.handle_auto_restart_logic(new_updates_found)

    async def handle_auto_restart_logic(self, new_updates_found: bool):
        players_count = await self.get_online_players()
        
        if players_count == -1:
            logger.error("[Steam模组监控] 获取玩家人数异常，已取消本次自动处理保护服务器。")
            return
            
        async with self.state_lock:
            mod_list_str = "\n".join([f"- {v['name']} ({k}) [{v['time']}]" for k, v in self.pending_updates.items()])
        
        if players_count == 0:
            async with self.state_lock:
                self.is_restarting = True
                
            msg = (
                f"🔄 【检测到steam订阅模组更新】\n"
                f"{mod_list_str}\n\n"
                f"🕵️ 经 RCON 查验，当前服务器【无玩家在线】。\n"
                f"🚀 正在静默执行重启升级，预计耗时 {self.server_restart_wait_minutes} 分钟，请稍候..."
            )
            await self.send_alert(msg)
            asyncio.create_task(self.execute_rcon_restart(is_auto=True))
            
        elif players_count > 0:
            if new_updates_found or not self.broadcast_sent_for_current_updates:
                qq_msg = (
                    f"⚠️ 【检测到steam订阅模组更新】\n"
                    f"{mod_list_str}\n\n"
                    f"👥 经 RCON 查验，当前服务器有【{players_count} 位玩家】在线。\n"
                    f"⏳ 已向游戏内发送广播预警。将在下一轮检测（或玩家离线后）自动执行重启，请留意后续通知。"
                )
                await self.send_alert(qq_msg)
                
                rcon = AsyncRCON(self.server_ip, self.server_port, self.server_rcon_password)
                try:
                    await rcon.connect()
                    clean_broadcast = self.server_rcon_broadcast.replace('"', "'")
                    await rcon.execute(f'servermsg "{clean_broadcast}"')
                    await rcon.close()
                    async with self.state_lock:
                        self.broadcast_sent_for_current_updates = True 
                        await self._save_data()
                except Exception as e:
                    logger.error(f"[Steam模组监控] 延迟发广播失败: {str(e)}")
                    if rcon: await rcon.close()
