import discord
from discord.ext import commands
from discord import app_commands
import random
import asyncio
import time

from utils.economy_utils import format_coin, inc_stat
from utils.economy_db import (
    get_user, update_balance, users
)
from utils.cooldowns import check_cooldown
from typing import List, Tuple, Dict

TABLE_LIMIT = 2000
MIN_BET = 1

EMOJI = {
    "clubs": {
        "2": "<:club_2:1439578581935067136>",
        "3": "<:club_3:1439578607897809019>",
        "4": "<:club_4:1439578631142772890>",
        "5": "<:club_5:1439578651627753523>",
        "6": "<:club_6:1439578684003455076>",
        "7": "<:club_7:1439579224620138546>",
        "8": "<:club_8:1439579285433225307>",
        "9": "<:club_9:1439579309307068456>",
        "10": "<:club_10:1439579331058733188>",
        "J": "<:club_J:1439579376529309787>",
        "Q": "<:club_Q:1439579432124678146>",
        "K": "<:club_K:1439579396154327040>",
        "A": "<:club_A:1439579351653027840>",
    },
    "diamonds": {
        "2": "<:diamond_2:1439579461086482524>",
        "3": "<:diamond_3:1439579491696640000>",
        "4": "<:diamond_4:1439579553965015133>",
        "5": "<:diamond_5:1439579582926950470>",
        "6": "<:diamond_6:1439579639931469844>",
        "7": "<:diamond_7:1439579669564358706>",
        "8": "<:diamond_8:1439579699587059852>",
        "9": "<:diamond_9:1439579732235522178>",
        "10": "<:diamond_10:1439579758231814206>",
        "J": "<:diamond_J:1439579836057387088>",
        "Q": "<:diamond_Q:1439579880424607804>",
        "K": "<:diamond_K:1439579857867505805>",
        "A": "<:diamond_A:1439579805266743428>",
    },
    "hearts": {
        "2": "<:heart_2:1439579944035418132>",
        "3": "<:heart_3:1439579994807603250>",
        "4": "<:heart_4:1439580021499887736>",
        "5": "<:heart_5:1439580044254121994>",
        "6": "<:heart_6:1439580072821522556>",
        "7": "<:heart_7:1439580105608269896>",
        "8": "<:heart_8:1439580132049424476>",
        "9": "<:heart_9:1439580158204837960>",
        "10": "<:heart_10:1439580202110947450>",
        "J": "<:heart_J:1439580255525539900>",
        "Q": "<:heart_Q:1439580309531263027>",
        "K": "<:heart_K:1439580284235284570>",
        "A": "<:heart_A:1439580229147557999>",
    },
    "spades": {
        "2": "<:spade_2:1439580355475804319>",
        "3": "<:spade_3:1439580379639185522>",
        "4": "<:spade_4:1439580411847118910>",
        "5": "<:spade_5:1439580436396376245>",
        "6": "<:spade_6:1439580463835644167>",
        "7": "<:spade_7:1439580492377755748>",
        "8": "<:spade_8:1439580516075700255>",
        "9": "<:spade_9:1439580544676532264>",
        "10": "<:spade_10:1439580577518059614>",
        "J": "<:spade_J:1439580629321650357>",
        "Q": "<:spade_Q:1439580673462632468>",
        "K": "<:spade_K:1439580650775511070>",
        "A": "<:spade_A:1439580602553860187>",
    }
}

RANKS = ["2","3","4","5","6","7","8","9","10","J","Q","K","A"]
SUITS = ["clubs","diamonds","hearts","spades"]
HL_REWARD = 150

MP_ROWS = 4
MP_COLUMNS = 5
MP_TOTAL = MP_ROWS * MP_COLUMNS
MP_INITIAL_HP = 12
MP_REWARD = 500

MATCHPAIRS_EMOJI = ["🥩","🍇","🍒","🫐","🥑","🍊","🍉","🍰","🍕","🍙"]

# ===== ヘルパー =====
def build_deck() -> List[Tuple[str,str,str]]:
    """(rank, suit, emoji) のタプルを52枚返す"""
    deck = []
    for suit in SUITS:
        for r in RANKS:
            deck.append((r, suit, EMOJI[suit][r]))
    random.shuffle(deck)
    return deck

