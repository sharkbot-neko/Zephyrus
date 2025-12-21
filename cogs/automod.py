import discord
from discord import app_commands
from discord.ext import commands
from motor.motor_asyncio import AsyncIOMotorClient
import re
import aiohttp
from datetime import timedelta
import json
import asyncio
import os
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()

mongo_client = AsyncIOMotorClient(os.getenv("MONGO_URI"))
db = mongo_client["automod"]
config_collection = db["configs"]
ngwords_collection = db["ngwords"]
db1 = mongo_client["serverlog"]
serverlog_collection = db1["channel.serverlog.channel"]

SAFE_BROWSING_API_KEY = "AIzaSyBFYKfUJXoQjwfDjo76wKIS6368bJ3m-Jw"
SAFESEARCH_ENDPOINT = "https://www.googleapis.com/customsearch/v1"
SEARCH_ENGINE_ID = "b25abc8abcde349e6"

def default_config():
    return {
        "invites": {"enabled": False, "timeout": False},
        "malicious": {"enabled": False, "timeout": False},
        "nsfw": {"enabled": False, "timeout": False},
        "ngwords": {"enabled": False, "timeout": False}
    }

async def get_config(guild_id: int):
    cfg = await config_collection.find_one({"_id": guild_id})
    defaults = default_config()

    if not cfg:
        cfg = defaults
        cfg["_id"] = guild_id
        await config_collection.insert_one(cfg)
    else:
        for key, value in defaults.items():
            if key not in cfg:
                cfg[key] = value
        await config_collection.update_one({"_id": guild_id}, {"$set": cfg}, upsert=True)

    return cfg

async def is_exempted(guild_id: int, channel_id: int, user_id: int, check_type: str):
    """指定されたユーザーまたはチャンネルが例外設定に該当するか判定"""
    exception_db = db["exceptions"]

    # チャンネル側設定
    ch_data = await exception_db.find_one({"_id": f"{guild_id}-channel-{channel_id}"})
    if ch_data and ch_data.get(check_type):
        return True

    # ユーザー側設定
    user_data = await exception_db.find_one({"_id": f"{guild_id}-user-{user_id}"})
    if user_data and user_data.get(check_type):
        return True

    return False

class AddWordModal(discord.ui.Modal, title="NGワードの追加"):
    def __init__(self, guild_id: int, user_id: int, parent_view):
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id
        self.parent_view = parent_view

        self.word = discord.ui.TextInput(
            label="NGワードを入力",
            placeholder="例: 雑魚",
            required=True,
            max_length=100
        )
        self.add_item(self.word)

    async def on_submit(self, interaction: discord.Interaction):
        content = self.word.value.strip()

        cfg = await ngwords_collection.find_one({"_id": self.guild_id}) or {}
        words = cfg.get("words", [])

        if content in words:
            await interaction.response.send_message(
                "<:warn:1394241229176311888>そのワードはすでに登録されています。",
                ephemeral=True
            )
            return

        words.append(content)
        await ngwords_collection.update_one(
            {"_id": self.guild_id}, {"$set": {"words": words}}, upsert=True
        )

        # パネル更新
        await self.parent_view.update_panel()

        # 追加通知
        await interaction.response.send_message(
            f"<:check:1394240622310850580>NGワード `{content}` を追加しました。",
            ephemeral=True
        )


