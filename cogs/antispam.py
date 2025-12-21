import discord
from discord.ext import commands
from datetime import timedelta
from motor.motor_asyncio import AsyncIOMotorClient
import re
import os
from dotenv import load_dotenv

load_dotenv()

mongo_client = AsyncIOMotorClient(os.getenv("MONGO_URI"))
db = mongo_client["antispam"]
config_collection = db["configs"]
log_collection = db["spam_logs"]
db1 = mongo_client["serverlog"]
serverlog_collection = db1["channel.serverlog.channel"]

def default_config():
    return {
        "message": {"enabled": False, "count": 5, "seconds": 8},
        "attachments": {"enabled": False, "max": 3},
        "emoji": {"enabled": False, "max": 10},
        "newlines": {"enabled": False, "max": 10},
    }

def is_trusted_moderator(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return (
        perms.administrator
        or perms.manage_messages
        or perms.moderate_members
    )

async def get_config(guild_id: int) -> dict:
    cfg = await config_collection.find_one({"_id": guild_id})
    if not cfg:
        cfg = default_config()
        cfg["_id"] = guild_id
        await config_collection.insert_one(cfg)
    return cfg

async def is_exempted(guild_id: int, channel_id: int, user_id: int, check_type: str):
    exception_db = mongo_client["automod"]["exceptions"]

    # チャンネル例外
    ch_data = await exception_db.find_one({"_id": f"{guild_id}-channel-{channel_id}"})
    if ch_data and ch_data.get(check_type):
        return True

    # ユーザー例外
    user_data = await exception_db.find_one({"_id": f"{guild_id}-user-{user_id}"})
    if user_data and user_data.get(check_type):
        return True

    return False


class AntiSpam(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.user_messages = {}

    # --- ハイブリッドグループ ---
    @commands.hybrid_group(name="antispam", description="アンチスパム設定", invoke_without_command=True)
    @commands.has_permissions(administrator=True)
    async def antispam(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send("利用可能なサブコマンド: `message`, `attachments`, `emoji`, `newlines`")

    # --- /antispam message ---
    @antispam.command(name="message", description="連投スパム対策設定をします。")
    @commands.has_permissions(administrator=True)
    async def message(
        self,
        ctx: commands.Context,
        有効: bool,
        回数: int = 5,
        秒数: int = 3
    ):
    
        if not ctx.author.guild_permissions.administrator:
            await ctx.reply("<:cross:1394240624202481705> このコマンドを実行するには管理者権限が必要です。", ephemeral=True)
            return
        
        cfg = await get_config(ctx.guild.id)
        cfg["message"].update({"enabled": 有効, "count": 回数, "seconds": 秒数})
        await config_collection.update_one({"_id": ctx.guild.id}, {"$set": {"message": cfg["message"]}})
        await ctx.send(f"<:check:1394240622310850580>連投スパム検知を {'有効' if 有効 else '無効'} にしました。\n閾値: {回数} 回 / {秒数} 秒")

    # --- /antispam attachments ---
    @antispam.command(name="attachments", description="添付ファイルスパム対策設定をします。")
    async def attachments(
        self,
        ctx: commands.Context,
        有効: bool,
        最大数: int = 3
    ):
        if not ctx.author.guild_permissions.administrator:
            await ctx.reply("<:cross:1394240624202481705> このコマンドを実行するには管理者権限が必要です。", ephemeral=True)
            return
        
        cfg = await get_config(ctx.guild.id)
        cfg["attachments"].update({"enabled": 有効, "max": 最大数})
        await config_collection.update_one({"_id": ctx.guild.id}, {"$set": {"attachments": cfg["attachments"]}})
        await ctx.send(f"<:check:1394240622310850580>添付ファイルスパム検知を {'有効' if 有効 else '無効'} にしました。\n閾値: {最大数} 個")

    # --- /antispam emoji ---
    @antispam.command(name="emoji", description="絵文字スパム対策設定をします。")
    async def emoji(
        self,
        ctx: commands.Context,
        有効: bool,
        最大数: int = 10
    ):
        if not ctx.author.guild_permissions.administrator:
            await ctx.reply("<:cross:1394240624202481705> このコマンドを実行するには管理者権限が必要です。", ephemeral=True)
            return
        
        cfg = await get_config(ctx.guild.id)
        cfg["emoji"].update({"enabled": 有効, "max": 最大数})
        await config_collection.update_one({"_id": ctx.guild.id}, {"$set": {"emoji": cfg["emoji"]}})
        await ctx.send(f"<:check:1394240622310850580>絵文字スパム検知を {'有効' if 有効 else '無効'} にしました。\n閾値: {最大数} 個")

    # --- /antispam newlines ---
    @antispam.command(name="newlines", description="改行スパム対策設定をします。")
    async def newlines(
        self,
        ctx: commands.Context,
        有効: bool,
        最大行数: int = 10
    ):

        if not ctx.author.guild_permissions.administrator:
            await ctx.reply("<:cross:1394240624202481705> このコマンドを実行するには管理者権限が必要です。", ephemeral=True)
            return
        
        cfg = await get_config(ctx.guild.id)
        cfg["newlines"].update({"enabled": 有効, "max": 最大行数})
        await config_collection.update_one({"_id": ctx.guild.id}, {"$set": {"newlines": cfg["newlines"]}})
        await ctx.send(f"<:check:1394240622310850580>改行スパム検知を {'有効' if 有効 else '無効'} にしました。\n閾値: {最大行数} 行")

    # --- メッセージ監視 ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return

        if is_trusted_moderator(message.author):
            return

        cfg = await get_config(message.guild.id)
        reason = None

        # 🔹 各スパム検知前に例外判定を追加
        if cfg["message"]["enabled"]:
            if not await is_exempted(message.guild.id, message.channel.id, message.author.id, "spam_message"):
                user_msgs = self.user_messages.setdefault(message.guild.id, {}).setdefault(message.author.id, [])
                now = message.created_at.timestamp()
                user_msgs.append((now, message.id))
                user_msgs[:] = [(t, mid) for t, mid in user_msgs if now - t <= cfg["message"]["seconds"]]
                if len(user_msgs) >= cfg["message"]["count"]:
                    reason = "メッセージスパム"

        if cfg["attachments"]["enabled"]:
            if not await is_exempted(message.guild.id, message.channel.id, message.author.id, "spam_attachment"):
                if len(message.attachments) >= cfg["attachments"]["max"]:
                    reason = "添付ファイルスパム"

        if cfg["emoji"]["enabled"]:
            if not await is_exempted(message.guild.id, message.channel.id, message.author.id, "spam_emoji"):
                custom_emoji = re.findall(r"<a?:\w+:\d+>", message.content)
                unicode_emoji = re.findall(
                    r"[\U0001F1E6-\U0001F1FF]|"
                    r"[\U0001F300-\U0001F5FF]|"
                    r"[\U0001F600-\U0001F64F]|"
                    r"[\U0001F680-\U0001F6FF]|"
                    r"[\U0001F700-\U0001F77F]|"
                    r"[\U0001F780-\U0001F7FF]|"
                    r"[\U0001F800-\U0001F8FF]|"
                    r"[\U0001F900-\U0001F9FF]|"
                    r"[\U0001FA00-\U0001FA6F]|"
                    r"[\U0001FA70-\U0001FAFF]|"
                    r"[\u2600-\u26FF]|"
                    r"[\u2700-\u27BF]",
                    message.content)
                emoji_count = len(custom_emoji) + len(unicode_emoji)
                if emoji_count >= cfg["emoji"]["max"]:
                    reason = "絵文字スパム"

        if cfg["newlines"]["enabled"]:
            if not await is_exempted(message.guild.id, message.channel.id, message.author.id, "spam_newline"):
                if message.content.count("\n") >= cfg["newlines"]["max"]:
                    reason = "多数の改行メッセージの送信"

        if reason:
            await self.handle_spam(message, reason)


    async def handle_spam(self, message: discord.Message, reason: str):
        await message.delete()
        success = False
        dmsent = "いいえ"
        dmreason = " "
        member = message.author

        # タイムアウト試行
        try:
            await member.timeout(timedelta(minutes=5), reason=reason)
            success = True
            embed_dm = discord.Embed(
                description=f"<:rightSort:1401174996574801950>理由: {reason}",
            )
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

        # サーバーログ通知
        if success:
            embed_log = discord.Embed(
                description=(
                    f"**<:timeoutAdd:1394658819556245667>{member.mention}を5分間タイムアウトしました。**\n"
                    f"<:space:1416299781869015081><:rightSort:1401174996574801950>**理由:**{reason}\n"
                    f"詳細\nDMの送信:{dmsent}{dmreason}"
                ),
                color=discord.Color.yellow(),
                timestamp=discord.utils.utcnow()
            )
        else:
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

        # MongoDB保存
        await log_collection.insert_one({
            "guild_id": message.guild.id,
            "user_id": member.id,
            "reason": reason,
            "success": success,
            "channel_id": message.channel.id,
            "timestamp": message.created_at
        })
    @antispam.error
    async def verify_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(f"<:cross:1394240624202481705> このコマンドを使うには管理者権限が必要です。", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(AntiSpam(bot))