def hand_value(cards: List[Tuple[str,str,str]]) -> int:
    """与えられた手札の最大21以下の値を返す（Aは1または11）"""
    total = 0
    aces = 0
    for r, s, e in cards:
        if r in ["J","Q","K"]:
            total += 10
        elif r == "A":
            total += 11
            aces += 1
        else:
            total += int(r)
    # Aを必要に応じて1に切り替える
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total

def fmt_cards(cards: List[Tuple[str,str,str]]) -> str:
    """カード絵文字列を返す"""
    return " ".join(e for (_,_,e) in cards)

def build_matchpairs_board():
    cards = MATCHPAIRS_EMOJI * 2 
    random.shuffle(cards)
    return cards


# ===== ブラックジャック Cog =====
class BlackjackView(discord.ui.View):
    def __init__(self, bot, ctx, game):
        super().__init__(timeout=120)
        self.bot = bot
        self.ctx = ctx
        self.game = game

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.ctx.author.id

    async def show_table(self, interaction, footer_text="", disable_view=False):
        g = self.game

        p_val = hand_value(g["player"])

        # 伏せ札を隠すモード
        if not g.get("reveal", False):
            # ディーラーは1枚だけ表示
            dealer_first = g["dealer"][0]
            dealer_line = f"ディーラー (??)\n## {dealer_first[2]}<:cardBack:1444637372791783424>"
        else:
            d_val = hand_value(g["dealer"])
            dealer_line = f"ディーラー ({d_val})\n## {fmt_cards(g['dealer'])}"

        # プレイヤー表示
        player_line = f"プレイヤー ({p_val})\n## {fmt_cards(g['player'])}"

        content = dealer_line + "\n\n" + player_line

        if footer_text:
            content += f"\n\n{footer_text}"

        if disable_view:
            await interaction.edit_original_response(content=content, view=None)
        else:
            await interaction.edit_original_response(content=content, view=self)
    
    async def dealer_turn(self, interaction: discord.Interaction):
        g = self.game

        # プレイヤーのバースト/ブラックジャックで勝負ついている場合は動かない
        p_val = hand_value(g["player"])
        if p_val > 21:
            return await self.resolve_outcome(interaction)

        # ディーラーの隠し札公開
        g["reveal"] = True
        await self.show_table(interaction, disable_view=True)

        deck = g["deck"]

        # 16以下なら引き続ける
        for _ in range(10):
            d_val = hand_value(g["dealer"])
            if d_val > 16:
                break

            if not deck:
                deck = build_deck()
                g["deck"] = deck

            g["dealer"].append(deck.pop())
            await asyncio.sleep(0.7)
            await self.show_table(interaction, disable_view=True)

        await asyncio.sleep(0.7)
        await self.resolve_outcome(interaction)
    
    async def resolve_outcome(self, interaction: discord.Interaction):
        """勝敗判定と支払い処理"""
        g = self.game
        p_val = hand_value(g["player"])
        d_val = hand_value(g["dealer"])
        bet = g["bet"]

        player_bust = p_val > 21
        dealer_bust = d_val > 21

        dealer_line = f"ディーラー ({d_val})\n## {fmt_cards(g['dealer'])}"
        player_line = f"プレイヤー ({p_val})\n## {fmt_cards(g['player'])}"

        result_text = dealer_line + "\n\n" + player_line + "\n"

        payout = 0
        note = ""

        # バースト
        if player_bust:
            payout = -bet
            result_text = (
                f"ディーラー ({d_val})\n## {fmt_cards(g['dealer'])}\n\n"
                f"プレイヤー ({p_val})  **バースト**\n## {fmt_cards(g['player'])}"
            )
            
        elif dealer_bust:
            payout = bet
            result_text = (
                f"ディーラー ({d_val})  **バースト**\n## {fmt_cards(g['dealer'])}\n\n"
                f"プレイヤー ({p_val})\n## {fmt_cards(g['player'])}"
            )


        else:
            # 通常勝負
            if p_val > d_val:
                payout = bet
            elif p_val < d_val:
                payout = -bet
            else:
                payout = 0

        # 支払い処理
        if payout > 0:
            await update_balance(self.ctx.guild.id, self.ctx.author.id, wallet_delta=payout)
            result_text += f"\n\n{format_coin(payout)}を獲得しました！"

        elif payout < 0:
            await update_balance(self.ctx.guild.id, self.ctx.author.id, wallet_delta=payout)
            result_text += f"\n\n{format_coin(-payout)}を失いました…"

        else:
            # ベット返却
            await update_balance(self.ctx.guild.id, self.ctx.author.id, wallet_delta=g["bet"])
            result_text += f"\n\n引き分け\n{format_coin(g['bet'])}は返金されました。"

        await interaction.edit_original_response(content=result_text, view=None)

    # ===== ボタン =====
    @discord.ui.button(label="ヒット", emoji="<:buttonHit:1444658586159419392>", style=discord.ButtonStyle.success)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        self.game["reveal"] = False
        g = self.game
        deck = g["deck"]

        if not deck:
            deck = build_deck()
            g["deck"] = deck

        g["player"].append(deck.pop())
        p_val = hand_value(g["player"])

        # バースト
        if p_val > 21:
            self.game["reveal"] = True
            await self.show_table(interaction, disable_view=True)
            await asyncio.sleep(1)
            await self.resolve_outcome(interaction)
            self.stop()
            return

        # 21 → 自動スタンド（ディーラーターンへ）
        if p_val == 21:
            self.game["reveal"] = True
            await self.show_table(interaction, disable_view=True)
            await asyncio.sleep(1)
            await self.resolve_outcome(interaction)
            self.stop()
            return

        # 通常ヒット
        await self.show_table(interaction)

    @discord.ui.button(label="スタンド", emoji="<:rightSort:1401174996574801950>", style=discord.ButtonStyle.danger)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        self.game["reveal"] = True
        await interaction.edit_original_response(view=None)
        await self.dealer_turn(interaction)
        self.stop()

    @discord.ui.button(label="ダブル", emoji="<:doubleRightArrow:1444664108611014817>", style=discord.ButtonStyle.primary)
    async def dbl(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        g = self.game
        user = await get_user(self.ctx.guild.id, self.ctx.author.id)

        # 所持金チェック
        if user["wallet"] < g["bet"]:
            return await interaction.followup.send(
                "<:cross:1394240624202481705> ダブルするための所持金が足りません。",
                ephemeral=True
            )

        # 追加ベットを引く
        await update_balance(self.ctx.guild.id, self.ctx.author.id, wallet_delta=-g["bet"])
        g["bet"] *= 2

        deck = g["deck"]
        if not deck:
            deck = build_deck()
            g["deck"] = deck

        # プレイヤーに1枚だけ追加
        g["player"].append(deck.pop())
        p_val = hand_value(g["player"])

        # 表示（ボタン消す）
        await interaction.edit_original_response(
            view=None
        )
        self.game["reveal"] = True
        await self.dealer_turn(interaction)
        self.stop()

class HighLowView(discord.ui.View):
    def __init__(self, bot, ctx, game):
        super().__init__(timeout=60)
        self.bot = bot
        self.ctx = ctx
        self.game = game   # { deck, current, wins }

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.ctx.author.id

    async def draw_second_card(self):
        """同ランクは再抽選で 2 枚目を返す"""
        deck = self.game["deck"]
        first = self.game["current"]

        while True:
            if not deck:
                deck = build_deck()
                self.game["deck"] = deck

            second = deck.pop()

            r1 = RANKS.index(first[0])
            r2 = RANKS.index(second[0])

            if r1 != r2:
                return second

    async def process_guess(self, interaction, guess: str):
        g = self.game
        first = g["current"]

        # 2枚目決定
        second = await self.draw_second_card()

        r1 = RANKS.index(first[0])
        r2 = RANKS.index(second[0])

        win = (
            (guess == "HIGH" and r2 > r1) or
            (guess == "LOW" and r2 < r1)
        )

        # ---- 結果表示 ----
        result_text = f"## {first[2]}{second[2]}\n\n"

        if win:
            g["wins"] += 1
            total = g["wins"] * HL_REWARD

            # 勝利処理
            await update_balance(self.ctx.guild.id, self.ctx.author.id, wallet_delta=HL_REWARD)

            result_text += (
                f"🎉 **勝ち！**\n"
                f"{format_coin(HL_REWARD)} を獲得しました！"
            )

            # 一旦結果を表示
            await interaction.response.edit_message(content=result_text, view=None)

            # 演出として少し待つ
            await asyncio.sleep(2)

            # 次の試合へ進む
            g["current"] = second

            next_text = f"## {second[2]}<:cardBack:1444637372791783424>\n\n"

            new_view = HighLowView(self.bot, self.ctx, g)

            await interaction.edit_original_response(content=next_text, view=new_view)

        else:
            # 負け
            total = g["wins"] * HL_REWARD

            result_text += (
                "**負け！**\n"
                f"最終結果: **{format_coin(total)}** を獲得しました！"
            )

            await interaction.response.edit_message(content=result_text, view=None)
    # ===== ボタン =====
    @discord.ui.button(label="HIGH", emoji="<:buttonPlus:1444665079776808971>", style=discord.ButtonStyle.success)
    async def high_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_guess(interaction, "HIGH")

    @discord.ui.button(label="LOW", emoji="<:buttonMinus:1444665078015066182>", style=discord.ButtonStyle.danger)
    async def low_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_guess(interaction, "LOW")
        
class MatchPairsButton(discord.ui.Button):
    def __init__(self, index, emoji_value, view_ref):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="",
            emoji="<:space:1416299781869015081>",
            row=index // MP_COLUMNS,
            custom_id=f"mp_{index}"
        )
        self.index = index
        self.emoji_value = emoji_value
        self.view_ref = view_ref

    async def callback(self, interaction: discord.Interaction):
        await self.view_ref.on_click(interaction, self.index)


