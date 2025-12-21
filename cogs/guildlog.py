from discord.ext import commands
import discord

class GuildLogCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.log_channel_id = 1399229118540812410

    async def send_log(self, embed: discord.Embed):
        log_channel = self.bot.get_channel(self.log_channel_id)
        if log_channel:
            try:
                await log_channel.send(embed=embed)
            except Exception as e:
                print(f"⚠️ ログ送信に失敗: {e}")

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        embed = discord.Embed(
            color=discord.Color.green()
        )
        if guild.icon:
            embed.set_author(name=f"{guild.name}(ID:{guild.id})に参加しました。", icon_url=guild.icon.url)
        else:
            embed.set_author(name=f"{guild.name}(ID:{guild.id})に参加しました。", icon_url=None)
        await self.send_log(embed)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        embed = discord.Embed(
            color=discord.Color.red()
        )
        if guild.icon:
            embed.set_author(name=f"{guild.name}(ID:{guild.id})から退出しました。", icon_url=guild.icon.url)
        else:
            embed.set_author(name=f"{guild.name}(ID:{guild.id})から退出しました。", icon_url=None)
        await self.send_log(embed)

async def setup(bot):
    await bot.add_cog(GuildLogCog(bot))