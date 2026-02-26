import asyncio
import aiohttp
import json
import re
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

@register("steam_mod_monitor", "YourName", "全自动 Steam 模组监控插件", "2.1.0")
class SteamModMonitor(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        self.push_group_id = self.config.get("push_group_id", "").strip()
        self.mod_ids_raw = self.config.get("mod_ids", "")
        self.poll_interval = self.config.get("poll_interval_minutes", 30)
        self.steam_api_base = self.config.get("steam_api_base", "https://api.steampowered.com").rstrip('/')
        self.steam_api_key = self.config.get("steam_api_key", "")
        self.push_template = self.config.get("push_template", "🚨 模组【{mod_name}】已更新！")
        
        self.mod_ids = [m.strip() for m in re.split(r'[,;]', self.mod_ids_raw) if m.strip()]
        self.last_update_times = {} 
        
        self.is_running = bool(self.push_group_id and self.mod_ids)
        
        if self.is_running:
            logger.info(f"[Steam模组监控] 插件已启动！目标群:{self.push_group_id}，共载入 {len(self.mod_ids)} 个模组。")
        else:
            logger.warning("[Steam模组监控] 启动挂起：未配置推送群号或模组列表为空。")

        self.monitor_task = asyncio.create_task(self.monitor_loop())

    @filter.command("steammod on")
    async def start_monitor(self, event: AstrMessageEvent):
        """群内手动开启指令"""
        if not self.push_group_id:
            yield event.plain_result("❌ 启动失败：请先在后台配置页面设置好 [推送群号]！")
            return
            
        self.is_running = True
        logger.info("[Steam模组监控] 管理员手动开启了轮询。")
        yield event.plain_result(f"✅ 监控已开启！正在对 {len(self.mod_ids)} 个模组进行高强度巡视。")
        await self.check_steam_updates()

    @filter.command("steammod off")
    async def stop_monitor(self, event: AstrMessageEvent):
        """群内手动关闭指令"""
        self.is_running = False
        logger.info("[Steam模组监控] 管理员手动暂停了轮询。")
        yield event.plain_result("🛑 监控已暂停！后台轮询已停止。")

    async def monitor_loop(self):
        """核心后台轮询死循环"""
        await asyncio.sleep(5) 
        
        while True:
            if self.is_running:
                logger.info(f"[Steam模组监控] 开始向 Steam 获取 {len(self.mod_ids)} 个模组的最新状态...")
                try:
                    await self.check_steam_updates()
                except Exception as e:
                    # 使用 repr(e) 打印具体的异常类，方便排错网络问题
                    logger.error(f"[Steam模组监控] 网络请求异常: {repr(e)}")
            
            await asyncio.sleep(self.poll_interval * 60)

    async def check_steam_updates(self):
        """核心查表函数"""
        url = f"{self.steam_api_base}/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
        data = {"itemcount": len(self.mod_ids)}
        for i, mod_id in enumerate(self.mod_ids):
            data[f"publishedfileids[{i}]"] = mod_id
            
        if self.steam_api_key:
            data["key"] = self.steam_api_key

        # 加入 30 秒超时保护，防止网络死锁
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, data=data) as response:
                if response.status != 200:
                    logger.error(f"[Steam模组监控] Steam 接口返回失败代码: {response.status}")
                    return
                    
                result = await response.json()
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
                        updated_count += 1
                        await self.push_alert(mod_id, mod_name)
                        
                logger.info(f"[Steam模组监控] 本轮检查完毕，共发现 {updated_count} 个模组有更新。")

    async def push_alert(self, mod_id: str, mod_name: str):
        """推送到指定的 QQ 群"""
        msg_text = self.push_template.replace("{mod_name}", mod_name).replace("{mod_id}", mod_id)
        try:
            await self.context.send_message(self.push_group_id, msg_text)
        except Exception as e:
            logger.error(f"[Steam模组监控] 消息推送到群 {self.push_group_id} 失败: {repr(e)}")