class MatchPairsView(discord.ui.View):
    def __init__(self, bot, ctx, board):
        super().__init__(timeout=0)
        self.bot = bot
        self.ctx = ctx
        self.board = board
        self.hp = MP_INITIAL_HP

        self.revealed = []        
        self.pending_hide = None  
        self.matched = set()      

        for i in range(MP_TOTAL):
            btn = MatchPairsButton(i, board[i], self)
            self.add_item(btn)

    def get_button(self, idx):
        for b in self.children:
            if isinstance(b, MatchPairsButton) and b.index == idx:
                return b
        return None

    def status_text(self, miss=False):
        matched_pairs = len(self.matched) // 2
        text = f"残りHP: **{self.hp}** | 揃ったペア: **{matched_pairs}/10**\n"
        text += "カードをめくってください。"
        return text

    async def on_click(self, i: discord.Interaction, idx: int):
        # ▼ ここ：前の失敗ペアを伏せる処理
        if self.pending_hide:
            a, b = self.pending_hide
            for x in (a, b):
                btn = self.get_button(x)

                btn.label = ""
                btn.emoji = "<:space:1416299781869015081>"
                btn.style = discord.ButtonStyle.secondary

            self.revealed = []
            self.pending_hide = None
            await i.response.edit_message(content=self.status_text(), view=self)
            return

        if idx in self.matched:
            await i.response.defer()
            return

        btn = self.get_button(idx)
        # ▼ 表にするとき
        btn.label = ""
        btn.emoji = self.board[idx]
        btn.style = discord.ButtonStyle.primary
        self.revealed.append(idx)

        if len(self.revealed) == 1:
            return await i.response.edit_message(content=self.status_text(), view=self)

        a, b = self.revealed
        if self.board[a] == self.board[b]:
            # 成功
            self.matched.update({a, b})
            for x in (a, b):
                bb = self.get_button(x)
                bb.label = ""
                bb.emoji = self.board[x]
                bb.style = discord.ButtonStyle.success
                bb.disabled = True

            self.revealed = []

            if len(self.matched) == MP_TOTAL:
                total = self.hp * MP_REWARD
                await update_balance(self.ctx.guild.id, self.ctx.author.id, wallet_delta=total)
                msg = f"結果: **勝ち！**\n{format_coin(total)} を獲得しました！"

                for c in self.children:
                    c.disabled = True

                await i.response.edit_message(content=msg, view=None)
                return

            return await i.response.edit_message(content=self.status_text(), view=self)
        else:
            # 失敗 → pending に入れる
            self.hp -= 1
            self.pending_hide = [a, b]

            if self.hp <= 0:
                for c in self.children:
                    c.disabled = True
                await i.response.edit_message(
                    content=f"結果:**負け**\n次回頑張りましょう。",
                    view=None
                )
                return

            return await i.response.edit_message(
                content=self.status_text(miss=True),
                view=self
            )




