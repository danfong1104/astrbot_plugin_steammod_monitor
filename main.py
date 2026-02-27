import asyncio
import aiohttp
import json
import re
import datetime
import struct
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Node, Plain

class AsyncRCON:
    """轻量级纯异步 Source RCON 客户端"""
    def __init__(self, host, port, password):
        self.host = host
        self.port = port
        self.password = password
        self.reader = None
        self.writer = None

    async def connect(self):
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=5.0
        )
        await self._send(3, self.password) 
        resp = await self._read()
        if resp.get('id') == -1:
            raise Exception("RCON 认证失败：密码错误或被拒绝")

    async def execute(self, command):
        await self._send(2, command) 
        resp = await self._read()
        return resp.get('body', '')

    async def _send(self, packet_type, body):
        body_encoded = body.encode('utf-8')
        packet_id = 1
        packet_size = 10 + len(body_encoded)
        packet = struct.pack('<iii', packet_size, packet_id, packet_type) + body_encoded + b'\x00\x00'
        self.writer.write(packet)
        await self.writer.drain()

    async def _read(self):
        size_data = await asyncio.wait_for(self.reader.readexactly(4), timeout=5.0)
        size = struct.unpack('<i', size_data)[0]
        packet_data = await asyncio.wait_for(self.reader.readexactly(size), timeout=5.0)
        packet_id, packet_type = struct.unpack('<ii', packet_data[:8])
        body = packet_data[8:-2].decode('utf-8', errors='ignore')
        return {'id': packet_id, 'type': packet_type, 'body': body}

    async def close(self):
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()

