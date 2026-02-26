import asyncio
import aiohttp
import json
import re
import datetime
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Node, Plain

@register("steam_mod_monitor", "YourName", "全自动 Steam 模组监控插件", "3.5.0")
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
        self.auto_reset_message = self.config.get("auto_reset_message", "🌅 早上好！游戏服务器已完成每日例行重启。当前所有模组警报已消除，大家可以放心进入肯塔基州啦！")
        self.last_reset_date = None 
        
        # 新增的服务器探针配置
        self.server_ip = self.config.get("server_ip", "").strip()
        self.server_port = self.config.get("server_port", 27015)
        
        self.mod_ids = [m.strip() for m in re.split(r'[,;]', self.mod_ids_raw) if m.strip()]
        self.last_update_times = {} 
        self.pending_updates = set() 
        self.is_running = bool(self.push_group_id and self.mod_ids)
        
        for task in asyncio.all_tasks():
            if task.get_name() in ["steam_mod_monitor_loop_task", "steam_mod_monitor_reset_task"]:
                task.cancel()
                logger.info(f"[Steam模组监控] 🧹 已清理上次重载遗留的旧后台任务: {task.get_name()}")

        if self.is_running:
            logger.info(f"[Steam模组监控] 插件已启动！共载入 {len(self.mod_ids)} 个模组。")
        else:
            logger.warning("[Steam模组监控] 启动挂起：未配置推送群号或模组列表为空。")

        self.monitor_task = asyncio.create_task(self.monitor_loop(), name="steam_mod_monitor_loop_task")
        self.reset_task = asyncio.create_task(self.auto_reset_loop(), name="steam_mod_monitor_reset_task")

    async def tcp_ping(self, host: str, port: int, timeout: int = 3):
        """核心 TCP 端口探测器"""
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
            yield event.plain_result("✅ 状态已重置！所有红灯已清除，目前全库显示为最新（绿灯）状态。")
            
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
            
        else:
            if not self.mod_ids:
                yield event.plain_result("❌ 模组列表为空，请先在控制台配置。")
                return
                
            yield event.plain_result(f"🔍 正在向 Steam 核对 {len(self.mod_ids)} 个模组的最后更新时间戳，请稍候...")
            async for msg in self.manual_status_check(event):
                yield msg

    async def auto_reset_loop(self):
        """带有健康度检查的每日定时重置任务"""
        await asyncio.sleep(10)
        while True:
            if self.is_running and self.auto_reset_enable and self.auto_reset_time:
                try:
                    now = datetime.datetime.now()
                    current_time_str = now.strftime("%H:%M")
                    current_date_str = now.strftime("%Y-%m-%d")
                    
                    if current_time_str == self.auto_reset_time and self.last_reset_date != current_date_str:
                        logger.info(f"[Steam模组监控] 触发每日定时任务 ({self.auto_reset_time})")
                        self.last_reset_date = current_date_str # 记录防止死循环
                        
                        # 核心拦截逻辑：配置了IP就必须先过安检
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
                                continue # 跳过后续的重置逻辑
                        
                        # 安检通过（或未配置IP），执行常规重置
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
