import discord
from discord.ext import commands
import json

def load_commands():
    with open("commands.json", "r", encoding="utf-8") as f:
        return json.load(f)


class HelpView(discord.ui.View):
    def __init__(self, embeds, author: discord.User, timeout=None):
        super().__init__(timeout=timeout)
        self.embeds = embeds
        self.index = 0
        self.author = author

    async def update_message(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=self.embeds[self.index], view=self)

    @discord.ui.button(emoji="<:leftSort:1401175053973848085>", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.author:
            return
        self.index = (self.index - 1) % len(self.embeds)
        await self.update_message(interaction)

    @discord.ui.button(emoji="<:rightSort:1401174996574801950>", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.author:
            return
        self.index = (self.index + 1) % len(self.embeds)
        await self.update_message(interaction)

class HelpCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="help", description="ヘルプを表示します。")
    async def help_command(self, ctx: commands.Context):
        commands_data = load_commands()
        embeds = []

        # 1ページに15個ずつ区切る
        chunk_size = 15
        for i in range(0, len(commands_data), chunk_size):
            chunk = commands_data[i:i + chunk_size]
            embed = discord.Embed(
                title="📖 ヘルプ",
                description="利用可能なコマンド一覧",
                color=discord.Color.blurple()
            )
            for cmd in chunk:
                embed.add_field(
                    name=f"`{ctx.prefix}{cmd['name']}`",
                    value=cmd["description"],
                    inline=False
                )
            embed.set_footer(text=f"ページ {i // chunk_size + 1}/{(len(commands_data) - 1) // chunk_size + 1}")
            embeds.append(embed)

        view = HelpView(embeds, ctx.author)
        await ctx.send(embed=embeds[0], view=view)


async def setup(bot):
    await bot.add_cog(HelpCog(bot))
