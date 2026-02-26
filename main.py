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
        await self._send(3, self.password) # 3: SERVERDATA_AUTH
        resp = await self._read()
        if resp.get('id') == -1:
            raise Exception("RCON 认证失败：密码错误或被拒绝")

    async def execute(self, command):
        await self._send(2, command) # 2: SERVERDATA_EXECCOMMAND
        resp = await self._read()
        return resp.get('body')

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

@register("steam_mod_monitor", "YourName", "全自动 Steam 模组监控与 RCON 控制插件", "4.0.0")
class SteamModMonitor(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        self.push_group_id = self.config.get("push_group_id", "").strip()
        self.mod_ids_raw = self.config.get("mod_ids", "")
        self.poll_interval = self.config.get("poll_interval_minutes", 30)
        self.max_retries = self.config.get("max_retries", 5)
        self.steam_api_base = self.config.get("steam_api_base", "https://api.steampowered.com").rstrip('/')
        self.steam_api_key = self.config.get("steam_api_key", "")
        self.push_template = self.config.get("push_template", "🚨 模组【{mod_name}】已更新！")
        
        self.auto_reset_enable = self.config.get("auto_reset_enable", False)
        self.auto_reset_time = self.config.get("auto_reset_time", "04:20").strip()
        self.auto_reset_message = self.config.get("auto_reset_message", "🌅 早上好！游戏服务器已完成每日例行重启。")
        self.last_reset_date = None 
        
        self.server_ip = self.config.get("server_ip", "").strip()
        self.server_port = self.config.get("server_port", 27015)
        self.server_rcon_password = self.config.get("server_rcon_password", "").strip()
        self.server_rcon_broadcast = self.config.get("server_rcon_broadcast", "Warning! Server will RESTART!")
        self.server_rcon_countdown = self.config.get("server_rcon_countdown", 60)
        
        self.mod_ids = [m.strip() for m in re.split(r'[,;]', self.mod_ids_raw) if m.strip()]
        self.last_update_times = {} 
        self.pending_updates = set() 
        self.is_running = bool(self.push_group_id and self.mod_ids)
        
        for task in asyncio.all_tasks():
            if task.get_name() in ["steam_mod_monitor_loop_task", "steam_mod_monitor_reset_task"]:
                task.cancel()

        if self.is_running:
            logger.info(f"[Steam模组监控] 插件 v4.0.0 启动完毕！共监控 {len(self.mod_ids)} 个模组。")
            
        self.monitor_task = asyncio.create_task(self.monitor_loop(), name="steam_mod_monitor_loop_task")
        self.reset_task = asyncio.create_task(self.auto_reset_loop(), name="steam_mod_monitor_reset_task")

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

    @filter.command("steammod")
    async def handle_steammod(self, event: AstrMessageEvent, action: str = ""):
        action = action.strip().lower()
        
        if action == "on":
            if not self.push_group_id:
                yield event.plain_result("❌ 启动失败：请先配置 [推送群号]！")
                return
            self.is_running = True
            yield event.plain_result(f"✅ 监控已开启！正在对 {len(self.mod_ids)} 个模组进行高强度巡视。")
            await self.check_steam_updates_with_retry(is_manual=True)
            
        elif action == "off":
            self.is_running = False
            yield event.plain_result("🛑 监控已暂停！后台轮询已停止。")
            
        elif action == "reset":
            self.pending_updates.clear()
            yield event.plain_result("✅ 状态已重置！所有红灯已清除。")
            
        elif action == "ping":
            if not self.server_ip or not self.server_port:
                yield event.plain_result("❌ 请先在配置页面填写 [服务器 IP/域名] 和 [探测端口]！")
                return
            yield event.plain_result(f"📡 正在 Ping 探测游戏服务器 ({self.server_ip}:{self.server_port}) ...")
            is_online = await self.tcp_ping(self.server_ip, self.server_port)
            if is_online:
                yield event.plain_result("✅ 状态：[在线]\n端口通信正常，说明服务器 100% 启动完毕并已加载所有 Mod。")
            else:
                yield event.plain_result("❌ 状态：[离线 / 无响应]\n端口被拒绝，服务器可能未启动、死机或网络异常。")
                
        elif action == "restart":
            # 核心杀招：执行 RCON 安全重启方案
            if not self.server_ip or not self.server_port or not self.server_rcon_password:
                yield event.plain_result("❌ 缺少 RCON 配置信息，请在控制台填好 IP、端口和 RCON 密码！")
                return
            
            yield event.plain_result(f"⚡ 正在连接服务器 RCON 执行安全保存与关机程序，等待倒计时 {self.server_rcon_countdown} 秒...")
            
            # 使用协程后台执行，不阻塞机器人收发消息
            asyncio.create_task(self.execute_rcon_restart(event))
            
        else:
            if not self.mod_ids:
                yield event.plain_result("❌ 模组列表为空，请先在控制台配置。")
                return
            yield event.plain_result(f"🔍 正在向 Steam 核对 {len(self.mod_ids)} 个模组的最后更新时间，请稍候...")
            async for msg in self.manual_status_check(event):
                yield msg

    async def execute_rcon_restart(self, event: AstrMessageEvent):
        """执行全套 RCON 丝滑安全关机连招"""
        rcon = AsyncRCON(self.server_ip, self.server_port, self.server_rcon_password)
        try:
            await rcon.connect()
            logger.info("[Steam模组监控] RCON 连接并认证成功！")
            
            # 1. 游戏内发广播
            clean_msg = self.server_rcon_broadcast.replace('"', "'") # 防止双引号语法错误
            await rcon.execute(f'servermsg "{clean_msg}"')
            logger.info(f"[Steam模组监控] 已发送全服广播: {clean_msg}")
            
            # 2. 等待群主设定的倒计时
            await asyncio.sleep(self.server_rcon_countdown)
            
            # 3. 强制保存进度
            await rcon.execute("save")
            logger.info("[Steam模组监控] 执行 Save 保存指令完毕。")
            await asyncio.sleep(5) # 给硬盘一点写入时间
            
            # 4. 优雅关机
            await rcon.execute("quit")
            logger.info("[Steam模组监控] 执行 Quit 关机指令完毕。")
            
            await rcon.close()
            
            success_msg = (
                f"✅ 【指令下达成功】\n"
                f"已完美执行：全服广播 -> 倒计时 -> 存档保护 -> 安全退出！\n"
                f"💡 游戏进程现已结束。若您的 Docker 已开启“自动重启”策略，服务器将在几十秒后带着最新模组王者归来！\n"
                f"（稍后可使用 /steammod ping 检查是否重启开服成功）"
            )
            await self.context.send_message(event.message_obj.group_id, success_msg)
            
        except Exception as e:
            logger.error(f"[Steam模组监控] RCON 重启任务失败: {repr(e)}")
            await self.context.send_message(event.message_obj.group_id, f"❌ RCON 任务执行失败：\n{repr(e)}\n请检查密码是否正确，或服务器是否已经离线。")
            if rcon:
                await rcon.close()

    async def auto_reset_loop(self):
        await asyncio.sleep(10)
        while True:
            if self.is_running and self.auto_reset_enable and self.auto_reset_time:
                try:
                    now = datetime.datetime.now()
                    current_time_str = now.strftime("%H:%M")
                    current_date_str = now.strftime("%Y-%m-%d")
                    
                    if current_time_str == self.auto_reset_time and self.last_reset_date != current_date_str:
                        logger.info(f"[Steam模组监控] 触发每日定时任务 ({self.auto_reset_time})")
                        self.last_reset_date = current_date_str 
                        
                        if self.server_ip and self.server_port:
                            logger.info(f"[Steam模组监控] 正在执行定时安检 Ping...")
                            is_online = await self.tcp_ping(self.server_ip, self.server_port)
                            if not is_online:
                                err_msg = (
                                    f"⚠️ 【服务器宕机告警】\n"
                                    f"自动重置任务已触发，但 AstrBot 无法连通游戏服 ({self.server_ip}:{self.server_port})！\n"
                                    f"👉 动作：已中止早安播报与红灯重置。\n"
                                    f"🛠️ 请服主立刻检查 Docker 容器是否卡死或重启报错！"
                                )
                                try:
                                    await self.context.send_message(self.push_group_id, err_msg)
                                except Exception:
                                    pass
                                continue 
                        
                        self.pending_updates.clear()
                        if self.auto_reset_message.strip():
                            try:
                                await self.context.send_message(self.push_group_id, self.auto_reset_message)
                            except Exception as e:
                                logger.error(f"[Steam模组监控] 发送定时重置播报失败: {repr(e)}")
                                
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"[Steam模组监控] 定时重置任务异常: {repr(e)}")
                    
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
                        yield event.plain_result(f"❌ 查询失败，Steam 接口返回状态码: {response.status}")
                        return
                        
                    result = await response.json()
                    items = result.get('response', {}).get('publishedfiledetails', [])
                    
                    lines = []
                    needs_update_count = 0
                    
                    for item in items:
                        mod_id = str(item.get('publishedfileid', ''))
                        mod_name = item.get('title', '未知模组')
                        current_time = item.get('time_updated', 0)
                        
                        if not mod_id:
                            continue
                            
                        if current_time > 0:
                            time_str = datetime.datetime.fromtimestamp(current_time).strftime('%Y-%m-%d %H:%M')
                        else:
                            time_str = "未知时间"
                        
                        if mod_id in self.pending_updates:
                            lines.append(f"🔴 {mod_name} ({mod_id}) | 需更新 (最新版本: {time_str})")
                            needs_update_count += 1
                        else:
                            lines.append(f"🟢 {mod_name} ({mod_id}) | {time_str}")
                    
                    long_text = "【当前服务器模组健康状态明细】\n\n" + "\n".join(lines)
                    
                    try:
                        bot_id = str(event.get_self_id())
                    except Exception:
                        bot_id = "10000" 
                    
                    forward_node = Node(
                        uin=bot_id,
                        custom_name="Steam模组管家",
                        content=[Plain(long_text)]
                    )
                    
                    yield event.chain_result([forward_node])
                    await asyncio.sleep(0.5)
                    
                    total = len(self.mod_ids)
                    summary = (
                        f"📊 监控状态总结：\n"
                        f"一共监控了 {total} 个模组\n"
                        f"需要更新的有 {needs_update_count} 个 ({needs_update_count}/{total})"
                    )
                    
                    if needs_update_count > 0:
                        summary += "\n\n💡 提示：重启游戏服务器后，请发送 /steammod reset 消除以上红灯报警。"
                    
                    yield event.plain_result(summary)
                    
        except Exception as e:
            yield event.plain_result(f"❌ 手动查询出现网络异常: {repr(e)}")

    async def monitor_loop(self):
        await asyncio.sleep(5) 
        while True:
            if self.is_running:
                logger.info(f"[Steam模组监控] 开始向 Steam 获取 {len(self.mod_ids)} 个模组的最新状态...")
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
                            if attempt == self.max_retries:
                                logger.error(f"[Steam模组监控] 接口报错: {response.status}，已放弃。")
                            await asyncio.sleep(5)
                            continue
                        result = await response.json()
                        await self.process_result(result, is_manual)
                        return 
            except Exception as e:
                if attempt < self.max_retries:
                    logger.debug(f"[Steam模组监控] 网络波动重试 {attempt}/{self.max_retries}...")
                    await asyncio.sleep(5) 
                else:
                    logger.error(f"[Steam模组监控] ❌ 自动轮询因网络问题最终失败: {repr(e)}")

    async def process_result(self, result, is_manual):
        items = result.get('response', {}).get('publishedfiledetails', [])
        updated_count = 0
        
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
                logger.info(f"[Steam模组监控] 🚨 发现更新！模组: {mod_name}")
                self.last_update_times[mod_id] = current_time
                self.pending_updates.add(mod_id) 
                updated_count += 1
                await self.push_alert(mod_id, mod_name)
                
        if not is_manual:
            if updated_count > 0:
                logger.info(f"[Steam模组监控] 本轮检查完毕，发现 {updated_count} 个更新。")
            else:
                logger.info(f"[Steam模组监控] 本轮检查完毕，所有模组均为最新。")

    async def push_alert(self, mod_id: str, mod_name: str):
        msg_text = self.push_template.replace("{mod_name}", mod_name).replace("{mod_id}", mod_id)
        try:
            await self.context.send_message(self.push_group_id, msg_text)
        except Exception as e:
            logger.error(f"[Steam模组监控] 推送失败: {repr(e)}")
