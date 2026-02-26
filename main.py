import asyncio
import aiohttp
import json
import re
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Node, Plain

@register("steam_mod_monitor", "YourName", "全自动 Steam 模组监控插件", "3.2.0")
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
        
        self.mod_ids = [m.strip() for m in re.split(r'[,;]', self.mod_ids_raw) if m.strip()]
        self.last_update_times = {} 
        self.pending_updates = set() 
        self.is_running = bool(self.push_group_id and self.mod_ids)
        
        # 清理旧的僵尸任务
        for task in asyncio.all_tasks():
            if task.get_name() == "steam_mod_monitor_loop_task":
                task.cancel()
                logger.info("[Steam模组监控] 🧹 已清理上次重载遗留的旧后台任务。")

        if self.is_running:
            logger.info(f"[Steam模组监控] 插件已启动！目标群:{self.push_group_id}，共载入 {len(self.mod_ids)} 个模组。")
        else:
            logger.warning("[Steam模组监控] 启动挂起：未配置推送群号或模组列表为空。")

        self.monitor_task = asyncio.create_task(self.monitor_loop(), name="steam_mod_monitor_loop_task")

    @filter.command("steammod")
    async def handle_steammod(self, event: AstrMessageEvent, action: str = ""):
        """统一指令入口分发"""
        action = action.strip().lower()
        
        if action == "on":
            if not self.push_group_id:
                yield event.plain_result("❌ 启动失败：请先在后台配置页面设置好 [推送群号]！")
                return
            self.is_running = True
            logger.info("[Steam模组监控] 管理员手动开启了轮询。")
            yield event.plain_result(f"✅ 监控已开启！正在对 {len(self.mod_ids)} 个模组进行高强度巡视。")
            await self.check_steam_updates_with_retry()
            
        elif action == "off":
            self.is_running = False
            logger.info("[Steam模组监控] 管理员手动暂停了轮询。")
            yield event.plain_result("🛑 监控已暂停！后台轮询已停止。")
            
        elif action == "reset":
            self.pending_updates.clear()
            yield event.plain_result("✅ 状态已重置！所有红灯已清除，目前全库显示为最新（绿灯）状态。")
            
        else:
            if not self.mod_ids:
                yield event.plain_result("❌ 模组列表为空，请先在控制台配置。")
                return
                
            yield event.plain_result(f"🔍 正在向 Steam 校验 {len(self.mod_ids)} 个模组的最新状态，请稍候...")
            async for msg in self.manual_status_check(event):
                yield msg

    async def manual_status_check(self, event: AstrMessageEvent):
        """手动获取状态，并使用【合并转发】发送明细"""
        url = f"{self.steam_api_base}/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
        data = {"itemcount": len(self.mod_ids)}
        for i, mod_id in enumerate(self.mod_ids):
            data[f"publishedfileids[{i}]"] = mod_id
        if self.steam_api_key:
            data["key"] = self.steam_api_key

        try:
            timeout = aiohttp.ClientTimeout(total=30)
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
                        if not mod_id:
                            continue
                        
                        if mod_id in self.pending_updates:
                            lines.append(f"🔴 {mod_name} ({mod_id}) - 需更新")
                            needs_update_count += 1
                        else:
                            lines.append(f"🟢 {mod_name} ({mod_id})")
                    
                    # 1. 构建长文本明细
                    long_text = "【当前服务器模组健康状态明细】\n\n" + "\n".join(lines)
                    
                    # 2. 获取机器人自身的 QQ 号 (【关键修复点】)
                    try:
                        bot_id = str(event.get_self_id())
                    except Exception:
                        bot_id = "10000" # 兜底防错
                    
                    # 3. 包装成一个“合并转发”节点 (Node)
                    forward_node = Node(
                        uin=bot_id,
                        custom_name="Steam模组管家",
                        content=[Plain(long_text)]
                    )
                    
                    # 发送合并转发卡片
                    yield event.chain_result([forward_node])
                    
                    # 停顿 0.5 秒，防止消息顺序错乱
                    await asyncio.sleep(0.5)
                    
                    # 4. 单独发送统计总结
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
                try:
                    await self.check_steam_updates_with_retry()
                except asyncio.CancelledError:
                    break 
                except Exception as e:
                    logger.error(f"[Steam模组监控] 未知异常: {repr(e)}")
            await asyncio.sleep(self.poll_interval * 60)

    async def check_steam_updates_with_retry(self):
        url = f"{self.steam_api_base}/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
        data = {"itemcount": len(self.mod_ids)}
        for i, mod_id in enumerate(self.mod_ids):
            data[f"publishedfileids[{i}]"] = mod_id
        if self.steam_api_key:
            data["key"] = self.steam_api_key

        timeout = aiohttp.ClientTimeout(total=30)
        for attempt in range(1, self.max_retries + 1):
            try:
                async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                    async with session.post(url, data=data) as response:
                        if response.status != 200:
                            logger.error(f"[Steam模组监控] 接口报错: {response.status} (第{attempt}次)")
                            if attempt < self.max_retries:
                                await asyncio.sleep(5)
                                continue
                            return
                        result = await response.json()
                        await self.process_result(result)
                        return 
            except Exception as e:
                if attempt < self.max_retries:
                    logger.warning(f"[Steam模组监控] ⚠️ 网络异常，重试 {attempt}/{self.max_retries}...")
                    await asyncio.sleep(5) 
                else:
                    logger.error(f"[Steam模组监控] ❌ 已达最大重试次数！最终报错: {repr(e)}")

    async def process_result(self, result):
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