@register("steam_mod_monitor", "YourName", "全自动 Steam 模组监控与零玩家侦测插件", "5.3.1")
class SteamModMonitor(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        # 引入全局 bot 实例，用于底层 Native API 推送
        self.global_bot = None
        self.SEND_TIMEOUT = 15.0
        
        self.push_group_id = self.config.get("push_group_id", "").strip()
        self.mod_ids_raw = self.config.get("mod_ids", "")
        self.poll_interval = self.config.get("poll_interval_minutes", 30)
        self.max_retries = self.config.get("max_retries", 5)
        self.steam_api_base = self.config.get("steam_api_base", "https://api.steampowered.com").rstrip('/')
        self.steam_api_key = self.config.get("steam_api_key", "")
        
        self.auto_reset_enable = self.config.get("auto_reset_enable", False)
        self.auto_reset_time = self.config.get("auto_reset_time", "04:20").strip()
        self.last_reset_date = None 
        
        self.server_ip = self.config.get("server_ip", "").strip()
        self.server_port = self.config.get("server_port", 27015)
        self.server_rcon_password = self.config.get("server_rcon_password", "").strip()
        
        self.server_rcon_broadcast = self.config.get("server_rcon_broadcast", "查询到模组有更新，不影响您本次游玩，您游戏结束1小时后会自动更新，如需要强制更新，请联系麻花！")
        self.server_rcon_countdown = self.config.get("server_rcon_countdown", 5)
        
        self.server_rcon_manual_broadcast = self.config.get("server_rcon_manual_broadcast", "服务器将在60秒后强制重启更新模组，请尽快找到安全的地方等待！")
        self.server_rcon_manual_countdown = self.config.get("server_rcon_manual_countdown", 60)
        
        self.server_restart_wait_minutes = self.config.get("server_restart_wait_minutes", 6)
        
        self.mod_ids = [m.strip() for m in re.split(r'[,;]', self.mod_ids_raw) if m.strip()]
        self.last_update_times = {} 
        self.pending_updates = {} 
        self.is_running = bool(self.push_group_id and self.mod_ids)
        self.is_restarting = False 
        self.broadcast_sent_for_current_updates = False 
        
        for task in asyncio.all_tasks():
            if task.get_name() in ["steam_mod_monitor_loop_task", "steam_mod_monitor_reset_task"]:
                task.cancel()

        if self.is_running:
            logger.info(f"[Steam模组监控] 插件 v5.3.1 启动完毕！探针延迟设为 {self.server_restart_wait_minutes} 分钟。")
            
        self.monitor_task = asyncio.create_task(self.monitor_loop(), name="steam_mod_monitor_loop_task")
        self.reset_task = asyncio.create_task(self.auto_reset_loop(), name="steam_mod_monitor_reset_task")

    async def send_alert(self, msg: str):
        """采用底层 Native API 直接推送，免疫框架 Session 格式校验错误"""
        if not self.push_group_id:
            return
            
        if not self.global_bot:
            logger.error("[Steam模组监控] ❌ 尚未捕获 Bot 实例！请在群内发送一次 /steammod 指令以激活底层推送能力！")
            return
            
        try:
            group_id_int = int(str(self.push_group_id).strip())
            msg_str = str(msg)
            
            await asyncio.wait_for(
                self.global_bot.api.call_action(
                    "send_group_msg", 
                    group_id=group_id_int, 
                    message=msg_str
                ),
                timeout=self.SEND_TIMEOUT
            )
            logger.info(f"[Steam模组监控] ✅ 消息成功推送到群: {group_id_int}")
        except Exception as e:
            logger.error(f"[Steam模组监控] ❌ Native API 调用推送群消息失败: {repr(e)}")

    async def tcp_ping(self, host: str, port: int, timeout: int = 3):
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False

    async def get_online_players(self):
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
            logger.error(f"[Steam模组监控] 获取玩家人数失败: {repr(e)}")
            return -1

    async def verify_server_health(self, silent_success=False):
        wait_seconds = self.server_restart_wait_minutes * 60
        logger.info(f"[Steam模组监控] 进入健康度等待周期，预计 {wait_seconds} 秒 ({self.server_restart_wait_minutes}分钟) 后进行初次探针测试...")
        await asyncio.sleep(wait_seconds) 
        
        is_online = await self.tcp_ping(self.server_ip, self.server_port)
        if not is_online:
            logger.warning("[Steam模组监控] ⚠️ 初次 Ping 失败！进入 3 分钟容错等待周期...")
            await asyncio.sleep(180) 
            is_online = await self.tcp_ping(self.server_ip, self.server_port)
            
        if is_online:
            logger.info("[Steam模组监控] ✅ 探针测试通过，服务器已恢复在线！")
            self.pending_updates.clear() 
            self.broadcast_sent_for_current_updates = False
            self.is_restarting = False
            
            if not silent_success:
                success_msg = (
                    f"✅ 【服务器已重启成功】\n"
                    f"服务器连通性检测正常！最新模组已加载完毕，MOD模组状态信息已经更新。\n"
                    f"🎮 大家可以进入游戏游玩啦！"
                )
                await self.send_alert(success_msg)
        else:
            logger.error("[Steam模组监控] ❌ 严重错误：经过延时重试，服务器依然离线！")
            self.is_restarting = False
            fail_msg = (
                f"❌ 【服务器宕机严重告警】\n"
                f"服务器在执行重启后未能正常恢复开服！\n"
                f"经过 {self.server_restart_wait_minutes}分钟初始等待 + 3 分钟延时重试，TCP 端口依然被拒绝。\n"
                f"🛠️ 请立刻检查 Docker 容器或服务端崩溃日志！"
            )
            await self.send_alert(fail_msg)

    @filter.command("steammod")
    async def handle_steammod(self, event: AstrMessageEvent, action: str = ""):
        # 核心逻辑：捕获 bot 实例，用于底层主动推送
        if not self.global_bot:
            self.global_bot = event.bot
            logger.info("[Steam模组监控] Bot 实例已成功捕获，具备底层主动推送能力！")

        action = action.strip().lower()
        if action == "on":
            self.is_running = True
            yield event.plain_result(f"✅ 监控已开启！正在对 {len(self.mod_ids)} 个模组进行高强度巡视。")
            await self.check_steam_updates_with_retry(is_manual=True)
        elif action == "off":
            self.is_running = False
            yield event.plain_result("🛑 监控已暂停！后台轮询已停止。")
        elif action == "reset":
            self.pending_updates.clear()
            self.broadcast_sent_for_current_updates = False
            yield event.plain_result("✅ 状态已强制人工重置！")
        elif action == "ping":
            if not self.server_ip:
                yield event.plain_result("❌ 未配置服务器 IP。")
                return
            yield event.plain_result(f"📡 正在 Ping 探测游戏服务器 ({self.server_ip}:{self.server_port}) ...")
            if await self.tcp_ping(self.server_ip, self.server_port):
                yield event.plain_result("✅ 状态：[在线]\n端口通信正常，说明服务器 100% 启动完毕。")
            else:
                yield event.plain_result("❌ 状态：[离线 / 无响应]\n端口被拒绝，服务器未启动或死机。")
        elif action == "restart":
            if not self.server_ip or not self.server_rcon_password:
                yield event.plain_result("❌ RCON 配置不全。")
                return
            yield event.plain_result(f"⚡ 收到手动重启指令！已下发【{self.server_rcon_manual_countdown}秒】强制重启广播...")
            self.is_restarting = True
            asyncio.create_task(self.execute_rcon_restart(is_auto=False))
        else:
            yield event.plain_result(f"🔍 正在向 Steam 核对 {len(self.mod_ids)} 个模组的最后更新时间，请稍候...")
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
                logger.info(f"[Steam模组监控] 已向游戏内发送人工强制重启广播。")
                await asyncio.sleep(self.server_rcon_manual_countdown)
                
            await rcon.execute("save")
            await asyncio.sleep(5) 
            await rcon.execute("quit")
            await rcon.close()
            
            logger.info("[Steam模组监控] Quit 指令已发送，移交健康探针接管。")
            asyncio.create_task(self.verify_server_health(silent_success=False))
            
        except Exception as e:
            logger.error(f"[Steam模组监控] RCON 重启失败: {repr(e)}")
            self.is_restarting = False
            await self.send_alert(f"❌ RCON 触发重启失败：{repr(e)}。无法自动更新，请人工检查。")
            if rcon: await rcon.close()

    async def auto_reset_loop(self):
        await asyncio.sleep(10)
        while True:
            if self.is_running and self.auto_reset_enable and self.auto_reset_time:
                try:
                    now = datetime.datetime.now()
                    if now.strftime("%H:%M") == self.auto_reset_time and self.last_reset_date != now.strftime("%Y-%m-%d"):
                        logger.info(f"[Steam模组监控] 触发每日定时安检 ({self.auto_reset_time})")
                        self.last_reset_date = now.strftime("%Y-%m-%d")
                        
                        if self.server_ip and self.server_port:
                            asyncio.create_task(self.verify_server_health(silent_success=True))
                        else:
                            self.pending_updates.clear()
                            
                except Exception as e:
                    logger.error(f"[Steam模组监控] 定时安检异常: {repr(e)}")
            await asyncio.sleep(20)

    async def manual_status_check(self, event: AstrMessageEvent):
        url = f"{self.steam_api_base}/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
        data = {"itemcount": len(self.mod_ids)}
        for i, mod_id in enumerate(self.mod_ids):
            data[f"publishedfileids[{i}]"] = mod_id
        if self.steam_api_key:
            data["key"] = self.steam_api_key

        try:
            timeout = aiohttp.ClientTimeout(total=45, connect=15)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                async with session.post(url, data=data) as response:
                    if response.status != 200:
                        yield event.plain_result(f"❌ 查询失败状态码: {response.status}")
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
                    
                    long_text = "【当前服务器模组健康状态明细】\n\n" + "\n".join(lines)
                    bot_id = str(event.get_self_id()) if hasattr(event, 'get_self_id') else "10000"
                    
                    yield event.chain_result([Node(uin=bot_id, custom_name="Steam模组管家", content=[Plain(long_text)])])
                    await asyncio.sleep(0.5)
                    
                    summary = f"📊 状态总结：共 {len(self.mod_ids)} 个，需更新 {needs_update_count} 个。"
                    yield event.plain_result(summary)
                    
        except Exception as e:
            yield event.plain_result(f"❌ 网络异常: {repr(e)}")

    async def monitor_loop(self):
        await asyncio.sleep(5) 
        while True:
            if self.is_running:
                logger.info(f"[Steam模组监控] 开始后台静默查表...")
                await self.check_steam_updates_with_retry(is_manual=False)
            await asyncio.sleep(self.poll_interval * 60)

    async def check_steam_updates_with_retry(self, is_manual=False):
        url = f"{self.steam_api_base}/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
        data = {"itemcount": len(self.mod_ids)}
        for i, mod_id in enumerate(self.mod_ids):
            data[f"publishedfileids[{i}]"] = mod_id
        if self.steam_api_key:
            data["key"] = self.steam_api_key

        timeout = aiohttp.ClientTimeout(total=45, connect=15)
        for attempt in range(1, self.max_retries + 1):
            try:
                async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                    async with session.post(url, data=data) as response:
                        if response.status != 200:
                            await asyncio.sleep(5)
                            continue
                        result = await response.json()
                        await self.process_result(result, is_manual)
                        return 
            except Exception as e:
                if attempt < self.max_retries:
                    await asyncio.sleep(5) 
                else:
                    logger.error(f"[Steam模组监控] ❌ 自动轮询最终失败: {repr(e)}")

    async def process_result(self, result, is_manual):
        items = result.get('response', {}).get('publishedfiledetails', [])
        new_updates_found = False
        
        for item in items:
            mod_id = str(item.get('publishedfileid', ''))
            mod_name = item.get('title', '未知模组')
            current_time = item.get('time_updated', 0)
            
            if not mod_id or current_time == 0:
                continue
                
            if mod_id not in self.last_update_times:
                self.last_update_times[mod_id] = current_time
                continue
                
            if current_time > self.last_update_times[mod_id]:
                logger.info(f"[Steam模组监控] 🚨 发现新版本！模组: {mod_name}")
                self.last_update_times[mod_id] = current_time
                time_str = datetime.datetime.fromtimestamp(current_time).strftime('%Y-%m-%d %H:%M')
                self.pending_updates[mod_id] = {"name": mod_name, "time": time_str}
                new_updates_found = True
                
        if not is_manual:
            if self.pending_updates and not self.is_restarting:
                await self.handle_auto_restart_logic(new_updates_found)

    async def handle_auto_restart_logic(self, new_updates_found):
        players_count = await self.get_online_players()
        mod_list_str = "\n".join([f"- {v['name']} ({k}) [{v['time']}]" for k, v in self.pending_updates.items()])
        
        if players_count == 0:
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
                    f"⏳ 已向游戏内发送广播预警。将在下一轮检测（或玩家全部离线后）自动执行重启，请留意后续通知。"
                )
                await self.send_alert(qq_msg)
                
                rcon = AsyncRCON(self.server_ip, self.server_port, self.server_rcon_password)
                try:
                    await rcon.connect()
                    clean_broadcast = self.server_rcon_broadcast.replace('"', "'")
                    await rcon.execute(f'servermsg "{clean_broadcast}"')
                    await rcon.close()
                    self.broadcast_sent_for_current_updates = True 
                    logger.info("[Steam模组监控] 有玩家在线，已推迟重启并发送中文广播。")
                except Exception as e:
                    logger.error(f"[Steam模组监控] 延迟重启时发广播失败: {repr(e)}")
                    if rcon: await rcon.close()