class EconomyGame(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.games: Dict[str, dict] = {}

    # ===============================
    # 🎣 FISH（釣り）
    # ===============================
    @commands.hybrid_command(name="fish", description="釣りをします。")
    async def fish(self, ctx: commands.Context):
        user = await get_user(ctx.guild.id, ctx.author.id)
        extra_msg = ""
        inventory = user.get("inventory")

        # 🔒 inventory が dict じゃなければ初期化
        if not isinstance(inventory, dict):
            inventory = {}
            await users.update_one(
                {"_id": f"{ctx.guild.id}-{ctx.author.id}"},
                {"$set": {"inventory": inventory}}
            )

        special_rod = inventory.get("特殊な釣り竿", 0)

        guild_id = ctx.guild.id
        user_id = ctx.author.id
        ok, remain = await check_cooldown(guild_id, user_id, "fish")
        if not ok:
            until = int(time.time() + remain)
            await ctx.reply(
                f"<:warn:1394241229176311888> クールダウン中です。<t:{until}:R>に再度実行してください。"
            )
            return

        user = await get_user(ctx.guild.id, ctx.author.id)
        inventory = user.get("inventory", {})
        special_rod = inventory.get("特殊な釣り竿", 0)
        await inc_stat(ctx.guild.id, ctx.author.id, "fish")

        # 1️⃣ メッセージ送信（段階1）
        msg = await ctx.reply("釣り竿を投げました！")
        await asyncio.sleep(3)

        # 2️⃣ メッセージ編集（段階2）
        await msg.edit(content="釣り竿を投げました！\nかじられた感触がします…")
        await asyncio.sleep(3)

        # ===============================
        # 🎣 特殊な釣り竿なし
        # ===============================
        if special_rod == 0:
            roll = random.randint(1, 100)

            if roll <= 60:
                # ❌ 逃げられ
                await msg.edit(content="釣り竿を投げました！\nかじられた感触がします…\n逃げられました…")
                return

            # 🎉 当たり（40%）
            reward = random.randint(600, 1400)

            await update_balance(ctx.guild.id, ctx.author.id, reward)

            # 🎁 レア収集品抽選（1%）
            if random.randint(1, 100) == 1:
                collections = user.get("collections")

                # 念のため list → dict 修正
                if not isinstance(collections, dict):
                    collections = {}
                    await users.update_one(
                        {"_id": f"{ctx.guild.id}-{ctx.author.id}"},
                        {"$set": {"collections": collections}}
                    )

                # まだ持っていない場合のみ付与
                if "🐠 - 熱帯魚" not in collections:
                    await users.update_one(
                        {"_id": f"{ctx.guild.id}-{ctx.author.id}"},
                        {"$set": {"collections.🐠 - 熱帯魚": 1}}
                    )
                    extra_msg = '\n🎉 さらに、収集品 **"🐠 - 熱帯魚"** を獲得しました！'
                else:
                    extra_msg = ""  # すでに持っている場合は何も出さない

            await msg.edit(
                content=(
                    f"釣り竿を投げました！\nかじられた感触がします…\n1匹の魚と**{format_coin(reward)}**を獲得しました！"
                    f"{extra_msg}"
                )
            )
            return

        # ===============================
        # 🎣 特殊な釣り竿あり（使う）
        # ===============================
        # 特殊な釣り竿を1つ消費
        inventory["特殊な釣り竿"] -= 1
        remain = inventory["特殊な釣り竿"]
        await users.update_one(
            {"_id": f"{ctx.guild.id}-{ctx.author.id}"},
            {"$set": {"inventory": inventory}}
        )

        roll = random.randint(1, 100)

        if roll <= 40:
            reward = random.randint(150, 350)
            result_text = f"釣り竿を投げました！\nかじられた感触がします…\n逃げられましたが、特殊な釣り竿により**{format_coin(reward)}**を獲得しました。"
        else:
            reward = random.randint(1500, 1600)
            result_text = f"釣り竿を投げました！\nかじられた感触がします…\n1匹の魚と**{format_coin(reward)}**を獲得しました！"

        await update_balance(ctx.guild.id, ctx.author.id, reward)

        # 🎁 レア収集品抽選（1%） ※特殊竿でも出る
        if random.randint(1, 100) == 1:
            collections = user.get("collections")

            # 念のため list → dict 修正
            if not isinstance(collections, dict):
                collections = {}
                await users.update_one(
                    {"_id": f"{ctx.guild.id}-{ctx.author.id}"},
                    {"$set": {"collections": collections}}
                )

            # まだ持っていない場合のみ付与
            if "🐠 - 熱帯魚" not in collections:
                await users.update_one(
                    {"_id": f"{ctx.guild.id}-{ctx.author.id}"},
                    {"$set": {"collections.🐠 - 熱帯魚": 1}}
                )
                extra_msg = '\n🎉 さらに、収集品 **"🐠 - 熱帯魚"** を獲得しました！'
            else:
                extra_msg = ""  # すでに持っている場合は何も出さない

        await msg.edit(
            content=(
                f"{result_text}"
                f"{extra_msg}"
                f"\n特殊な釣り竿を使用しました。残りは**{remain}個**です。"
            )
        )

    # ===============================
    # 🃏 BLACKJACK
    # ===============================
    @commands.hybrid_command(name="blackjack", description="ブラックジャックをプレイします。", aliases=["bj"])
    @app_commands.rename(bet="賭け金")
    async def blackjack(self, ctx: commands.Context, bet: int):
        # checks
        if bet < MIN_BET:
            return await ctx.reply(f"<:cross:1394240624202481705> 最低賭け金は{format_coin(MIN_BET)}です。", ephemeral=True)
        if bet > TABLE_LIMIT:
            return await ctx.reply(f"<:warn:1394241229176311888> テーブルリミットは{format_coin(TABLE_LIMIT)}です。賭け金を下げてください。", ephemeral=True)

        user = await get_user(ctx.guild.id, ctx.author.id)
        if user["wallet"] < bet:
            return await ctx.reply("<:cross:1394240624202481705> 所持金が不足しています。", ephemeral=True)

        key = f"{ctx.guild.id}-{ctx.author.id}"
        if key in self.games:
            return await ctx.reply("<:warn:1394241229176311888> 既にブラックジャックをプレイ中です。", ephemeral=True)

        # デッキ作成
        deck = build_deck()

        # 初期配り：プレイヤー2枚、ディーラー2枚（1枚はフェイスダウン）
        player = [deck.pop(), deck.pop()]
        dealer = [deck.pop(), deck.pop()]

        # 賭け金を先に差し引く（勝ち時に戻す/増やす）
        await update_balance(ctx.guild.id, ctx.author.id, wallet_delta=-bet)

        # --- 初手の合計値計算 ---
        p_val = hand_value(player)
        d_val = hand_value(dealer)

        player_bj = (p_val == 21 and len(player) == 2)
        dealer_bj = (d_val == 21 and len(dealer) == 2)

        # ===== 初手ブラックジャック処理 =====
        if player_bj or dealer_bj:
            # 両者ブラックジャック → 引き分け
            if player_bj and dealer_bj:
                # ベット返金
                await update_balance(ctx.guild.id, ctx.author.id, wallet_delta=bet)

                return await ctx.reply(
                    f"ディーラー ({d_val})  **ブラックジャック**\n## {fmt_cards(dealer)}\n\n"
                    f"プレイヤー ({p_val})  **ブラックジャック**\n## {fmt_cards(player)}\n\n"
                    f"引き分け\n{format_coin(bet)} は返金されました。",
                    ephemeral=False
                )

            # プレイヤーのみ BJ → 勝利
            if player_bj:
                reward = bet  # 標準2倍（1倍返却 + 1倍利益）

                await update_balance(ctx.guild.id, ctx.author.id, wallet_delta=reward)

                return await ctx.reply(
                    f"ディーラー ({d_val})\n## {fmt_cards(dealer)}\n\n"
                    f"プレイヤー ({p_val})  **ブラックジャック**\n## {fmt_cards(player)}\n\n"
                    f"{format_coin(reward)} を獲得しました！",
                    ephemeral=False
                )

            # ディーラーのみ BJ → プレイヤー負け
            if dealer_bj:
                return await ctx.reply(
                    f"ディーラー ({d_val})  **ブラックジャック**\n## {fmt_cards(dealer)}\n\n"
                    f"プレイヤー ({p_val})\n## {fmt_cards(player)}\n\n"
                    f"{format_coin(bet)} を失いました…",
                    ephemeral=False
                )


        game = {
            "deck": deck,
            "player": player,
            "dealer": dealer,
            "bet": bet,
            "initial_player": list(player),
            "initial_dealer": list(dealer),
        }
        self.games[key] = game

        p_val = hand_value(player)
        # 表示：ディーラーは1枚隠し
        player_line = f"プレイヤー ({p_val})\n## {fmt_cards(player)}"
        dealer_line = f"ディーラー (??)\n## {dealer[0][2]}<:cardBack:1444637372791783424>"

        # view with buttons
        view = BlackjackView(self.bot, ctx, game)

        # 保存されたゲームは resolve_outcome 内で決着後に消す
        async def cleanup_task():
            # wait for view to finish then remove game key
            await view.wait()
            self.games.pop(key, None)

        # start cleanup in background (no blocking)
        self.bot.loop.create_task(cleanup_task())

        if ctx.interaction:
            await ctx.interaction.response.send_message(
                f"ディーラー (??)\n## {dealer[0][2]}<:cardBack:1444637372791783424>\n\n{player_line}",
                view=view
            )
        else:
            await ctx.reply(
                f"ディーラー (??)\n## {dealer[0][2]}<:cardBack:1444637372791783424>\n\n{player_line}",
                view=view
            )

    # ===============================
    # 🔼 HIGH & LOW
    # ===============================
    @commands.hybrid_command(name="highlow", description="ハイ&ローをプレイします。")
    async def highlow(self, ctx: commands.Context):

        deck = build_deck()
        first = deck.pop()

        game = {
            "deck": deck,
            "current": first,
            "wins": 0,
        }

        text = (f"## {first[2]}<:cardBack:1444637372791783424>\n\n")

        view = HighLowView(self.bot, ctx, game)

        if ctx.interaction:
            await ctx.interaction.response.send_message(text, view=view)
        else:
            await ctx.reply(text, view=view)

    # ===============================
    # 🧠 MATCH PAIRS（神経衰弱）
    # ===============================
    @commands.hybrid_command(name="matchpairs", description="神経衰弱をプレイします。")
    async def matchpairs(self, ctx: commands.Context):

        key = f"{ctx.guild.id}-{ctx.author.id}"
        if key in self.games:
            return await ctx.reply("<:warn:1394241229176311888> 既にゲーム中です。", ephemeral=True)

        board = build_matchpairs_board()
        view = MatchPairsView(self.bot, ctx, board)
        self.games[key] = view

        async def cleanup():
            await view.wait()
            self.games.pop(key, None)

        self.bot.loop.create_task(cleanup())

        msg = f"残りHP: {MP_INITIAL_HP}\nカードをめくってください。"

        if ctx.interaction:
            await ctx.interaction.response.send_message(msg, view=view)
        else:
            await ctx.reply(msg, view=view)

async def setup(bot):
    await bot.add_cog(EconomyGame(bot))