class NGWordConfigView(discord.ui.View):
    def __init__(self, guild_id: int, user_id: int, message: discord.Message = None):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.user_id = user_id
        self.message = message

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="NGワード追加", style=discord.ButtonStyle.green, emoji="<:plus:1394231723390275645>")
    async def add_word(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = AddWordModal(self.guild_id, self.user_id, self)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="NGワード削除", style=discord.ButtonStyle.red, emoji="<:minus:1394231720995197158>")
    async def remove_word(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = await ngwords_collection.find_one({"_id": self.guild_id}) or {}
        words = cfg.get("words", [])
        if not words:
            await interaction.response.send_message(
                "<:warn:1394241229176311888>登録されているワードがありません。",
                ephemeral=True
            )
            return

        options = [discord.SelectOption(label=w, value=w) for w in words]
        select = discord.ui.Select(placeholder="削除するワードを選択", options=options)

        async def select_callback(select_interaction: discord.Interaction):
            if select_interaction.user.id != self.user_id:
                return
            word = select.values[0]
            words.remove(word)
            await ngwords_collection.update_one(
                {"_id": self.guild_id}, {"$set": {"words": words}}, upsert=True
            )

            await select_interaction.response.edit_message(content=f"<:check:1394240622310850580>NGワード `{word}` を削除しました。", view=None)

            # 元のパネルも更新
            await self.update_panel()

        select.callback = select_callback
        view = discord.ui.View()
        view.add_item(select)

        await interaction.response.send_message(
            "削除するワードを選んでください。",
            view=view,
            ephemeral=True
        )


    async def update_panel(self):
        if not self.message:
            return

        cfg = await ngwords_collection.find_one({"_id": self.guild_id}) or {}
        words = cfg.get("words", [])
        word_list = "\n".join([f"• {w}" for w in words]) if words else "なし"

        embed = discord.Embed(
            title="<:spanner:1399035839324880958>NGワード設定パネル",
            description=f"**登録ワード一覧**\n{word_list}",
            color=discord.Color.blurple()
        )

        try:
            await self.message.edit(embed=embed, view=self)
        except Exception as e:
            print(f"[update_panel] メッセージ編集失敗: {e}")

async def check_malicious_url(content: str) -> bool:
    url_pattern = r"(https?://[^\s]+)"
    urls = re.findall(url_pattern, content)
    if not urls:
        return False

    api_url = f"https://safebrowsing.googleapis.com/v4/threatMatches:find?key={SAFE_BROWSING_API_KEY}"

    payload = {
        "client": {
            "clientId": "your-discord-bot",
            "clientVersion": "1.0"
        },
        "threatInfo": {
            "threatTypes": [
                "MALWARE",
                "SOCIAL_ENGINEERING",
                "UNWANTED_SOFTWARE",
                "POTENTIALLY_HARMFUL_APPLICATION"
            ],
            "platformTypes": ["ANY_PLATFORM"],
            "threatEntryTypes": ["URL"],
            "threatEntries": [{"url": u} for u in urls]
        }
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(api_url, json=payload) as resp:
            if resp.status != 200:
                print(f"Safe Browsing API error: {resp.status}")
                return False
            data = await resp.json()
            return "matches" in data

genai.configure(api_key=os.getenv("GEMINI_API_KEY_FOR_AUTOMOD"))

async def check_nsfw_gemini(content: str) -> dict:
    """
    Gemini 2.0 Flash-Lite でテキストの安全性を判定。
    戻り値: {"nsfw": bool, "reason": str}
    """

    prompt = f"""
あなたはコンテンツモデレーターです。
次の文章が性的・暴力的・ヘイトスピーチ・アダルト・グロテスク・過激・不快な内容を含む場合は nsfw: true に、
それ以外は nsfw: false に設定してください。

出力は次のJSON形式 **のみ** で出力してください。説明や文章は絶対に書かないでください。

例:
{{"nsfw": false, "reason": "特に問題なし"}}

判定対象の文章:
"{content}"
"""

    try:
        # Gemini呼び出しを非同期でラップ
        loop = asyncio.get_event_loop()
        model = genai.GenerativeModel("gemini-2.0-flash-lite")

        response = await loop.run_in_executor(None, lambda: model.generate_content(prompt))
        text = response.text.strip()

        # コードブロックに包まれていた場合の除去
        if text.startswith("```"):
            text = text.strip("` \n")
            text = text.replace("json", "")

        # JSON解析
        result = json.loads(text)
        print(f"[Gemini判定結果] {result}")

        # フォーマット保証
        return {
            "nsfw": bool(result.get("nsfw", False)),
            "reason": result.get("reason", "判定情報なし")
        }

    except Exception as e:
        print(f"[NSFW判定エラー]: {e}")
        return {"nsfw": False, "reason": str(e)}


class AutoMod(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # === automod コマンド ===
    @commands.hybrid_group(name="automod", description="自動モデレーション設定")
    @commands.has_permissions(administrator=True)
    async def automod(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send("利用可能なサブコマンド: `invites`, `malicious`, `nsfw`, `ngwords`, `configwords`")

    @automod.command(name="invites", description="招待リンク対策の設定をします")
    async def invites(self, ctx, 有効: bool, タイムアウト: bool = False):
        cfg = await get_config(ctx.guild.id)
        cfg["invites"].update({"enabled": 有効, "timeout": タイムアウト})
        await config_collection.update_one(
            {"_id": ctx.guild.id}, {"$set": {"invites": cfg["invites"]}}, upsert=True
        )
        await ctx.send(
            f"<:check:1394240622310850580>招待リンク対策設定を{'有効' if 有効 else '無効'}にしました。\n"
            f"タイムアウト:{'有効' if タイムアウト else '無効'}"
        )

    @automod.command(name="malicious", description="フィッシング・詐欺サイト対策の設定をします")
    async def malicious(self, ctx, 有効: bool, タイムアウト: bool = False):
        cfg = await get_config(ctx.guild.id)
        cfg["malicious"].update({"enabled": 有効, "timeout": タイムアウト})
        await config_collection.update_one(
            {"_id": ctx.guild.id}, {"$set": {"malicious": cfg["malicious"]}}, upsert=True
        )
        await ctx.send(
            f"<:check:1394240622310850580>フィッシング・詐欺サイト対策設定を{'有効' if 有効 else '無効'}にしました。\n"
            f"タイムアウト:{'有効' if タイムアウト else '無効'}"
        )

    @automod.command(name="nsfw", description="NSFWサイト誘導対策の設定をします")
    async def nsfw(self, ctx, 有効: bool, タイムアウト: bool = False):
        cfg = await get_config(ctx.guild.id)
        cfg["nsfw"].update({"enabled": 有効, "timeout": タイムアウト})
        await config_collection.update_one(
            {"_id": ctx.guild.id}, {"$set": {"nsfw": cfg["nsfw"]}}, upsert=True
        )
        await ctx.send(
            f"<:check:1394240622310850580>NSFWサイト誘導対策設定を{'有効' if 有効 else '無効'}にしました。\n"
            f"タイムアウト:{'有効' if タイムアウト else '無効'}"
        )

    @automod.command(name="ngwords", description="NGワード対策の設定をします。")
    async def ngwords(self, ctx, 有効: bool, タイムアウト: bool = False):
        await ngwords_collection.update_one(
            {"_id": ctx.guild.id},
            {"$set": {"enabled": 有効, "timeout": タイムアウト}},
            upsert=True
        )
        await ctx.send(
            f"<:check:1394240622310850580>NGワードブロック設定を{'有効' if 有効 else '無効'}にしました。\n"
            f"タイムアウト: {'有効' if タイムアウト else '無効'}"
        )


    @automod.command(name="configwords", description="NGワードリストを編集します。")
    async def configwords(self, ctx: commands.Context):
        cfg = await ngwords_collection.find_one({"_id": ctx.guild.id}) or {}
        words = cfg.get("words", [])
        word_list = "\n".join([f"• {w}" for w in words]) if words else "なし"

        embed = discord.Embed(
            title="⚙️ NGワード設定パネル",
            description=f"**登録ワード一覧**\n{word_list}",
            color=discord.Color.blurple()
        )

        view = NGWordConfigView(ctx.guild.id, ctx.author.id)
        msg = await ctx.send(embed=embed, view=view, ephemeral=False)
        view.message = msg

        
    # === メッセージ検知 ===
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild:
            return

        if isinstance(message.author, discord.Member):
            if message.author.guild_permissions.administrator:
                return

        cfg = await get_config(message.guild.id)
        exception_db = db["exceptions"]

        # 例: 招待リンク検知前
        if await is_exempted(message.guild.id, message.channel.id, message.author.id, "invites"):
            return  # このチャンネルまたはユーザーは「招待リンク送信」例外対象

        ngcfg = await ngwords_collection.find_one({"_id": message.guild.id}) or {}
        content = message.content.lower()

        content_lower = message.content.lower()  # 通常テキスト判定用
        shorteners = ["bit.ly", "goo.gl", "t.co", "tinyurl.com", "x.gd", "v.gd", "is.gd"]

        # URL抽出（元の大文字・小文字を保持）
        urls = re.findall(r"https?://[^\s]+", message.content)
        if urls:
            print(f"検出されたURL: {urls}")  # デバッグ出力

        invite_pattern = r"(?:https?://)?(discord\.gg|discord\.com/invite)/[a-zA-Z0-9]+"
    
        # 通常の招待リンク（小文字化して判定）
        if cfg["invites"]["enabled"] and not await is_exempted(message.guild.id, message.channel.id, message.author.id, "invite"):
            if re.search(invite_pattern, content_lower):
                if cfg["invites"].get("timeout"):
                    await self.punish(message, "招待リンクの送信", cfg["invites"])
                else:
                    await message.delete()
                    try:
                        await message.author.send("<:cross:1394240624202481705>このサーバーでは招待リンクを送信できません。")
                    except discord.Forbidden:
                        pass
                return

        # --- 短縮URL展開チェック ---
        if cfg["invites"]["enabled"] and not await is_exempted(message.guild.id, message.channel.id, message.author.id, "invite"):
            if re.search(invite_pattern, content_lower):
                for url in urls:
                    if any(s in url.lower() for s in shorteners):  # 短縮URLの一致は小文字で比較
                        try:
                            async with aiohttp.ClientSession() as session:
                                async with session.get(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                                    final_url = str(resp.url)
                                    print(f"{url} → {final_url}")  # 展開結果をデバッグ表示

                                    # 展開先のURL判定は小文字化して比較
                                    if "discord.gg" in final_url.lower() or "discord.com/invite" in final_url.lower():
                                        if cfg["invites"].get("timeout"):
                                            await self.punish(message, "短縮URLを使用した招待リンクの送信", cfg["invites"])
                                        else:
                                            await message.delete()
                                            try:
                                                await message.author.send(
                                                    "<:cross:1394240624202481705>このサーバーでは短縮した招待リンクを送信できません。"
                                                )
                                            except discord.Forbidden:
                                                pass
                                        return
                        except Exception as e:
                            print(f"[AutoMod] 短縮URL展開エラー: {e}")
                            continue

        # フィッシング/詐欺サイト
        if cfg["malicious"]["enabled"]:
            if await is_exempted(message.guild.id, message.channel.id, message.author.id, "malicious_url"):
                return
            if await check_malicious_url(message.content):
                if cfg["malicious"].get("timeout"):
                    await self.punish(message, "詐欺サイトの可能性のあるURLの送信", cfg["malicious"])
                else:
                    await message.delete()
                    try:
                        await message.author.send(
                            "<:cross:1394240624202481705>詐欺サイトの可能性があるURLが含まれているため、送信できません。"
                        )
                    except discord.Forbidden:
                        pass
                return

        # NSFWサイト
        if cfg["nsfw"]["enabled"]:

            # 免除なら何もせず return
            if await is_exempted(message.guild.id, message.channel.id, message.author.id, "nsfw_url"):
                return

            # URL抽出
            url_pattern = r"https?://[^\s]+"
            urls = re.findall(url_pattern, message.content)

            # URLが無い → NSFWチェックしない
            if not urls:
                return

            # ---- Geminiチェック ----
            result_raw = await check_nsfw_gemini(message.content)

            # --- レスポンスが不正な場合の fail-safe ---
            if not isinstance(result_raw, dict):
                print("[NSFW] Geminiのレスポンスが辞書形式ではありません:", result_raw)
                return  # 誤爆防止 → 何もしない

            # --- nsfw値の正規化処理 ---
            nsfw_raw = result_raw.get("nsfw", False)

            if isinstance(nsfw_raw, str):
                nsfw = nsfw_raw.strip().lower() == "true"
            elif isinstance(nsfw_raw, (int, float)):
                nsfw = bool(nsfw_raw)
            else:
                nsfw = bool(nsfw_raw)

            # デバッグログ
            print(f"[NSFWチェック] URL: {urls} 判定結果: {result_raw} → nsfw={nsfw}")

            # ---- NSFW判定 ----
            if nsfw:
                reason = "NSFWサイトへの誘導"
        
                if cfg["nsfw"].get("timeout"):
                    # タイムアウト付き punish
                    await self.punish(message, reason, cfg["nsfw"])
                else:
                    # 削除のみ
                    try:
                        await message.delete()
                    except discord.NotFound:
                        pass

                    # DM送信（拒否されてても安全に）
                    try:
                        await message.author.send("<:cross:1394240624202481705> NSFWサイトの誘導はできません。")
                    except discord.Forbidden:
                        pass

        # NGワード
        if ngcfg.get("enabled", False):
            if await is_exempted(message.guild.id, message.channel.id, message.author.id, "ng_word"):
                return
            words = ngcfg.get("words", [])
            content = message.content.lower()
            if any(w.lower() in content for w in words):
                if ngcfg.get("timeout"):
                    await self.punish(message, "NGワードが含まれたメッセージの送信", {"timeout": True})
                else:
                    try:
                        await message.delete()
                        await message.author.send(
                            f"<:cross:1394240624202481705>NGワードが含まれているため、送信できません。"
                        )
                    except discord.Forbidden:
                        pass
                return

    async def punish(self, message: discord.Message, reason: str, cfg_section: dict):
        await message.delete()
        success = False
        dmsent = "いいえ"
        dmreason = " "
        member = message.author

        # ==== 理由ごとの DM メッセージ ====
        reason_messages = {
            "招待リンクの送信": "<:cross:1394240624202481705>このサーバーでは招待リンクを送信できません。",
            "短縮URLを使用した招待リンクの送信": "<:cross:1394240624202481705>このサーバーでは短縮した招待リンクを送信できません。",
            "詐欺サイトの可能性のあるURLの送信": "<:cross:1394240624202481705>詐欺サイトの可能性があるURLが含まれているため、送信できません。",
            "NSFWサイトへの誘導": "<:cross:1394240624202481705>NSFWサイトへの誘導が含まれているため、送信できません。",
            "NGワードが含まれたメッセージの送信": "<:cross:1394240624202481705>NGワードが含まれているため、送信できません。",
        }
        dm_text = reason_messages.get(reason, f"⚠️ {reason} が検出されたため、送信できません。")

        # ==== タイムアウト有効時 ====
        if cfg_section.get("timeout"):
            try:
                await member.timeout(timedelta(minutes=5), reason=reason)
                success = True
                # DM送信
                embed_dm = discord.Embed(description=f"<:rightSort:1401174996574801950>理由:{reason}")
                embed_dm.set_author(
                    name=f"あなたは{member.guild.name}で5分間タイムアウトとなりました。",
                    icon_url=member.display_avatar.url
                )
                if not member.bot:
                    try:
                        await member.send(embed=embed_dm)
                        dmsent = "はい"
                    except discord.Forbidden:
                        dmreason = "\n<:space:1416299781869015081><:rightSort:1401174996574801950>**理由**:受信拒否"
                    except Exception as e:
                        dmreason = f"\n<:space:1416299781869015081><:rightSort:1401174996574801950>**理由**:{e}"
            except Exception as e:
                success = False
                dmreason = f"{e}"

            # チャンネル通知（成功時のみ）
            if success:
                embed_channel = discord.Embed(
                    description=f"<:timeoutAdd:1394658819556245667>{member.mention}を5分間タイムアウトしました。\n"
                                f"<:space:1416299781869015081><:rightArrow:1416300337614159923>理由:{reason}",
                    color=discord.Color.yellow()
                )
                await message.channel.send(embed=embed_channel)


        # ==== タイムアウト無効時 ====
        else:
            embed_dm = discord.Embed(description=dm_text, color=discord.Color.red())
            embed_dm.set_author(
                name=member.guild.name,
                icon_url=member.guild.icon.url if member.guild.icon else None
            )
            if not member.bot:
                try:
                    await member.send(embed=embed_dm)
                    dmsent = "はい"
                    success = True
                except discord.Forbidden:
                    dmreason = "\n<:space:1416299781869015081><:rightSort:1401174996574801950>**理由**:受信拒否"
                except Exception as e:
                    dmreason = f"\n<:space:1416299781869015081><:rightSort:1401174996574801950>**理由**:{e}"

        # ==== サーバーログ通知 ====
        if success and cfg_section.get("timeout"):
            # タイムアウト成功時
            embed_log = discord.Embed(
                description=(
                    f"**<:timeoutAdd:1394658819556245667>{member.mention}を5分間タイムアウトしました。**\n"
                    f"<:space:1416299781869015081><:rightSort:1401174996574801950>**理由:**{reason}\n"
                    f"詳細\nDMの送信:{dmsent}{dmreason}"
                ),
                color=discord.Color.yellow(),
                timestamp=discord.utils.utcnow()
            )
        elif success:
            # 警告(DM送信)成功時
            embed_log = discord.Embed(
                description=(
                    f"**<:warn:1394241229176311888>{member.mention}のメッセージを削除し、警告を送信しました。**\n"
                    f"<:space:1416299781869015081><:rightSort:1401174996574801950>**理由:**{reason}\n"
                    f"詳細\nDMの送信:{dmsent}{dmreason}"
                ),
                color=discord.Color.orange(),
                timestamp=discord.utils.utcnow()
            )
        else:
            # 失敗時
            embed_log = discord.Embed(
                description=(
                    f"<:warn:1394241229176311888>{member.mention}のタイムアウトに失敗しました。\n"
                    f"<:space:1416299781869015081><:rightSort:1401174996574801950>**理由:**{reason}\n"
                    f"詳細\n失敗理由:{dmreason}"
                ),
                color=discord.Color.red(),
                timestamp=discord.utils.utcnow()
            )

        embed_log.set_author(name=member.name, icon_url=member.display_avatar.url)
        embed_log.set_footer(text=f"ユーザーID: {member.id}")

        serverlog = await serverlog_collection.find_one({"_id": message.guild.id})
        if serverlog:
            log_ch = message.guild.get_channel(serverlog.get("log_channel_id"))
            if log_ch:
                try:
                    await log_ch.send(embed=embed_log)
                except Exception as e:
                    print(f"ログ送信失敗: {e}")

    @automod.error
    async def automod_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(f"<:cross:1394240624202481705> このコマンドを使うには管理者権限が必要です。", ephemeral=True)


async def setup(bot):
    await bot.add_cog(AutoMod(bot))
