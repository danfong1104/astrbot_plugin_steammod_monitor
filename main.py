import asyncio
import aiohttp
import re
import os
import json
import datetime
import struct
from pathlib import Path

# 尝试导入异步 SSH 库，为 LinuxGSM 模式提供底层支持
try:
    import asyncssh
    HAS_ASYNCSSH = True
except ImportError:
    HAS_ASYNCSSH = False

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


@register("steam_mod_monitor", "YourName", "Steam 创意工坊管家与游戏服 RCON 控制核心", "5.7.0")
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
        
        # 双模执行配置
        self.restart_method = str(self.config.get("restart_method", "rcon")).strip().lower()
        self.ssh_host = str(self.config.get("ssh_host", "")).strip()
        self.ssh_port = int(self.config.get("ssh_port", 22))
        self.ssh_user = str(self.config.get("ssh_user", "steam")).strip()
        self.ssh_password = str(self.config.get("ssh_password", "")).strip()

        # 自定义指令注入点
        self.cmd_rcon_save = str(self.config.get("cmd_rcon_save", "save")).strip()
        self.cmd_rcon_quit = str(self.config.get("cmd_rcon_quit", "quit")).strip()
        self.cmd_ssh_restart = str(self.config.get("cmd_ssh_restart", "cd /home/steam && ./pzserver restart")).strip()
        
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
        if not self.global_bot:
            self.global_bot = event.bot

    async def send_alert(self, msg: str):
        if not self.push_group_id:
            return
        if self.push_group_id.isdigit() and self.global_bot:
            try:
                group_id_int = int(self.push_group_id)
                await asyncio.wait_for(
                    self.global_bot.api.call_action("send_group_msg", group_id=group_id_int, message=str(msg)),
                    timeout=self.SEND_TIMEOUT
                )
                return
            except Exception as e:
                pass

        try:
            chain = MessageChain().message(msg)
            await self.context.send_message(self.push_group_id, chain)
        except Exception as ex:
            pass

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

    async def get_online_players_details(self) -> tuple[int, list]:
        """获取在线玩家人数和名单详情"""
        if not self.server_ip or not self.server_port or not self.server_rcon_password:
            return -1, []
        rcon = AsyncRCON(self.server_ip, self.server_port, self.server_rcon_password)
        try:
            await rcon.connect()
            resp = await rcon.execute("players")
            await rcon.close()
            
            lines = resp.split('\n')
            players = []
            for line in lines:
                line = line.strip()
                if line.startswith('- '):
                    players.append(line[2:].strip())
                    
            match = re.search(r'Players connected \((\d+)\)', resp)
            count = int(match.group(1)) if match else len(players)
            return count, players
        except Exception as e:
            return -1, []

    async def get_online_players(self) -> int:
        count, _ = await self.get_online_players_details()
        return count

    async def verify_server_health(self, silent_success=False):
        wait_seconds = self.server_restart_wait_minutes * 60
        await asyncio.sleep(wait_seconds) 
        
        is_online = await self.tcp_ping(self.server_ip, self.server_port)
        if not is_online:
            await asyncio.sleep(HEALTH_CHECK_RETRY_INTERVAL) 
            is_online = await self.tcp_ping(self.server_ip, self.server_port)
            
        async with self.state_lock:
            self.is_restarting = False
            if is_online:
                self.pending_updates.clear() 
                self.broadcast_sent_for_current_updates = False
                await self._save_data()

        if is_online:
            if not silent_success:
                success_msg = (
                    f"✅ 【服务器已重启成功】\n"
                    f"服务器连通性检测正常！最新模组已加载完毕。\n"
                    f"🎮 大家可以进入游戏游玩啦！"
                )
                await self.send_alert(success_msg)
        else:
            fail_msg = (
                f"❌ 【服务器宕机严重告警】\n"
                f"重启后未能正常恢复开服！TCP 端口被拒绝。\n"
                f"🛠️ 请立刻检查容器或服务端崩溃日志！"
            )
            await self.send_alert(fail_msg)

    @filter.command("steammod")
    async def handle_steammod(self, event: AstrMessageEvent, action: str = ""):
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
                
            yield event.plain_result(f"⚡ 收到指令！模式：[{self.restart_method}]。已下发【{self.server_rcon_manual_countdown}秒】广播...")
            asyncio.create_task(self.execute_rcon_restart(is_auto=False))
            
        elif action == "check":
            yield event.plain_result("🔍 正在为您生成【服务器全景运行体检报告】，由于需要抓取底层物理数据，请稍候约5秒...")
            async for msg in self.generate_check_report(event):
                yield msg
            
        else:
            yield event.plain_result(f"🔍 核对 {len(self.mod_ids)} 个模组最后更新时间，请稍候...")
            async for msg in self.manual_status_check(event):
                yield msg

    async def generate_check_report(self, event: AstrMessageEvent):
        """生成精美的全景体检报告"""
        
        # 1. 抓取游戏服务端数据 (RCON & Ping)
        is_online = await self.tcp_ping(self.server_ip, self.server_port) if self.server_ip else False
        p_count, p_list = await self.get_online_players_details()
        
        game_status = "✅ 运行中 (端口通畅)" if is_online else "❌ 离线 / 端口被拒"
        player_info = f"{p_count} 人" if p_count >= 0 else "未知 (RCON未连接)"
        player_names = ", ".join(p_list) if p_list else ("当前无幸存者在线" if p_count == 0 else "无法获取名单")
        
        # 2. 抓取宿主机底层数据 (SSH)
        ssh_status = "❌ 配置不全或连接失败"
        uptime_str, cpu_str, ram_str, disk_str = "未知", "未知", "未知", "未知"
        backup_status_str = "❌ 探测失败"
        
        if HAS_ASYNCSSH and self.ssh_host:
            # 极其精简巧妙的组合探测命令
            probe_cmd = """
            echo "---SYS---"
            uptime -p
            top -bn1 | grep "Cpu(s)" | awk '{print $2 + $4}'
            free -m | awk 'NR==2{printf "%s/%s MB (%.1f%%)", $3,$2,$3*100/$2 }'
            df -h /home/steam | awk 'NR==2{print $4 " 可用 / " $2 " 总量 (" $5 " 已用)"}'
            echo "---BAK---"
            stat -c "%y|%s|%n" $(ls -t /home/steam/pz_core_backup_*.tar.gz 2>/dev/null | head -n 1) 2>/dev/null || echo "NOT_FOUND"
            """
            try:
                async with asyncssh.connect(self.ssh_host, port=self.ssh_port, username=self.ssh_user, password=self.ssh_password, known_hosts=None) as conn:
                    result = await conn.run(probe_cmd, check=False)
                    if result.exit_status == 0:
                        ssh_status = "✅ 连接正常"
                        out = result.stdout.strip()
                        if "---SYS---" in out and "---BAK---" in out:
                            parts = out.split("---BAK---")
                            sys_lines = [line.strip() for line in parts[0].split("---SYS---")[1].strip().split('\n') if line.strip()]
                            bak_line = parts[1].strip()
                            
                            if len(sys_lines) >= 4:
                                uptime_str = sys_lines[0]
                                cpu_str = sys_lines[1] + "%"
                                ram_str = sys_lines[2]
                                disk_str = sys_lines[3]
                                
                            if bak_line == "NOT_FOUND":
                                backup_status_str = "⚠️ 未找到任何备份文件"
                            else:
                                # 解析：2026-03-02 05:00:00.000000000 +0800|154012|/home/steam/...tar.gz
                                bak_parts = bak_line.split('|')
                                if len(bak_parts) >= 3:
                                    date_str = bak_parts[0].split('.')[0] # 截取掉毫秒
                                    size_mb = round(int(bak_parts[1]) / (1024 * 1024), 2)
                                    file_name = bak_parts[2].split('/')[-1]
                                    
                                    # 检查是否是今天生成的
                                    now_date = datetime.datetime.now().strftime("%Y-%m-%d")
                                    is_today = now_date in date_str
                                    
                                    icon = "✅ 备份任务正常！" if is_today else "⚠️ 警告：最新备份不是今日生成的！"
                                    backup_status_str = f"{icon}\n🕒 最新创建：{date_str}\n📦 档案大小：{size_mb} MB\n📁 档案名称：{file_name}"

            except Exception as e:
                ssh_status = f"❌ SSH连接异常 ({type(e).__name__})"

        # 3. 抓取管家插件数据
        mod_status = f"🔴 有 {len(self.pending_updates)} 个模组等待更新！" if self.pending_updates else "🟢 所有模组均是最新版本"
        auto_check = f"已开启 (每日 {self.auto_reset_time})" if self.auto_reset_enable else "已关闭"

        # 4. 组装终极报告
        report = (
            "📊【服务器全景运行体检报告】\n"
            "==========================\n"
            "🖥️ 宿主机 (Ubuntu) 物理状态\n"
            f"🔌 SSH状态：{ssh_status}\n"
            f"⏱️ 运行时间：{uptime_str}\n"
            f"🧠 CPU 负载：{cpu_str}\n"
            f"💽 内存占用：{ram_str}\n"
            f"💾 磁盘空间：{disk_str}\n\n"
            
            "💾 核心数据备份状态\n"
            f"{backup_status_str}\n\n"
            
            "🧟 游戏服务端 (Project Zomboid)\n"
            f"🎮 进程状态：{game_status}\n"
            f"👥 在线人数：{player_info}\n"
            f"📋 玩家列表：{player_names}\n\n"
            
            "🛠️ Steam 模组管家状态\n"
            f"👁️ 巡视目标：{len(self.mod_ids)} 个模组\n"
            f"🚨 更新预警：{mod_status}\n"
            f"⏰ 定时安检：{auto_check}\n"
            "=========================="
        )
        
        bot_id = str(event.get_self_id()) if hasattr(event, 'get_self_id') else "10000"
        yield event.chain_result([Node(uin=bot_id, custom_name="Steam管家中控面板", content=[Plain(report)])])

    # ================= 分发引擎 =================
    async def execute_rcon_restart(self, is_auto=True):
        if self.restart_method == "linuxgsm":
            await self._execute_linuxgsm_restart(is_auto)
        else:
            await self._execute_native_rcon_restart(is_auto)

    # 方案 A：纯血 RCON 断电模式
    async def _execute_native_rcon_restart(self, is_auto):
        rcon = AsyncRCON(self.server_ip, self.server_port, self.server_rcon_password)
        try:
            await rcon.connect()
            if is_auto:
                await asyncio.sleep(self.server_rcon_countdown)
            else:
                clean_msg = self.server_rcon_manual_broadcast.replace('"', "'")
                await rcon.execute(f'servermsg "{clean_msg}"')
                await asyncio.sleep(self.server_rcon_manual_countdown)
                
            if self.cmd_rcon_save:
                await rcon.execute(self.cmd_rcon_save)
                await asyncio.sleep(SAVE_DELAY) 
            if self.cmd_rcon_quit:
                await rcon.execute(self.cmd_rcon_quit)
                
            await rcon.close()
            asyncio.create_task(self.verify_server_health(silent_success=False))
            
        except Exception as e:
            async with self.state_lock:
                self.is_restarting = False
            await self.send_alert(f"❌ RCON 触发失败，请人工检查。报错: {type(e).__name__}")
            if rcon: await rcon.close()

    # 方案 B：LinuxGSM SSH 拉起模式
    async def _execute_linuxgsm_restart(self, is_auto):
        if not HAS_ASYNCSSH:
            msg = "❌ 缺少 asyncssh 库，无法使用 LinuxGSM 模式。\n请进入 AstrBot 终端执行 pip install asyncssh"
            await self.send_alert(msg)
            async with self.state_lock: self.is_restarting = False
            return

        rcon = AsyncRCON(self.server_ip, self.server_port, self.server_rcon_password)
        try:
            await rcon.connect()
            if not is_auto:
                clean_msg = self.server_rcon_manual_broadcast.replace('"', "'")
                await rcon.execute(f'servermsg "{clean_msg}"')
                await asyncio.sleep(self.server_rcon_manual_countdown)
            else:
                await asyncio.sleep(self.server_rcon_countdown)
            await rcon.close()
        except Exception as e:
            if rcon: await rcon.close()

        try:
            async with asyncssh.connect(self.ssh_host, port=self.ssh_port, username=self.ssh_user, password=self.ssh_password, known_hosts=None) as conn:
                result = await conn.run(self.cmd_ssh_restart, check=False, term_type='xterm')
                
                if result.exit_status != 0:
                    stdout_str = str(result.stdout) if result.stdout else "无输出"
                    stderr_str = str(result.stderr) if result.stderr else "无报错信息"
                    await self.send_alert(f"❌ SSH 指令执行异常！\n详细输出: {stdout_str[:200]}\n报错: {stderr_str[:200]}")
                
                asyncio.create_task(self.verify_server_health(silent_success=False))
                
        except Exception as e:
             await self.send_alert(f"❌ SSH 调用失败: {e}")
             async with self.state_lock: self.is_restarting = False

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
                    pass
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
                    pass

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
                    if rcon: await rcon.close()
