import asyncio
import aiohttp
import time
import json
import os
from astrbot.api.all import *

@register("pz_mod_monitor", "YourName", "监控Steam创意工坊模组更新并在群内播报", "1.0.0")
class PZModMonitor(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        # 从后台配置界面读取数据
        self.mod_ids_raw = self.config.get("mod_ids", "")
        self.poll_interval = self.config.get("poll_interval_minutes", 30)
        self.steam_api_key = self.config.get("steam_api_key", "")
        self.push_template = self.config.get("push_template", "🚨 模组【{mod_name}】已更新！")
        
        # 内部状态记录
        self.mod_ids = [m.strip() for m in self.mod_ids_raw.split(",") if m.strip()]
        self.last_update_times = {} # 记录每个模组最新时间戳: { "3077900375": 1700000000 }
        self.subscribe_groups = set() # 记录哪些群订阅了通知
        
        # 本地持久化文件，用来保存订阅了通知的群
        self.data_file = "data/pz_monitor_subs.json"
        self._load_subs()

        # 启动后台轮询任务
        self.monitor_task = asyncio.create_task(self.monitor_loop())

    def _load_subs(self):
        """加载订阅群组数据"""
        if os.path.exists(self.data_file):
            with open(self.data_file, "r", encoding="utf-8") as f:
                self.subscribe_groups = set(json.load(f))

    def _save_subs(self):
        """保存订阅群组数据"""
        with open(self.data_file, "w", encoding="utf-8") as f:
            json.dump(list(self.subscribe_groups), f)

    @filter.command("开启模组监控")
    async def subscribe_monitor(self, event: MessageEvent):
        """群内指令：绑定当前群作为推送目标"""
        session_id = event.message_obj.group_id or event.message_obj.sender_id
        if not session_id:
            await event.reply("获取群组ID失败，请在群聊中使用该命令。")
            return
            
        self.subscribe_groups.add(session_id)
        self._save_subs()
        await event.reply(f"✅ 当前群已成功绑定！\n监控名单共 {len(self.mod_ids)} 个模组，每 {self.poll_interval} 分钟轮询一次 Steam 状态。")

    async def monitor_loop(self):
        """核心后台轮询循环"""
        await asyncio.sleep(10) # 延迟启动，等机器人完全初始化
        
        while True:
            if self.mod_ids and self.subscribe_groups:
                try:
                    await self.check_steam_updates()
                except Exception as e:
                    self.context.logger.error(f"[PZ模组监控] 轮询出错: {e}")
            
            # 等待设定的轮询周期（分钟转换为秒）
            await asyncio.sleep(self.poll_interval * 60)

    async def check_steam_updates(self):
        """向 Steam API 发送请求并对比时间戳"""
        url = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
        data = {"itemcount": len(self.mod_ids)}
        for i, mod_id in enumerate(self.mod_ids):
            data[f"publishedfileids[{i}]"] = mod_id
            
        # 优先使用 API Key 可以防止被限流
        if self.steam_api_key:
            data["key"] = self.steam_api_key

        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data) as response:
                if response.status != 200:
                    self.context.logger.error(f"[PZ模组监控] Steam API 响应错误: {response.status}")
                    return
                    
                result = await response.json()
                items = result.get('response', {}).get('publishedfiledetails', [])
                
                for item in items:
                    mod_id = str(item.get('publishedfileid', ''))
                    mod_name = item.get('title', '未知模组')
                    current_time = item.get('time_updated', 0)
                    
                    if not mod_id or current_time == 0:
                        continue
                        
                    # 第一次抓取，只记录时间戳，不推送
                    if mod_id not in self.last_update_times:
                        self.last_update_times[mod_id] = current_time
                        continue
                        
                    # 发现时间戳变大，说明更新了！
                    if current_time > self.last_update_times[mod_id]:
                        self.last_update_times[mod_id] = current_time
                        await self.push_alert(mod_id, mod_name)

    async def push_alert(self, mod_id: str, mod_name: str):
        """向所有订阅的群组发送告警文本"""
        # 替换配置界面里的占位符
        msg_text = self.push_template.replace("{mod_name}", mod_name).replace("{mod_id}", mod_id)
        
        for group_id in self.subscribe_groups:
            # 这里调用了 AstrBot 的通用推送接口
            await self.context.send_message(group_id, msg_text)
