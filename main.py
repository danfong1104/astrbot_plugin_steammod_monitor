import asyncio
import io
import json
import os
import random
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from PIL import Image as PILImage, ImageDraw, ImageFont
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain, Image
from astrbot.api.star import Context, Star, StarTools

# 渲染配置
BG_COLOR_TOP = (49, 80, 66)
BG_COLOR_BOTTOM = (28, 35, 44)
AVATAR_SIZE = 80
COVER_W, COVER_H = 80, 120
IMG_W, IMG_H = 512, 192


class SteamLibraryMonitor(Star):
    """Steam游戏库监控插件，监控好友游戏库变动并通知新购买的游戏。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir: Path = StarTools.get_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # API配置
        self.steam_api_key: str = config.get("steam_api_key", "")
        self.itad_api_key: str = config.get("itad_api_key", "")
        self.sgdb_api_key: str = config.get("sgdb_api_key", "")
        self.poll_interval: int = config.get("poll_interval", 30)
        self.enable_notification: bool = config.get("enable_notification", True)
        self.render_image: bool = config.get("render_image", True)
        self.show_game_info: bool = config.get("show_game_info", True)

        # 消息模板
        self.message_template: str = config.get("message_template", "恭喜 {username} 新入库了 {gamename}")

        # 购买评价配置
        self.comment_at_lowest: str = config.get("comment_at_lowest", "绝佳的买入时机，薅羊毛小能手！")
        self.comment_above_lowest: str = config.get("comment_above_lowest", "有点冤大头了呢！为什么不再等等。")

        # ITAD API基础地址
        self.itad_api_base: str = "https://api.isthereanydeal.com"

        # 从配置中解析Steam ID列表（文本格式，每行一个）
        self.steam_ids_config: list[dict] = self._parse_steam_ids(config.get("steam_ids", ""))
        # 从配置中解析推送群号列表（文本格式，每行一个）
        self.notify_groups: list[str] = self._parse_groups(config.get("notify_groups", ""))

        # 数据文件路径
        self.games_cache_file: Path = self.data_dir / "games_cache.json"

        # 加载数据
        self.games_cache: dict[str, list[int]] = self._load_json(self.games_cache_file, {})

        # 轮询任务
        self._poll_task: Optional[asyncio.Task] = None
        self._client: Optional[httpx.AsyncClient] = None

        # 字体路径
        self.fonts_dir: Path = Path(__file__).parent / "fonts"

        # 记录最后检查时间
        self.last_check_time: Optional[datetime] = None

        # 缓存平台适配器 ID（首次使用时动态获取）
        self._cached_adapter_id: Optional[str] = None

        # 启动轮询
        self._start_polling()

    def _parse_steam_ids(self, text: str) -> list[dict]:
        """解析Steam ID配置文本。

        格式：每行一个，支持 "steam_id:nickname" 或只写 "steam_id"
        """
        result = []
        if not text or not text.strip():
            return result

        for line in text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue

            parts = line.split(":", 1)
            steam_id = parts[0].strip()
            nickname = parts[1].strip() if len(parts) > 1 else ""

            if steam_id:
                result.append({"steam_id": steam_id, "nickname": nickname})

        return result

    def _parse_groups(self, text: str) -> list[str]:
        """解析群号配置文本。

        格式：每行一个群号
        """
        result = []
        if not text or not text.strip():
            return result

        for line in text.strip().split("\n"):
            line = line.strip()
            if line:
                result.append(line)

        return result

    def _load_json(self, path: Path, default):
        """从JSON文件加载数据。"""
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"加载 {path} 失败: {e}")
        return default

    def _save_json(self, path: Path, data):
        """保存数据到JSON文件。"""
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存 {path} 失败: {e}")

    async def _get_adapter_id(self) -> Optional[str]:
        """获取平台适配器 ID，优先 AIOCQHTTP（QQ），备用其他平台。"""
        # 已缓存直接返回
        if self._cached_adapter_id:
            return self._cached_adapter_id

        # 按优先级尝试各个平台适配器
        for adapter_type in [
            filter.PlatformAdapterType.AIOCQHTTP,    # QQ (NapCat/LLOneBot)
            filter.PlatformAdapterType.QQOFFICIAL,   # QQ 官方机器人
            filter.PlatformAdapterType.DINGTALK,     # 钉钉
            filter.PlatformAdapterType.WECOM,        # 企业微信
            filter.PlatformAdapterType.LARK,         # 飞书
        ]:
            try:
                platform = self.context.get_platform(adapter_type)
                if platform:
                    meta = platform.meta()
                    if meta and meta.id:
                        self._cached_adapter_id = meta.id
                        logger.info(f"[Steam游戏库监控] 检测到平台适配器: {meta.id}")
                        return self._cached_adapter_id
            except Exception:
                continue

        logger.error("[Steam游戏库监控] ❌ 未找到任何可用平台适配器")
        logger.error("[Steam游戏库监控] 请确保已配置并启用至少一个消息平台（如 aiocqhttp）")
        return None

    def _build_group_umo(self, adapter_id: str, group_id: str) -> str:
        """根据 adapter_id 和群号构建 UMO 字符串。"""
        return f"{adapter_id}:GroupMessage:{group_id}"

    def _start_polling(self):
        """启动轮询任务。"""
        logger.info("=" * 50)
        logger.info("[Steam游戏库监控] 插件启动中...")

        if not self.steam_api_key:
            logger.warning("[Steam游戏库监控] Steam API Key 未配置，轮询任务未启动")
            return

        if not self.steam_ids_config:
            logger.warning("[Steam游戏库监控] 未配置监控的Steam ID，轮询任务未启动")
            return

        # 输出监控配置信息
        logger.info(f"[Steam游戏库监控] 监控用户列表 ({len(self.steam_ids_config)} 人):")
        for i, friend in enumerate(self.steam_ids_config, 1):
            steam_id = friend.get("steam_id", "")
            nickname = friend.get("nickname", "") or "未设置昵称"
            logger.info(f"  {i}. {nickname} (ID: {steam_id})")

        logger.info(f"[Steam游戏库监控] 推送群号列表 ({len(self.notify_groups)} 个):")
        if self.notify_groups:
            for group_id in self.notify_groups:
                logger.info(f"  - 群 {group_id}")
        else:
            logger.info("  - 未配置推送群号")

        # 计算下次轮询时间
        next_check = datetime.now()
        logger.info(f"[Steam游戏库监控] 轮询间隔: {self.poll_interval} 分钟")
        logger.info(f"[Steam游戏库监控] 图片渲染: {'启用' if self.render_image else '禁用'}")
        logger.info(f"[Steam游戏库监控] 通知推送: {'启用' if self.enable_notification else '禁用'}")
        logger.info(f"[Steam游戏库监控] ITAD API: {'已配置' if self.itad_api_key else '未配置（无法获取史低价格）'}")
        logger.info("=" * 50)

        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("[Steam游戏库监控] 轮询任务已启动")

    async def _poll_loop(self):
        """轮询循环。"""
        while True:
            now = datetime.now()
            logger.info(f"[Steam游戏库监控] 开始轮询检查 - {now.strftime('%Y-%m-%d %H:%M:%S')}")

            try:
                await self._check_all_friends()
                self.last_check_time = now
                logger.info(f"[Steam游戏库监控] 轮询检查完成")
            except Exception as e:
                logger.error(f"[Steam游戏库监控] 轮询检查失败: {e}")

            # 计算下次轮询时间
            next_time = datetime.now().timestamp() + self.poll_interval * 60
            next_check_str = datetime.fromtimestamp(next_time).strftime('%Y-%m-%d %H:%M:%S')
            logger.info(f"[Steam游戏库监控] 下次轮询时间: {next_check_str}")

            await asyncio.sleep(self.poll_interval * 60)

    async def _get_client(self) -> httpx.AsyncClient:
        """获取或创建HTTP客户端。"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def _get_owned_games(self, steam_id: str) -> Optional[list[dict]]:
        """获取用户拥有的所有游戏。"""
        client = await self._get_client()
        url = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
        params = {
            "key": self.steam_api_key,
            "steamid": steam_id,
            "format": "json",
            "include_appinfo": 1,
            "include_played_free_games": 1,
        }

        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            return data.get("response", {}).get("games", [])
        except Exception as e:
            logger.error(f"获取 {steam_id} 的游戏列表失败: {e}")
            return None

    async def _get_player_summary(self, steam_id: str) -> Optional[dict]:
        """获取玩家基本信息。"""
        client = await self._get_client()
        url = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
        params = {
            "key": self.steam_api_key,
            "steamids": steam_id,
            "format": "json",
        }

        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            players = data.get("response", {}).get("players", [])
            return players[0] if players else None
        except Exception as e:
            logger.error(f"获取 {steam_id} 的玩家信息失败: {e}")
            return None

    async def _get_sgdb_cover(self, game_name: str, appid: int) -> Optional[str]:
        """从SteamGridDB获取游戏封面图URL。"""
        if not self.sgdb_api_key:
            return None

        client = await self._get_client()
        headers = {"Authorization": f"Bearer {self.sgdb_api_key}"}

        try:
            # 搜索游戏
            search_url = f"https://www.steamgriddb.com/api/v2/search/autocomplete/{game_name}"
            resp = await client.get(search_url, headers=headers)
            data = resp.json()

            if not data.get("success") or not data.get("data"):
                # 尝试通过appid查询
                game_url = f"https://www.steamgriddb.com/api/v2/games/steam/{appid}"
                resp_game = await client.get(game_url, headers=headers)
                data_game = resp_game.json()
                if data_game.get("success") and data_game.get("data"):
                    sgdb_game_id = data_game["data"]["id"]
                    grid_url = f"https://www.steamgriddb.com/api/v2/grids/game/{sgdb_game_id}?dimensions=600x900&type=static&limit=1"
                    resp_grid = await client.get(grid_url, headers=headers)
                    data_grid = resp_grid.json()
                    if data_grid.get("success") and data_grid.get("data"):
                        return data_grid["data"][0]["url"]
                return None

            sgdb_game_id = data["data"][0]["id"]
            grid_url = f"https://www.steamgriddb.com/api/v2/grids/game/{sgdb_game_id}?dimensions=600x900&type=static&limit=1"
            resp2 = await client.get(grid_url, headers=headers)
            data2 = resp2.json()

            if data2.get("success") and data2.get("data"):
                return data2["data"][0]["url"]
            return None

        except Exception as e:
            logger.error(f"获取SGDB封面失败: {e}")
            return None

    async def _search_game_on_itad(self, game_name: str) -> Optional[str]:
        """在ITAD上搜索游戏，返回游戏ID (gid)。"""
        if not self.itad_api_key:
            return None

        client = await self._get_client()
        try:
            resp = await client.get(
                f"{self.itad_api_base}/games/search/v1",
                params={"key": self.itad_api_key, "title": game_name, "limit": 5},
                timeout=10
            )
            if resp.status_code != 200:
                return None

            data = resp.json()
            if not data:
                return None

            # 返回第一个匹配结果的ID
            return data[0].get("id")
        except Exception as e:
            logger.error(f"ITAD搜索游戏失败: {e}")
            return None

    async def _get_game_price_from_itad(self, gid: str) -> Optional[dict]:
        """从ITAD获取游戏价格和史低信息。"""
        if not self.itad_api_key or not gid:
            return None

        client = await self._get_client()
        try:
            # 使用POST请求获取价格信息
            resp = await client.post(
                f"{self.itad_api_base}/games/prices/v3",
                params={"key": self.itad_api_key, "country": "CN", "shops": 61},
                json=[gid],
                timeout=10
            )
            if resp.status_code != 200:
                logger.warning(f"ITAD价格API返回状态码: {resp.status_code}")
                return None

            data = resp.json()
            if not data or not isinstance(data, list) or len(data) == 0:
                return None

            game_data = data[0]
            deals = game_data.get("deals", [])
            history_low = game_data.get("historyLow", {})

            # 获取当前价格（从deals中找steam商店的）
            current_price = None
            original_price = None
            discount = 0

            for deal in deals:
                if deal.get("shop", {}).get("name", "").lower() == "steam":
                    price_info = deal.get("price", {})
                    regular_info = deal.get("regular", {})
                    current_price = price_info.get("amount")
                    original_price = regular_info.get("amount")
                    discount = deal.get("cut", 0)
                    break

            # 获取史低价格（优先级：all > y1 > m3）
            lowest_price = None
            for period in ["all", "y1", "m3"]:
                low_info = history_low.get(period)
                if low_info and low_info.get("amount") is not None:
                    lowest_price = low_info.get("amount")
                    break

            return {
                "current": current_price,
                "original": original_price,
                "discount": discount,
                "lowest": lowest_price,
                "currency": "CNY"
            }
        except Exception as e:
            logger.error(f"ITAD获取价格失败: {e}")
            return None

    async def _get_game_price_from_steam(self, appid: int) -> Optional[dict]:
        """从Steam官方API获取游戏价格（备用方案）。"""
        client = await self._get_client()

        try:
            url = f"https://store.steampowered.com/api/appdetails?appids={appid}&cc=cn"
            resp = await client.get(url, timeout=10)

            if resp.status_code != 200:
                return None

            data = resp.json()
            if not isinstance(data, dict):
                return None

            app_data = data.get(str(appid))
            if not app_data or not isinstance(app_data, dict):
                return None

            if not app_data.get("success"):
                return None

            game_data = app_data.get("data", {})
            if not isinstance(game_data, dict):
                return None

            # 检查是否免费
            if game_data.get("is_free"):
                return {"current": 0, "original": 0, "discount": 0, "lowest": 0, "currency": "CNY"}

            price_data = game_data.get("price_overview")
            if not price_data or not isinstance(price_data, dict):
                return None

            # 价格是以分为单位
            current_price = price_data.get("final", 0) / 100
            original_price = price_data.get("initial", 0) / 100
            discount = price_data.get("discount_percent", 0)

            return {
                "current": current_price,
                "original": original_price,
                "discount": discount,
                "lowest": None,  # Steam API不提供史低
                "currency": price_data.get("currency", "CNY")
            }
        except Exception as e:
            logger.error(f"Steam价格API失败: {e}")
            return None

    async def _get_game_price(self, appid: int, game_name: str = "") -> Optional[dict]:
        """获取游戏价格信息（优先使用ITAD，备用Steam）。"""
        store_url = f"https://store.steampowered.com/app/{appid}"

        # 优先使用ITAD获取价格和史低
        if self.itad_api_key and game_name:
            gid = await self._search_game_on_itad(game_name)
            if gid:
                itad_price = await self._get_game_price_from_itad(gid)
                if itad_price and itad_price.get("current") is not None:
                    itad_price["store_url"] = store_url
                    # 格式化价格显示
                    current = itad_price.get("current", 0)
                    original = itad_price.get("original", 0)
                    lowest = itad_price.get("lowest")

                    itad_price["current_formatted"] = f"¥{current:.2f}" if current else "未知"
                    itad_price["original_formatted"] = f"¥{original:.2f}" if original and original != current else ""
                    itad_price["lowest_formatted"] = f"¥{lowest:.2f}" if lowest else "未知"
                    return itad_price

        # 备用：使用Steam API
        steam_price = await self._get_game_price_from_steam(appid)
        if steam_price:
            steam_price["store_url"] = store_url
            current = steam_price.get("current", 0)
            original = steam_price.get("original", 0)
            steam_price["current_formatted"] = f"¥{current:.2f}" if current else "免费"
            steam_price["original_formatted"] = f"¥{original:.2f}" if original and original != current else ""
            steam_price["lowest_formatted"] = "未知（需配置ITAD API Key）"
            return steam_price

        # 免费游戏
        return {
            "current": 0,
            "original": 0,
            "discount": 0,
            "lowest": 0,
            "current_formatted": "免费",
            "original_formatted": "",
            "lowest_formatted": "免费",
            "store_url": store_url,
            "currency": "CNY"
        }

    async def _download_image(self, url: str, save_path: Path) -> bool:
        """下载图片并保存。"""
        try:
            client = await self._get_client()
            resp = await client.get(url, timeout=15)
            if resp.status_code == 200:
                with open(save_path, "wb") as f:
                    f.write(resp.content)
                return True
        except Exception as e:
            logger.error(f"下载图片失败: {e}")
        return False

    async def _get_avatar(self, steam_id: str, avatar_url: str) -> Optional[Path]:
        """获取玩家头像。"""
        avatar_dir = self.data_dir / "avatars"
        avatar_dir.mkdir(exist_ok=True)
        avatar_path = avatar_dir / f"{steam_id}.jpg"

        if avatar_path.exists():
            return avatar_path

        if await self._download_image(avatar_url, avatar_path):
            return avatar_path
        return None

    async def _get_game_cover(self, appid: int, game_name: str) -> Optional[Path]:
        """获取游戏封面。"""
        cover_dir = self.data_dir / "covers"
        cover_dir.mkdir(exist_ok=True)
        cover_path = cover_dir / f"{appid}.jpg"

        if cover_path.exists():
            return cover_path

        cover_url = await self._get_sgdb_cover(game_name, appid)
        if cover_url and await self._download_image(cover_url, cover_path):
            return cover_path
        return None

    def _render_gradient_bg(self, img_w: int, img_h: int) -> PILImage.Image:
        """生成渐变背景。"""
        base = PILImage.new("RGB", (img_w, img_h), BG_COLOR_TOP)
        top_r, top_g, top_b = BG_COLOR_TOP
        bot_r, bot_g, bot_b = BG_COLOR_BOTTOM
        for y in range(img_h):
            ratio = y / (img_h - 1)
            r = int(top_r * (1 - ratio) + bot_r * ratio)
            g = int(top_g * (1 - ratio) + bot_g * ratio)
            b = int(top_b * (1 - ratio) + bot_b * ratio)
            for x in range(img_w):
                base.putpixel((x, y), (r, g, b))
        return base

    def _get_font(self, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
        """获取字体。"""
        font_name = "NotoSansHans-Medium.otf" if bold else "NotoSansHans-Regular.otf"
        font_path = self.fonts_dir / font_name

        if font_path.exists():
            return ImageFont.truetype(str(font_path), size)

        # 尝试系统字体
        try:
            return ImageFont.truetype("msyh.ttc", size)  # 微软雅黑
        except:
            return ImageFont.load_default()

    async def _render_new_game_image(
        self,
        player_name: str,
        avatar_path: Optional[Path],
        game_name: str,
        cover_path: Optional[Path],
    ) -> bytes:
        """渲染新游戏通知图片（只包含封面、头像、玩家名、游戏名）。"""
        img = self._render_gradient_bg(IMG_W, IMG_H).convert("RGBA")
        draw = ImageDraw.Draw(img)

        # 获取字体
        font_bold = self._get_font(28, bold=True)
        font = self._get_font(22)

        # 1. 渲染封面图（左侧）
        cover_right = 0
        if cover_path and cover_path.exists():
            try:
                cover_src = PILImage.open(cover_path).convert("RGBA")
                scale = IMG_H / cover_src.height
                new_w = int(cover_src.width * scale)
                cover_resized = cover_src.resize((new_w, IMG_H), PILImage.LANCZOS)
                img.paste(cover_resized, (0, 0), cover_resized)
                cover_right = new_w
            except Exception as e:
                logger.error(f"封面渲染失败: {e}")

        # 2. 渲染头像（封面右侧）
        avatar_margin = 24
        avatar_x = cover_right + avatar_margin
        avatar_y = 30

        if avatar_path and avatar_path.exists():
            try:
                avatar = PILImage.open(avatar_path).convert("RGBA").resize((AVATAR_SIZE, AVATAR_SIZE))
                # 圆角遮罩
                mask = PILImage.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
                draw_mask = ImageDraw.Draw(mask)
                draw_mask.rounded_rectangle((0, 0, AVATAR_SIZE, AVATAR_SIZE), radius=AVATAR_SIZE // 5, fill=255)
                avatar_rgba = avatar.copy()
                avatar_rgba.putalpha(mask)
                img.alpha_composite(avatar_rgba, (avatar_x, avatar_y))
            except Exception as e:
                logger.error(f"头像渲染失败: {e}")

        # 3. 渲染文字（头像右侧）
        text_x = avatar_x + AVATAR_SIZE + avatar_margin
        text_y = avatar_y + 10

        # 玩家名
        draw.text((text_x, text_y), player_name, font=font_bold, fill=(255, 255, 255, 255))

        # "购买了新游戏"
        draw.text((text_x, text_y + 36), "购买了新游戏", font=font, fill=(200, 255, 200, 255))

        # 游戏名（可能需要换行）
        text_area_w = IMG_W - text_x - 20
        game_name_lines = self._text_wrap(game_name, font, text_area_w)
        for idx, line in enumerate(game_name_lines):
            draw.text((text_x, text_y + 72 + idx * 30), line, font=font, fill=(129, 173, 81, 255))

        # 转换为RGB并返回字节
        rgb_img = img.convert("RGB")
        buf = io.BytesIO()
        rgb_img.save(buf, format="PNG")
        buf.seek(0)
        return buf.getvalue()

    def _text_wrap(self, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
        """自动换行。"""
        lines = []
        line = ""
        dummy_img = PILImage.new("RGB", (10, 10))
        draw = ImageDraw.Draw(dummy_img)

        for char in text:
            bbox = draw.textbbox((0, 0), line + char, font=font)
            width = bbox[2] - bbox[0]
            if width <= max_width:
                line += char
            else:
                lines.append(line)
                line = char
        if line:
            lines.append(line)
        return lines if lines else [""]

    async def _check_friend_games(self, steam_id: str, nickname: str) -> list[dict]:
        """检查单个好友的游戏库变化。"""
        tag = f"[{nickname}({steam_id})]"

        current_games = await self._get_owned_games(steam_id)
        if current_games is None:
            logger.warning(f"[Steam游戏库监控] {tag} API返回None（私密资料/API错误/限流），跳过本次检查")
            return []

        current_game_ids = {game["appid"] for game in current_games}
        cached_game_ids = set(self.games_cache.get(steam_id, []))

        # 首次检查：只记录缓存，不通知
        if steam_id not in self.games_cache:
            self.games_cache[steam_id] = list(current_game_ids)
            self._save_json(self.games_cache_file, self.games_cache)
            logger.info(f"[Steam游戏库监控] {tag} 📝 首次记录游戏库，共 {len(current_game_ids)} 个游戏（不推送）")
            return []

        # 对比差异
        new_game_ids = current_game_ids - cached_game_ids
        removed_game_ids = cached_game_ids - current_game_ids
        new_games = [g for g in current_games if g["appid"] in new_game_ids]

        # 逐用户详细日志
        logger.info(
            f"[Steam游戏库监控] {tag} "
            f"📊 当前: {len(current_game_ids)} 个 | "
            f"缓存: {len(cached_game_ids)} 个 | "
            f"新增: {len(new_games)} | "
            f"移除: {len(removed_game_ids)}"
        )

        if new_games:
            for g in new_games:
                logger.info(f"[Steam游戏库监控] {tag} 🆕 新增游戏: {g.get('name', '?')} (appid={g.get('appid', '?')})")
            # 有新游戏 → 更新缓存
            self.games_cache[steam_id] = list(current_game_ids)
            self._save_json(self.games_cache_file, self.games_cache)
            logger.info(f"[Steam游戏库监控] {tag} ✅ 缓存已更新为 {len(current_game_ids)} 个游戏")
        else:
            logger.info(f"[Steam游戏库监控] {tag} ✅ 无新增游戏，跳过推送")

        return new_games

    async def _check_all_friends(self):
        """检查所有好友的游戏库变化。"""
        if not self.steam_ids_config:
            logger.info("[Steam游戏库监控] 无监控用户，跳过检查")
            return

        # 检查平台适配器是否就绪
        adapter_id = await self._get_adapter_id()
        if not adapter_id and self.notify_groups:
            logger.warning("[Steam游戏库监控] ⚠️ 未找到可用平台适配器，无法推送通知！")
            logger.warning("[Steam游戏库监控] 请确保已配置并启用至少一个消息平台（如 aiocqhttp）")

        logger.info(f"[Steam游戏库监控] ========== 开始检查 {len(self.steam_ids_config)} 个用户 ==========")

        total_new = 0
        total_notified = 0
        check_results = []

        for friend_config in self.steam_ids_config:
            steam_id = friend_config.get("steam_id", "")
            nickname = friend_config.get("nickname", "")

            if not steam_id:
                logger.warning("[Steam游戏库监控] 跳过空Steam ID配置")
                continue

            # 如果没有昵称，从Steam获取
            if not nickname:
                player_info = await self._get_player_summary(steam_id)
                nickname = player_info.get("personaname", steam_id) if player_info else steam_id

            new_games = await self._check_friend_games(steam_id, nickname)

            if new_games and self.enable_notification:
                total_new += len(new_games)
                if adapter_id:
                    await self._notify_new_games(steam_id, nickname, new_games, adapter_id)
                    total_notified += len(new_games)
                    check_results.append(f"  {nickname}: 🆕 {len(new_games)} 个新游戏 → ✅ 已推送")
                else:
                    game_names = [g.get("name", "?") for g in new_games]
                    logger.warning(f"[Steam游戏库监控] ⚠️ 检测到 {nickname} 的新游戏 ({', '.join(game_names)})，但无可用平台适配器，跳过推送")
                    check_results.append(f"  {nickname}: 🆕 {len(new_games)} 个新游戏 → ❌ 无适配器")
            elif new_games and not self.enable_notification:
                total_new += len(new_games)
                check_results.append(f"  {nickname}: 🆕 {len(new_games)} 个新游戏 → ⏸️ 通知已禁用")
            else:
                check_results.append(f"  {nickname}: ✅ 无变化")

        logger.info(f"[Steam游戏库监控] ========== 检查完毕 ==========")
        if check_results:
            for line in check_results:
                logger.info(f"[Steam游戏库监控] {line}")
        logger.info(
            f"[Steam游戏库监控] 📊 汇总: 检查 {len(self.steam_ids_config)} 人 | "
            f"新增 {total_new} 个游戏 | 推送 {total_notified} 条通知"
        )

    async def _notify_new_games(self, steam_id: str, nickname: str, new_games: list[dict], adapter_id: str = ""):
        """发送新游戏购买通知。"""
        if not new_games:
            return

        logger.info(f"[Steam游戏库监控] 📤 开始推送 {nickname} 的 {len(new_games)} 个新游戏通知...")

        # 获取推送群号列表
        group_ids = self.notify_groups

        if not group_ids:
            logger.warning(f"[Steam游戏库监控] ⚠️ {nickname} 购买了新游戏，但未配置推送群号，无法推送")
            return

        for game in new_games:
            game_name = game.get("name", "未知游戏")
            appid = game.get("appid", 0)

            # 构建消息文本
            message_text = self.message_template.format(username=nickname, gamename=game_name)

            # 获取游戏价格信息
            game_info_lines = []
            store_url = f"https://store.steampowered.com/app/{appid}"

            if self.show_game_info:
                price_info = await self._get_game_price(appid, game_name)
                if price_info:
                    current_formatted = price_info.get("current_formatted", "未知")
                    original_formatted = price_info.get("original_formatted", "")
                    lowest_formatted = price_info.get("lowest_formatted", "未知")
                    discount = price_info.get("discount", 0)
                    current_price = price_info.get("current", 0)
                    lowest_price = price_info.get("lowest")
                    store_url = price_info.get("store_url", store_url)

                    game_info_lines.append(f"💰 当前国区售价: {current_formatted}")

                    if discount > 0 and original_formatted:
                        game_info_lines.append(f"📉 原价: {original_formatted} (-{discount}%)")

                    game_info_lines.append(f"🏷️ 史低售价: {lowest_formatted}")

                    # 购买评价（当前价格 vs 史低）
                    if lowest_price is not None and current_price is not None:
                        if current_price <= lowest_price:
                            comment = self.comment_at_lowest
                        else:
                            comment = self.comment_above_lowest
                    else:
                        comment = "无法判断（缺少价格数据）"
                    game_info_lines.append(f"📝 购买评价: {comment}")

                    game_info_lines.append(f"🔗 Steam商店: {store_url}")

            # 完整消息文本
            info_text = "\n".join(game_info_lines)
            full_message = f"🎮 {message_text}\n{info_text}" if game_info_lines else f"🎮 {message_text}"

            if self.render_image:
                try:
                    # 获取玩家信息
                    player_info = await self._get_player_summary(steam_id)
                    avatar_url = player_info.get("avatarfull", "") if player_info else ""
                    avatar_path = await self._get_avatar(steam_id, avatar_url) if avatar_url else None

                    # 获取游戏封面
                    cover_path = await self._get_game_cover(appid, game_name)

                    # 渲染图片
                    image_data = await self._render_new_game_image(
                        nickname, avatar_path, game_name, cover_path
                    )

                    # 保存临时图片
                    temp_path = self.data_dir / "temp_notify.png"
                    with open(temp_path, "wb") as f:
                        f.write(image_data)

                    # 使用 MessageChain 发送图文混合消息
                    msg_chain = [
                        Plain(full_message),
                        Image.fromFileSystem(str(temp_path))
                    ]

                    for group_id in group_ids:
                        # 如果没有外部传入 adapter_id，动态获取
                        aid = adapter_id or await self._get_adapter_id()
                        if not aid:
                            logger.error(f"[Steam游戏库监控] ❌ 无法向群 {group_id} 推送：无可用平台适配器")
                            continue

                        umo = self._build_group_umo(aid, group_id)
                        try:
                            await self.context.send_message(umo, MessageChain(msg_chain))
                            logger.info(f"[Steam游戏库监控] ✅ 已发送 {nickname} 的新游戏通知到群 {group_id}")
                        except Exception as e:
                            logger.error(f"[Steam游戏库监控] ❌ 发送通知到群 {group_id} 失败: {e}")

                except Exception as e:
                    logger.error(f"[Steam游戏库监控] ❌ 渲染图片失败: {e}")
                    # 降级为纯文本通知
                    await self._send_text_notify(full_message, group_ids, adapter_id)
            else:
                # 纯文本通知
                await self._send_text_notify(full_message, group_ids, adapter_id)

    async def _send_text_notify(self, message: str, group_ids: list[str], adapter_id: str = ""):
        """发送文本通知。"""
        for group_id in group_ids:
            aid = adapter_id or await self._get_adapter_id()
            if not aid:
                logger.error(f"[Steam游戏库监控] ❌ 无法向群 {group_id} 推送纯文本：无可用平台适配器")
                continue

            umo = self._build_group_umo(aid, group_id)
            try:
                await self.context.send_message(umo, message)
                logger.info(f"[Steam游戏库监控] ✅ 已发送文本通知到群 {group_id}")
            except Exception as e:
                logger.error(f"[Steam游戏库监控] ❌ 发送文本通知到群 {group_id} 失败: {e}")

    @filter.command_group("steamlib")
    def steamlib(self):
        """Steam游戏库监控命令组。"""
        pass

    @steamlib.command("test", alias={"sl测试", "sltest"})
    async def test_notify(self, event: AstrMessageEvent):
        """测试推送效果。"""
        if not self.steam_api_key:
            yield event.plain_result("❌ 请先在插件配置中设置 Steam Web API Key")
            return

        if not self.steam_ids_config:
            yield event.plain_result("📋 未配置监控的Steam ID")
            return

        yield event.plain_result("🎮 正在准备测试推送...")

        # 随机选择一个用户
        friend_config = random.choice(self.steam_ids_config)
        steam_id = friend_config.get("steam_id", "")
        nickname = friend_config.get("nickname", "")

        if not nickname:
            player_info = await self._get_player_summary(steam_id)
            nickname = player_info.get("personaname", steam_id) if player_info else steam_id

        # 获取用户的最后购买游戏（从缓存中随机选一个）
        cached_games = self.games_cache.get(steam_id, [])
        if not cached_games:
            yield event.plain_result(f"❌ {nickname} 的游戏库缓存为空，请先运行 /steamlib check 初始化")
            return

        # 获取游戏信息
        games = await self._get_owned_games(steam_id)
        if not games:
            yield event.plain_result(f"❌ 无法获取 {nickname} 的游戏列表")
            return

        # 随机选择一个游戏
        game = random.choice(games)
        game_name = game.get("name", "未知游戏")
        appid = game.get("appid", 0)

        if self.render_image:
            try:
                # 获取玩家头像
                player_info = await self._get_player_summary(steam_id)
                avatar_url = player_info.get("avatarfull", "") if player_info else ""
                avatar_path = await self._get_avatar(steam_id, avatar_url) if avatar_url else None

                # 获取游戏封面
                cover_path = await self._get_game_cover(appid, game_name)

                # 渲染图片（使用测试标题）
                image_data = await self._render_test_image(
                    nickname, avatar_path, game_name, cover_path
                )

                # 保存临时图片
                temp_path = self.data_dir / "temp_test.png"
                with open(temp_path, "wb") as f:
                    f.write(image_data)

                # 构建消息
                message_text = self.message_template.format(username=nickname, gamename=game_name)

                # 获取游戏价格信息
                game_info_lines = []
                store_url = f"https://store.steampowered.com/app/{appid}"

                if self.show_game_info:
                    price_info = await self._get_game_price(appid, game_name)
                    if price_info:
                        current_formatted = price_info.get("current_formatted", "未知")
                        original_formatted = price_info.get("original_formatted", "")
                        lowest_formatted = price_info.get("lowest_formatted", "未知")
                        discount = price_info.get("discount", 0)
                        current_price = price_info.get("current", 0)
                        lowest_price = price_info.get("lowest")
                        store_url = price_info.get("store_url", store_url)

                        game_info_lines.append(f"💰 当前国区售价: {current_formatted}")

                        if discount > 0 and original_formatted:
                            game_info_lines.append(f"📉 原价: {original_formatted} (-{discount}%)")

                        game_info_lines.append(f"🏷️ 史低售价: {lowest_formatted}")

                        # 购买评价（当前价格 vs 史低）
                        if lowest_price is not None and current_price is not None:
                            if current_price <= lowest_price:
                                comment = self.comment_at_lowest
                            else:
                                comment = self.comment_above_lowest
                        else:
                            comment = "无法判断（缺少价格数据）"
                        game_info_lines.append(f"📝 购买评价: {comment}")

                        game_info_lines.append(f"🔗 Steam商店: {store_url}")

                # 完整消息
                info_text = "\n".join(game_info_lines)
                full_message = f"🎮 测试推送\n🎮 {message_text}\n{info_text}" if game_info_lines else f"🎮 测试推送\n🎮 {message_text}"

                # 发送到当前会话（图文混合）
                yield event.plain_result(full_message)
                yield event.image_result(str(temp_path))

            except Exception as e:
                logger.error(f"测试推送渲染失败: {e}")
                yield event.plain_result(f"🎮 测试推送（文本模式）\n\n{self.message_template.format(username=nickname, gamename=game_name)}\n📝 测试效果")
        else:
            yield event.plain_result(f"🎮 测试推送\n\n{self.message_template.format(username=nickname, gamename=game_name)}\n📝 测试效果")

    async def _render_test_image(
        self,
        player_name: str,
        avatar_path: Optional[Path],
        game_name: str,
        cover_path: Optional[Path],
    ) -> bytes:
        """渲染测试通知图片。"""
        # 创建渐变背景
        img = self._render_gradient_bg(IMG_W, IMG_H).convert("RGBA")
        draw = ImageDraw.Draw(img)

        # 获取字体
        font_bold = self._get_font(28, bold=True)
        font = self._get_font(22)
        font_small = self._get_font(16)

        # 1. 渲染封面图（左侧）
        cover_right = 0
        if cover_path and cover_path.exists():
            try:
                cover_src = PILImage.open(cover_path).convert("RGBA")
                scale = IMG_H / cover_src.height
                new_w = int(cover_src.width * scale)
                cover_resized = cover_src.resize((new_w, IMG_H), PILImage.LANCZOS)
                img.paste(cover_resized, (0, 0), cover_resized)
                cover_right = new_w
            except Exception as e:
                logger.error(f"封面渲染失败: {e}")

        # 2. 渲染头像（封面右侧）
        avatar_margin = 24
        avatar_x = cover_right + avatar_margin
        avatar_y = 30

        if avatar_path and avatar_path.exists():
            try:
                avatar = PILImage.open(avatar_path).convert("RGBA").resize((AVATAR_SIZE, AVATAR_SIZE))
                # 圆角遮罩
                mask = PILImage.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
                draw_mask = ImageDraw.Draw(mask)
                draw_mask.rounded_rectangle((0, 0, AVATAR_SIZE, AVATAR_SIZE), radius=AVATAR_SIZE // 5, fill=255)
                avatar_rgba = avatar.copy()
                avatar_rgba.putalpha(mask)
                img.alpha_composite(avatar_rgba, (avatar_x, avatar_y))
            except Exception as e:
                logger.error(f"头像渲染失败: {e}")

        # 3. 渲染文字（头像右侧）
        text_x = avatar_x + AVATAR_SIZE + avatar_margin
        text_y = avatar_y + 10

        # 玩家名
        draw.text((text_x, text_y), player_name, font=font_bold, fill=(255, 255, 255, 255))

        # "测试效果"（替代"购买了新游戏"）
        draw.text((text_x, text_y + 36), "测试效果", font=font, fill=(255, 200, 100, 255))

        # 游戏名（可能需要换行）
        text_area_w = IMG_W - text_x - 20
        game_name_lines = self._text_wrap(game_name, font, text_area_w)
        for idx, line in enumerate(game_name_lines):
            draw.text((text_x, text_y + 72 + idx * 30), line, font=font, fill=(129, 173, 81, 255))

        # 转换为RGB并返回字节
        rgb_img = img.convert("RGB")
        buf = io.BytesIO()
        rgb_img.save(buf, format="PNG")
        buf.seek(0)
        return buf.getvalue()

    @steamlib.command("list", alias={"sl列表", "sllist"})
    async def list_friends(self, event: AstrMessageEvent):
        """查看监控的好友列表。"""
        if not self.steam_ids_config:
            yield event.plain_result("📋 未配置监控的Steam ID，请在插件配置中添加")
            return

        lines = ["📋 Steam好友监控列表:\n"]
        for friend_config in self.steam_ids_config:
            steam_id = friend_config.get("steam_id", "")
            nickname = friend_config.get("nickname", "")
            game_count = len(self.games_cache.get(steam_id, []))

            display_name = nickname if nickname else steam_id
            lines.append(f"  👤 {display_name} (ID: {steam_id})")
            lines.append(f"     📊 游戏数: {game_count}")

        yield event.plain_result("\n".join(lines))

    @steamlib.command("check", alias={"sl检查", "slcheck"})
    async def check_now(self, event: AstrMessageEvent):
        """立即检查游戏库变动。"""
        if not self.steam_api_key:
            yield event.plain_result("❌ 请先在插件配置中设置 Steam Web API Key")
            return

        if not self.steam_ids_config:
            yield event.plain_result("📋 未配置监控的Steam ID")
            return

        yield event.plain_result("🔄 正在检查游戏库变动...")

        total_new = 0
        results = []

        for friend_config in self.steam_ids_config:
            steam_id = friend_config.get("steam_id", "")
            nickname = friend_config.get("nickname", "")

            if not steam_id:
                continue

            if not nickname:
                player_info = await self._get_player_summary(steam_id)
                nickname = player_info.get("personaname", steam_id) if player_info else steam_id

            new_games = await self._check_friend_games(steam_id, nickname)
            if new_games:
                total_new += len(new_games)
                game_names = [g.get("name", "未知游戏") for g in new_games]
                results.append(f"👤 {nickname}:\n" + "\n".join([f"  - {n}" for n in game_names]))
                # 触发推送通知
                if self.enable_notification and self.notify_groups:
                    logger.info(f"[Steam游戏库监控] check命令触发推送: {nickname} 新增 {len(new_games)} 个游戏")
                    await self._notify_new_games(steam_id, nickname, new_games)

        if results:
            yield event.plain_result(f"🆕 检测到 {total_new} 个新游戏:\n\n" + "\n\n".join(results) + "\n\n✅ 已触发推送通知")
        else:
            yield event.plain_result("✅ 所有好友暂无新增游戏")

    @steamlib.command("info", alias={"sl信息", "slinfo"})
    async def friend_info(self, event: AstrMessageEvent, steam_id: str):
        """查看好友详细信息。"""
        # 查找好友配置
        friend_config = None
        for config in self.steam_ids_config:
            if config.get("steam_id") == steam_id:
                friend_config = config
                break

        if not friend_config:
            yield event.plain_result(f"❌ 未找到Steam ID: {steam_id}")
            return

        nickname = friend_config.get("nickname", "")
        game_count = len(self.games_cache.get(steam_id, []))

        # 获取玩家在线状态
        player_info = await self._get_player_summary(steam_id)
        status = "未知"
        if player_info:
            if not nickname:
                nickname = player_info.get("personaname", steam_id)
            state = player_info.get("personastate", 0)
            status_map = {0: "离线", 1: "在线", 2: "忙碌", 3: "离开", 4: "打盹", 5: "想交易", 6: "想玩"}
            status = status_map.get(state, "未知")

        lines = [
            f"👤 好友信息: {nickname}",
            f"🆔 Steam ID: {steam_id}",
            f"🟢 状态: {status}",
            f"📊 游戏数: {game_count}",
        ]

        yield event.plain_result("\n".join(lines))

    @steamlib.command("help", alias={"sl帮助", "slhelp"})
    async def help(self, event: AstrMessageEvent):
        """显示帮助信息。"""
        # 尝试初始化适配器 ID（用于显示状态）
        adapter_id = await self._get_adapter_id()
        adapter_status = f"✅ {adapter_id}" if adapter_id else "⚠️ 未检测到（请确认已启用消息平台）"

        help_text = f"""🎮 Steam游戏库监控插件

📌 命令列表:
  /steamlib test - 测试推送效果（随机用户+游戏）
  /steamlib list - 查看监控列表
  /steamlib check - 立即检查游戏库变动
  /steamlib info <steam_id> - 查看好友详细信息
  /steamlib help - 显示此帮助

🔧 推送状态:
  平台适配器: {adapter_status}

⚙️ 配置说明:
  在 AstrBot WebUI 插件配置中设置：
  - Steam Web API Key（必填）
  - ITAD API Key（可选，用于获取史低价格）
  - SteamGridDB API Key（可选，用于获取游戏封面）
  - 要监控的Steam ID列表
  - 推送通知的群号列表（直接填群号即可，插件会自动获取平台信息）
  - 消息模板（支持变量：{{username}} {{gamename}}）
  - 是否显示游戏资讯（价格等）

🔗 获取Steam Web API Key:
https://steamcommunity.com/dev/apikey

🔗 获取SteamGridDB API Key:
https://www.steamgriddb.com/profile/preferences/api

🔗 获取ITAD API Key:
https://isthereanydeal.com/apps/"""
        yield event.plain_result(help_text)

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        """AstrBot加载完成后的回调。"""
        logger.info("Steam游戏库监控插件已加载")

    async def terminate(self):
        """插件卸载时的清理工作。"""
        # 取消轮询任务
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        # 关闭HTTP客户端
        if self._client and not self._client.is_closed:
            await self._client.aclose()

        logger.info("Steam游戏库监控插件已卸载")
