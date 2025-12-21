import discord
from discord.ext import commands
import traceback
import random
import string
import io
import datetime
import os

ERROR_TRACEBACK_CHANNEL_ID = 1394294521113612318
ERROR_LOG_DIR = "error_logs"  # ローカル保存先

# フォルダがなければ作成
os.makedirs(ERROR_LOG_DIR, exist_ok=True)


class ErrorHandler(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def generate_error_code(self, length=6):
        """エラー識別コードを生成"""
        return ''.join(random.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=length))

    async def send_error_traceback(self, ctx_or_inter, error_id, error_text, filename, guild_name):
        """エラー情報をEmbed＋ファイルで送信し、ローカルにも保存"""
        channel = self.bot.get_channel(ERROR_TRACEBACK_CHANNEL_ID)
        safe_guild_name = (guild_name or "DM").replace(" ", "_").replace("/", "_")
        safe_filename = (filename or "Unknown").replace(" ", "_").replace("/", "_")
        file_name = f"errorTraceback-{safe_guild_name}-{safe_filename}-{error_id}.txt"

        # === ローカルに保存 ===
        local_path = os.path.join(ERROR_LOG_DIR, file_name)
        with open(local_path, "w", encoding="utf-8") as f:
            f.write(f"=== エラー情報 ===\n")
            f.write(f"サーバー: {guild_name or 'DM'}\n")
            f.write(f"ファイル/コマンド: {filename}\n")
            if hasattr(ctx_or_inter, "user"):
                f.write(f"ユーザー: {ctx_or_inter.user} ({ctx_or_inter.user.id})\n")
            elif hasattr(ctx_or_inter, "author"):
                f.write(f"ユーザー: {ctx_or_inter.author} ({ctx_or_inter.author.id})\n")
            f.write(f"発生日時: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("\n=== Traceback ===\n")
            f.write(error_text)

        # === Discord送信用ファイル作成 ===
        file = discord.File(io.BytesIO(error_text.encode('utf-8')), filename=file_name)

        # === Embed作成 ===
        embed = discord.Embed(
            title=f"<:error:1394294289353277582> エラー発生（コード: {error_id}）",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )

        guild_info = guild_name or "DM / 不明"
        user_info = (
            f"{ctx_or_inter.user} ({ctx_or_inter.user.id})"
            if hasattr(ctx_or_inter, "user")
            else f"{ctx_or_inter.author} ({ctx_or_inter.author.id})"
        )

        embed.add_field(name="サーバー", value=guild_info, inline=False)
        embed.add_field(name="ユーザー", value=user_info, inline=False)
        if hasattr(ctx_or_inter, "command") and ctx_or_inter.command:
            embed.add_field(name="コマンド", value=ctx_or_inter.command.qualified_name, inline=False)

        # === Discordへ送信 ===
        if channel:
            await channel.send(embed=embed, file=file)
        else:
            print(f"[WARN] Error log channel ({ERROR_TRACEBACK_CHANNEL_ID}) が見つかりません。")

    # ============================================================
    # 通常コマンドのエラーハンドラ
    # ============================================================
    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        # この判定を差し替え
        if (
            hasattr(ctx.command, 'on_error')
            and callable(ctx.command.on_error)
        ) or (
            ctx.cog
            and hasattr(ctx.cog, 'cog_command_error')
            and ctx.cog.cog_command_error.__func__ is not commands.Cog.cog_command_error
        ):
            return
        if isinstance(error, (commands.CommandNotFound, commands.MissingPermissions)):
            return

        error_id = self.generate_error_code()
        error_text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        guild_name = ctx.guild.name if ctx.guild else "DM"
        filename = ctx.command.qualified_name if ctx.command else "不明コマンド"

        print(f"[ERROR CODE {error_id}] in {guild_name}\n{error_text}")

        await self.send_error_traceback(ctx, error_id, error_text, filename, guild_name)

        await ctx.send(
            f"<:error:1394294289353277582> コマンド実行中にエラーが発生しました。\nエラーコード: `{error_id}`",
            ephemeral=True if hasattr(ctx, 'interaction') else False
        )

    # ============================================================
    # スラッシュコマンドのエラーハンドラ
    # ============================================================
    @commands.Cog.listener()
    async def on_app_command_error(self, interaction: discord.Interaction, error):
        if isinstance(error, (discord.app_commands.errors.CommandNotFound, commands.MissingPermissions)):
            return

        error_id = self.generate_error_code()
        error_text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        guild_name = interaction.guild.name if interaction.guild else "DM"
        command_name = interaction.command.name if interaction.command else "不明コマンド"

        print(f"[ERROR CODE {error_id}] in {guild_name}\n{error_text}")

        await self.send_error_traceback(interaction, error_id, error_text, command_name, guild_name)

        try:
            msg = f"<:error:1394294289353277582> コマンド実行中にエラーが発生しました。\nエラーコード: `{error_id}`"
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


async def setup(bot):
    await bot.add_cog(ErrorHandler(bot))
