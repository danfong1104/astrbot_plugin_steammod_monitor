import asyncio
import aiohttp
import json
import re
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

@register("pz_mod_monitor", "YourName", "全自动 Steam 模组监控插件", "2.0.0")
class PZModMonitor(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        # 读取 Web 后台配置
        self.push_group_id = self.config.get("push_group_id", "").strip()
        self.mod_ids_raw = self.config.get("mod_ids", "")
        self.poll_interval = self.config.get("poll_interval_minutes", 30)
        self.steam_api_base = self.config.get("steam_api_base", "https://api.steampowered.com").rstrip('/')
        self.steam_api_key = self.config.get("steam_api_key", "")
        self.push_template = self.config.get("push_template", "🚨 模组【{mod_name}】已更新！")
        
        # 利用正则表达式，同时兼容分号(;)和逗号(,)作为分隔符
        self.mod_ids = [m.strip() for m in re.split(r'[,;]', self.mod_ids_raw) if m.strip()]
        self.last_update_times = {} 
        
        # 控制轮询开关。只要填了群号，默认开机自动运行
        self.is_running = bool(self.push_group_id and self.mod_ids)
        
        if self.is_running:
            self.context.logger.info(f"[PZ模组监控] 插件已启动！目标群:{self.push_group_id}，共载入 {len(self.mod_ids)} 个模组。")
        else:
            self.context.logger.warning("[PZ模组监控] 启动挂起：未配置推送群号或模组列表为空。")

        # 挂载后台异步轮询任务
        self.monitor_task = asyncio.create_task(self.monitor_loop())

    @filter.command("steammod on")
    async def start_monitor(self, event: AstrMessageEvent):
        """群内手动开启指令"""
        if not self.push_group_id:
            yield event.plain_result("❌ 启动失败：请先在后台配置页面设置好 [推送目标 QQ 群号]！")
            return
            
        self.is_running = True
        self.context.logger.info("[PZ模组监控] 管理员手动开启了轮询。")
        yield event.plain_result(f"✅ 监控已开启！正在对 {len(self.mod_ids)} 个模组进行高强度巡视。")
        # 开启后立即执行一次检查
        await self.check_steam_updates()

    @filter.command("steammod off")
    async def stop_monitor(self, event: AstrMessageEvent):
        """群内手动关闭指令"""
        self.is_running = False
        self.context.logger.info("[PZ模组监控] 管理员手动暂停了轮询。")
        yield event.plain_result("🛑 监控已暂停！后台轮询已停止。")

    async def monitor_loop(self):
        """核心后台轮询死循环"""
        await asyncio.sleep(5) # 等待框架初始化
        
        while True:
            if self.is_running:
                self.context.logger.info(f"[PZ模组监控] 开始向 Steam 获取 {len(self.mod_ids)} 个模组的最新状态...")
                try:
                    await self.check_steam_updates()
                except Exception as e:
                    self.context.logger.error(f"[PZ模组监控] 网络请求异常: {e}")
            
            # 等待设定的轮询周期
            await asyncio.sleep(self.poll_interval * 60)

    async def check_steam_updates(self):
        """核心查表函数"""
        url = f"{self.steam_api_base}/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
        data = {"itemcount": len(self.mod_ids)}
        for i, mod_id in enumerate(self.mod_ids):
            data[f"publishedfileids[{i}]"] = mod_id
            
        if self.steam_api_key:
            data["key"] = self.steam_api_key

        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data) as response:
                if response.status != 200:
                    self.context.logger.error(f"[PZ模组监控] Steam 接口返回失败代码: {response.status}")
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
                        
                    # 首次抓取，只建档记录，不报警
                    if mod_id not in self.last_update_times:
                        self.last_update_times[mod_id] = current_time
                        continue
                        
                    # 对比时间戳，发现更新
                    if current_time > self.last_update_times[mod_id]:
                        self.context.logger.info(f"[PZ模组监控] 🚨 发现更新！模组: {mod_name}")
                        self.last_update_times[mod_id] = current_time
                        updated_count += 1
                        await self.push_alert(mod_id, mod_name)
                        
                self.context.logger.info(f"[PZ模组监控] 本轮检查完毕，共发现 {updated_count} 个模组有更新。")

    async def push_alert(self, mod_id: str, mod_name: str):
        """推送到指定的 QQ 群"""
        msg_text = self.push_template.replace("{mod_name}", mod_name).replace("{mod_id}", mod_id)
        try:
            # 兼容老版本与新版本的发送逻辑
            await self.context.send_message(self.push_group_id, msg_text)
        except Exception as e:
            self.context.logger.error(f"[PZ模组监控] 消息推送到群 {self.push_group_id} 失败: {e}")
