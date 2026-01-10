import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import random
import asyncio
from datetime import datetime, timedelta, timezone
from motor.motor_asyncio import AsyncIOMotorClient
from utils.economy_utils import format_coin, inc_stat
from utils.economy_db import get_user, update_balance
from typing import List, Dict, Optional, Tuple
from collections import deque
from dotenv import load_dotenv

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
mongo = AsyncIOMotorClient(MONGO_URI) if MONGO_URI else None
db = mongo["economy_global"] if mongo else None

TICKETS_COLL = db["lottery_tickets"]
META_COLL = db["lottery_meta"]
SETTINGS_COLL = db["lottery_settings"]

TICKET_PRICE = 1000
MAX_PER_PURCHASE = 50

# payouts
PRIZE_1 = 1_000_000
PRIZE_2 = 10_000
PRIZE_3 = 500

# timezone JST
JST = timezone(timedelta(hours=9))

# helpers
def make_ticket():
    """組XX-YYYYYY 形式を返す"""
    group = random.randint(1, 99)
    num = random.randint(0, 999999)
    return f"{group:02d}組-{num:06d}"

def ticket_parts(ticket: str):
    """
    48組-676768 → ("48", "676768")
    """
    try:
        left, num = ticket.split("-")  # left = "48組"
        group = left.replace("組", "")  # "48"
        return group, num
    except Exception:
        return None, None

def now_jst():
    return datetime.now(JST)

async def get_current_round():
    doc = await META_COLL.find_one({"_id": "current"})
    if not doc:
        # init
        doc = {"_id": "current", "round": 1, "next_draw": None}
        now = now_jst()
        days_ahead = (7 - now.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        next_monday = (now + timedelta(days=days_ahead)).replace(hour=0, minute=0, second=0, microsecond=0)
        doc["next_draw"] = next_monday.isoformat()
        await META_COLL.insert_one(doc)
    return doc

async def bump_round_and_get_new_round():
    doc = await META_COLL.find_one_and_update(
        {"_id": "current"},
        {"$inc": {"round": 1}},
        upsert=True,
        return_document=True
    )
    if not doc:
        doc = await get_current_round()
    return doc["round"]

async def ensure_meta():
    doc = await META_COLL.find_one({"_id": "current"})
    if not doc:
        await get_current_round()

class LotteryView(discord.ui.View):
    def __init__(self, bot, result_doc, winners_summary, full_list_text, round_id):
        super().__init__(timeout=None)
        self.bot = bot
        self.result_doc = result_doc
        self.winners_summary = winners_summary
        self.full_list_text = full_list_text
        self.round_id = round_id

    @discord.ui.button(label="全番号表示", style=discord.ButtonStyle.secondary)
    async def show_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        if len(self.full_list_text) > 1900:
            # send as file
            fp = f"lottery_round_{self.round_id}_all.txt"
            with open(fp, "w", encoding="utf-8") as f:
                f.write(self.full_list_text)
            await interaction.response.send_message("全番号をファイルで送ります。", file=discord.File(fp), ephemeral=True)
        else:
            await interaction.response.send_message(f"全番号一覧:\n{self.full_list_text}", ephemeral=True)

class LotteryCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.draw_task_handle = None
        # start background scheduler once bot ready
        bot.loop.create_task(self._background_start())

    async def _background_start(self):
        # wait until bot is ready
        await self.bot.wait_until_ready()
        await ensure_meta()
        # start loop
        if not hasattr(self, "_draw_loop"):
            self._draw_loop = self.bot.loop.create_task(self._draw_scheduler())

    async def _draw_scheduler(self):
        # scheduler: sleep until next Monday 00:00 JST, then run draw, loop
        while True:
            meta = await get_current_round()
            next_draw_iso = meta.get("next_draw")
            if not next_draw_iso:
                # compute next monday
                now = now_jst()
                days_ahead = (7 - now.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7
                next_monday = (now + timedelta(days=days_ahead)).replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                next_monday = datetime.fromisoformat(next_draw_iso)
            now = now_jst()
            wait_seconds = (next_monday - now).total_seconds()
            if wait_seconds <= 0:
                # time to draw
                try:
                    await self.perform_draw(triggered_by=None)
                except Exception as e:
                    print(f"[Lottery] 自動抽選でエラー: {e}")
                # set next draw to following monday
                next_monday = (now + timedelta(days=(7 - now.weekday()) % 7 or 7)).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=7)
                await META_COLL.update_one({"_id": "current"}, {"$set": {"next_draw": next_monday.isoformat()}}, upsert=True)
                # small sleep before next loop
                await asyncio.sleep(5)
                continue
            # sleep but wake earlier if bot stops
            await asyncio.sleep(min(wait_seconds, 60*60))  # wake up hourly to re-evaluate

    async def perform_draw(self, triggered_by: commands.Context = None):
        meta = await get_current_round()
        round_id = meta["round"]

        cursor = TICKETS_COLL.find({"round": round_id})
        tickets = [doc async for doc in cursor]

        # =====================================
        # 🎯 1等：購入されたチケットから抽選
        # =====================================
        user_tickets = [t["ticket"] for t in tickets]

        if len(user_tickets) == 0:
            # 購入なし → ランダム生成して結果だけ通知
            winners_1 = [make_ticket() for _ in range(3)]
        elif len(user_tickets) <= 3:
            winners_1 = user_tickets[:]  # 全部当たり
        else:
            winners_1 = random.sample(user_tickets, 3)

        # 数字部分
        winners_nums = [ticket_parts(t)[1] for t in winners_1]

        payouts: Dict[int, int] = {}
        winners_list_lines = []
        full_list_lines = []

        # =====================================
        # 🎯 当選判定
        # =====================================
        full_list_lines = []   # ←すべての結果をここに集める

        for t in tickets:
            ticket_str = t["ticket"]
            user_id = t["user_id"]

            grp, num = ticket_parts(ticket_str)
            awarded = 0
            reason = None
            result_text = "はずれ..."

            # 1等
            if ticket_str in winners_1:
                awarded = PRIZE_1
                reason = f"1等 ({ticket_str})"
                result_text = "1等当選！"

            else:
                last3 = num[-3:]
                last1 = num[-1]

                # 2等
                if any(w[-3:] == last3 for w in winners_nums):
                    awarded = PRIZE_2
                    reason = f"2等 (下3桁一致:{last3})"
                    result_text = "2等当選！"

                # 3等
                elif any(w[-1] == last1 for w in winners_nums):
                    awarded = PRIZE_3
                    reason = f"3等 (下1桁一致:{last1})"
                    result_text = "3等当選！"

            # 結果行を保存（はずれ含む）
            full_list_lines.append(f"{ticket_str}  {result_text}")

            # 当選者一覧（1等だけ）
            if awarded > 0:
                payouts.setdefault(user_id, 0)
                payouts[user_id] += awarded

                if ticket_str in winners_1:   # ←1等だけ追加
                    winners_list_lines.append(
                        f"<@{user_id}> - {ticket_str} - {reason} - {format_coin(awarded)}"
                    )

        # =====================================
        # 🎯 結果保存
        # =====================================
        result_doc = {
            "round": round_id,
            "winners_1": winners_1,
            "drawn_at": now_jst().isoformat(),
            "total_tickets": len(tickets),
        }
        await db["lottery_results"].insert_one(result_doc)

        # 賞金配布
        for uid, amount in payouts.items():
            await update_balance(0, uid, wallet_delta=amount)

        winners_summary = "\n".join(winners_list_lines) if winners_list_lines else ""
        full_list_text = "\n".join(full_list_lines) if full_list_lines else "(購入なし)"

        # =====================================
        # 🎯 Embed 生成
        # =====================================

        first_prize_winners = []
        for t in tickets:
            if t["ticket"] in winners_1:
                first_prize_winners.append(f"<@{t['user_id']}> - {t['ticket']}")

        embed = discord.Embed(
            title="<:ticket:1414217916206813337>宝くじ抽選結果",
            description=f"ラウンド **{round_id}** の結果です。",
            color=discord.Color.gold()
        )

        # ---- 1等（番号）----
        embed.add_field(
            name="1等",
            value="\n".join(winners_1),
            inline=True
        )

        # ---- 2等（下3桁）----
        embed.add_field(
            name="2等",
            value=f"下3桁 **{winners_nums[0][-3:]}**",
            inline=True
        )

        # ---- 3等（下1桁）----
        embed.add_field(
            name="3等",
            value=f"下1桁 **{winners_nums[0][-1]}**",
            inline=True
        )

        # ---- 🔥 1等当選者一覧 ----
        if first_prize_winners:
            embed.add_field(
                name="1等当選者一覧",
                value="\n".join(first_prize_winners),
                inline=False
            )
        else:
            embed.add_field(
                name="1等当選者一覧",
                value="該当者なし",
                inline=False
            )

        # =====================================
        # 🎯 ギルド通知
        # =====================================
        async for cfg in SETTINGS_COLL.find({"notify_channel_id": {"$ne": None}}):
            gid = cfg["_id"]
            ch_id = cfg["notify_channel_id"]

            guild = self.bot.get_guild(gid)
            if not guild:
                try: ch = await self.bot.fetch_channel(ch_id)
                except: continue
            else:
                ch = guild.get_channel(ch_id)
                if not ch:
                    try: ch = await self.bot.fetch_channel(ch_id)
                    except: ch = None

            if ch:
                view = LotteryView(self.bot, result_doc, winners_summary, full_list_text, round_id)
                try:
                    await ch.send(embed=embed, view=view)
                except Exception as e:
                    print(f"[Lottery] 通知送信エラー guild {gid} ch {ch_id}: {e}")

        # =====================================
        # 🎯 DM通知
        # =====================================
        notified = set()
        for t in tickets:
            uid = t["user_id"]
            if uid in notified:
                continue
            notified.add(uid)

            try:
                user_obj = await self.bot.fetch_user(uid)
            except:
                user_obj = None

            dm_text = f"宝くじ ラウンド {round_id} の抽選が完了しました。\n"
            award = payouts.get(uid, 0)

            if award > 0:
                dm_text += f"🎉 おめでとうございます！ **{format_coin(award)}** を獲得しました！"
            else:
                dm_text += "今回は残念ながら当選がありませんでした。"

            if user_obj:
                try:
                    await user_obj.send(dm_text)
                except:
                    pass

        # =====================================
        # 🎯 ラウンド更新
        # =====================================
        await bump_round_and_get_new_round()

        now = now_jst()
        days_to_next = (7 - now.weekday()) % 7 or 7
        next_monday = (now + timedelta(days=days_to_next)).replace(hour=0, minute=0, second=0)
        await META_COLL.update_one({"_id": "current"}, {"$set": {"next_draw": next_monday.isoformat()}}, upsert=True)

    # -------------------------
    # Commands
    # -------------------------
    @app_commands.guilds()  # allow in guild contexts
    @commands.hybrid_group(name="lottery", description="宝くじコマンド")
    async def lottery(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.reply("利用可能: `/lottery buy`, `/lottery mytickets`, `/lottery notify`, `/lottery drawnow`")

    @lottery.command(name="buy", description="宝くじを購入します。1枚1000コイン（1ラウンド最大50枚）")
    @app_commands.rename(amount="購入枚数")
    async def buy(self, ctx: commands.Context, amount: int = 1):
        if amount < 1:
            return await ctx.reply("購入枚数は 1 以上にしてください。", ephemeral=True)

        # ────────────────
        # 🔥 ラウンドの総所持枚数チェック
        # ────────────────
        meta = await get_current_round()
        round_id = meta["round"]

        current_count = await TICKETS_COLL.count_documents({
            "round": round_id,
            "user_id": ctx.author.id
        })

        if current_count + amount > MAX_PER_PURCHASE:
            return await ctx.reply(
                f"このラウンドでは最大 {MAX_PER_PURCHASE} 枚までです。\n"
                f"現在 {current_count} 枚所持しています。\n"
                f"購入可能枚数：**{MAX_PER_PURCHASE - current_count} 枚まで**",
                ephemeral=True
            )
        # ────────────────

        # 所持金チェック
        user = await get_user(ctx.guild.id, ctx.author.id)
        total_cost = TICKET_PRICE * amount
        if user["wallet"] < total_cost:
            return await ctx.reply("<:cross:1394240624202481705> 所持金が不足しています。", ephemeral=True)

        # チャージ
        await update_balance(ctx.guild.id, ctx.author.id, wallet_delta=-total_cost)

        # 購入処理
        created = []
        docs = []
        for _ in range(amount):
            t = make_ticket()
            docs.append({
                "round": round_id,
                "ticket": t,
                "user_id": ctx.author.id,
                "guild_first": ctx.guild.id,
                "created_at": now_jst().isoformat()
            })
            created.append(t)
            await inc_stat(ctx.guild.id, ctx.author.id, "lottery")

        if docs:
            await TICKETS_COLL.insert_many(docs)

        await ctx.reply(
            f"宝くじを {amount} 枚購入しました！\n"
            f"現在の所持枚数：**{current_count + amount} / 50 枚**\n"
            f"支払い：{format_coin(total_cost)}"
        )    
    @lottery.command(name="mytickets", description="自分の保有チケットを確認します")
    async def mytickets(self, ctx: commands.Context):
        meta = await get_current_round()
        round_id = meta["round"]
        cursor = TICKETS_COLL.find({"round": round_id, "user_id": ctx.author.id})
        tickets = [doc async for doc in cursor]
        if not tickets:
            return await ctx.reply("今ラウンドのチケットはありません。", ephemeral=True)
        lines = [t["ticket"] for t in tickets]
        await ctx.reply(f"あなたのチケット（ラウンド {round_id}）:\n" + "\n".join(lines), ephemeral=True)

    @lottery.command(name="notify", description="このサーバーの宝くじ結果通知チャンネルを設定します。")
    @app_commands.describe(ch="通知先チャンネル（または空で解除）")
    @commands.has_permissions(manage_guild=True)
    async def notify(self, ctx: commands.Context, ch: discord.TextChannel = None):
        guild_id = ctx.guild.id
        if ch is None:
            await SETTINGS_COLL.update_one({"_id": guild_id}, {"$set": {"notify_channel_id": None}}, upsert=True)
            return await ctx.reply("宝くじ結果通知チャンネルを解除しました。")
        await SETTINGS_COLL.update_one({"_id": guild_id}, {"$set": {"notify_channel_id": ch.id}}, upsert=True)
        await ctx.reply(f"このサーバーの宝くじ結果通知チャンネルを {ch.mention} に設定しました。")


HAND_EMOJI = {
    "rock": "✊",
    "scissors": "✌️",
    "paper": "✋"
}

# 勝敗判定
def judge(p1, p2):
    if p1 == p2:
        return "draw"
    if (
        (p1 == "rock" and p2 == "scissors") or
        (p1 == "scissors" and p2 == "paper") or
        (p1 == "paper" and p2 == "rock")
    ):
        return "p1"
    return "p2"


# --- 参加確認ビュー ---
class JankenInviteView(discord.ui.View):
    def __init__(self, ctx, opponent):
        super().__init__(timeout=30)
        self.ctx = ctx
        self.opponent = opponent
        self.accepted = None
        self.message = None

    async def on_timeout(self):
        if self.accepted is None:
            try:
                await self.message.edit(
                    content=f"{self.opponent.mention}は対戦を拒否しました。",
                    view=None
                )
            except:
                pass

    @discord.ui.button(label="対戦開始", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent.id:
            return

        self.accepted = True
        await interaction.response.edit_message(content="準備中...", view=None)
        self.stop()

    @discord.ui.button(label="対戦拒否", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent.id:
            return

        self.accepted = False
        await interaction.response.edit_message(content=f"{self.opponent.mention}は対戦を拒否しました。", view=None)
        self.stop()

EMPTY = "<:space:1416299781869015081>"
CIRCLE = "<:buttonCircle:1446493192626114654>"
CROSS = "<:buttonCross:1446492795320799283>"

WIN_LINES = [
    (0,1,2), (3,4,5), (6,7,8),  # rows
    (0,3,6), (1,4,7), (2,5,8),  # cols
    (0,4,8), (2,4,6)            # diags
]

# ボタン（1セル）
class TTButton(discord.ui.Button):
    def __init__(self, index: int, view_ref):
        # 初期は空（灰色 secondary）
        super().__init__(style=discord.ButtonStyle.secondary, label="", custom_id=f"tt_{index}")
        self.index = index
        self.view_ref = view_ref

    async def callback(self, interaction: discord.Interaction):
        await self.view_ref.on_click(interaction, self.index)

# 招待ビュー（相手に承諾を求める）
class TTTInviteView(discord.ui.View):
    def __init__(self, challenger: discord.Member, opponent: discord.Member):
        super().__init__(timeout=30)
        self.challenger = challenger
        self.opponent = opponent
        self.accepted: Optional[bool] = None
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # 参加ボタンは相手のみ押せる
        return interaction.user.id == self.opponent.id

    async def on_timeout(self):
        if self.accepted is None and self.message:
            try:
                await self.message.edit(content=f"{self.opponent.mention}は対戦を拒否しました。", view=None)
            except:
                pass

    @discord.ui.button(label="対戦開始", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.accepted = True
        await interaction.response.edit_message(content="準備中...", view=None)
        self.stop()

    @discord.ui.button(label="対戦拒否", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.accepted = False
        await interaction.response.edit_message(content=f"{self.opponent.mention}は対戦を拒否しました。", view=None)
        self.stop()

# メインゲームビュー（ボタンいっぱい）
class TicTacToeView(discord.ui.View):
    def __init__(self, ctx: commands.Context, p1: discord.Member, p2: discord.Member, mode: str):
        # timeout を None にしておく（必要なら秒数入れる）
        super().__init__(timeout=None)
        self.ctx = ctx
        self.p1 = p1
        self.p2 = p2
        self.mode = mode 
        self.highlight_idx = {
            self.p1.id: None,
            self.p2.id: None
        }
        self.pending_highlight = {
            self.p1.id: False,
            self.p2.id: False
        }
        self.start = 0
        self.highlighted_player = 0

        self.board: List[Optional[str]] = [None] * 9

        self.symbol = {
            p1.id: CROSS,   # 先手 ×
            p2.id: CIRCLE   # 後手 ○
        }
        self.turn_player = p1  # 先手は challenger
        self.message: Optional[discord.Message] = None

        self.placements = {
            p1.id: deque(),
            p2.id: deque()
        }

        for idx in range(9):
            btn = TTButton(idx, self)
            row = idx // 3
            btn.row = row
            self.add_item(btn)

        self._refresh_buttons_styles()

    def get_button_by_index(self, idx: int) -> Optional[TTButton]:
        for c in self.children:
            if isinstance(c, TTButton) and getattr(c, "index", None) == idx:
                return c
        return None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in (self.p1.id, self.p2.id):
            return False
        if interaction.user.id != self.turn_player.id:
            return False
        return True

    def _refresh_buttons_styles(self):
        # リセット＆表示反映のみ（状態変更しない）
        for i, v in enumerate(self.board):
            btn = self.get_button_by_index(i)
            if not btn:
                continue

            if v is None:
                btn.emoji = EMPTY
                btn.style = discord.ButtonStyle.secondary
                btn.disabled = False
            else:
                btn.emoji = v
                btn.style = discord.ButtonStyle.secondary
                btn.disabled = True

        # four：現在の highlight_idx を参照して（複数プレイヤー分でも）描画
        if self.mode == "four":
            for pid, idx in self.highlight_idx.items():
                if idx is not None:
                    btn = self.get_button_by_index(idx)
                    if btn:
                        btn.style = discord.ButtonStyle.primary
                        btn.disabled = True
        
    def _check_winner(self) -> Optional[Tuple[discord.Member, List[int]]]:
        """勝者がいれば (winner_member, winning_line) を返す"""
        for a,b,c in WIN_LINES:
            v1, v2, v3 = self.board[a], self.board[b], self.board[c]
            if v1 and v1 == v2 == v3:
                symbol = v1
                player = self.p1 if self.symbol[self.p1.id] == symbol else self.p2
                return player, [a,b,c]
        return None

    def _is_draw(self) -> bool:
        if self.mode == "four":
            return False  # four モードは絶対に引き分けにならない
        return all(x is not None for x in self.board)

    async def end_game(self, content: str):
        for c in self.children:
            if isinstance(c, TTButton):
                c.disabled = True
        try:
            await self.message.edit(content=content, view=self)
        except:
            pass


    async def on_click(self, interaction: discord.Interaction, index: int):
        player = interaction.user
        pid = player.id
        sym = self.symbol[pid]
        dq = self.placements[pid]

        # ---- 0) もし前ターンで「削除発生」してたら、今ターンはハイライトを付ける ----
        # pending_highlight は「前の置きで削除が走った」フラグ
        if self.mode == "four" and self.pending_highlight.get(pid):
            # dq が既に最終形（前ターンで削除済）なら dq[0] が次に消える駒
            if len(dq) >= 1:
                self.highlight_idx[pid] = dq[0]
            else:
                self.highlight_idx[pid] = None
            # フラグは消す（ハイライトは一度だけ表示させる）
            self.pending_highlight[pid] = False
        else:
            # 通常は前のハイライトを消しておく（表示をリセット）
            self.highlight_idx[pid] = None

        # ---- 1) 置く ----
        self.board[index] = sym
        dq.append(index)

        # ---- 2) もし上限を超えたら、古い駒を消す ----
        #    ここで削除が発生したら「次のターンでハイライトを表示」するため pending_highlight を True にする
        if self.mode == "four" and len(dq) > 3:
            removed = dq.popleft()
            self.board[removed] = None
            # mark that next time this player places, we should show the highlight
            self.pending_highlight[pid] = True
            # 直後はハイライトを表示しない（ワンテンポ遅らせる）
            self.highlight_idx[pid] = None

        # ---- 3) 勝敗チェック ----
        win = self._check_winner()
        if win:
            winner, line = win
            for i in line:
                btn = self.get_button_by_index(i)
                if btn:
                    # 勝利マスは emoji も明示的に戻す（消えていると表示されない）
                    btn.emoji = self.board[i]
                    btn.style = discord.ButtonStyle.success
                    btn.disabled = True

            content = f"**勝者:{winner.display_name}**\n{self.p1.mention} vs {self.p2.mention}"
            await interaction.response.edit_message(content=content, view=self)
            return await self.end_game(content)

        # ---- 4) 引き分けチェック（fourモードでは false） ----
        if self._is_draw():
            self._refresh_buttons_styles()
            content = "引き分け！"
            await interaction.response.edit_message(content=content, view=self)
            return await self.end_game(content)

        # ---- 5) ターン交代 ----
        self.turn_player = self.p1 if self.turn_player.id == self.p2.id else self.p2

        # ---- 6) UI 更新（この1箇所のみ）----
        self._refresh_buttons_styles()
        status = (
            f"{self.p1.display_name} vs {self.p2.display_name} — "
            f"次の手: {self.turn_player.mention} ({self.symbol[self.turn_player.id]})"
        )
        await interaction.response.edit_message(content=status, view=self)


# --- 手選択ビュー ---
class JankenSelectView(discord.ui.View):
    def __init__(self, p1, p2, amount, ctx):
        super().__init__(timeout=10)
        self.p1 = p1
        self.p2 = p2
        self.amount = amount  # ← 賭け金
        self.ctx = ctx
        self.choice = {}
        self.message = None

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True

        msg = "**時間切れ！**\n"

        # 両者未選択
        if self.p1.id not in self.choice and self.p2.id not in self.choice:
            msg += "両者は時間以内に出せませんでした！\n引き分け扱いで返金します。"
            await self.message.edit(content=msg, view=None)
            return

        # p1未選択 → p2勝ち
        if self.p1.id not in self.choice:
            winner = self.p2
            loser = self.p1
            msg += f"{self.p1.mention}は時間以内に出せませんでした。 **勝者：{self.p2.mention}**"
        else:
            winner = self.p1
            loser = self.p2
            msg += f"{self.p2.mention}は時間時間以内に出せませんでした。 **勝者：{self.p1.mention}**"

        await self.apply_bet_result(winner, loser, self.amount)
        await self.message.edit(content=msg, view=None)

    async def apply_bet_result(self, winner, loser, amount):
        # 経済システムの wallet 更新
        await update_balance(self.ctx.guild.id, winner.id, wallet_delta=amount)
        await update_balance(self.ctx.guild.id, loser.id, wallet_delta=-amount)

    async def finish(self):
        self.stop()

        for c in self.children:
            c.disabled = True

        p1_hand = self.choice[self.p1.id]
        p2_hand = self.choice[self.p2.id]
        result = judge(p1_hand, p2_hand)

        if result == "draw":
            text = (
                f"{self.p1.mention} **{HAND_EMOJI[p1_hand]}** vs "
                f"{self.p2.mention} **{HAND_EMOJI[p2_hand]}**\n\n"
                "**引き分け！**\n賭け金は返金されました。"
            )
            await self.message.edit(content=text, view=None)
            return

        elif result == "p1":
            winner = self.p1
            loser = self.p2
        else:
            winner = self.p2
            loser = self.p1

        text = (
            f"{self.p1.mention} **{HAND_EMOJI[p1_hand]}** vs "
            f"{self.p2.mention} **{HAND_EMOJI[p2_hand]}**\n\n"
            f"**勝者：{winner.mention}**"
        )

        # 賭け金移動
        await self.apply_bet_result(winner, loser, self.amount)

        text += f"\n\n{winner.mention}は{format_coin(self.amount)}を獲得しました！"

        await self.message.edit(content=text, view=None)

    async def set_choice(self, interaction, hand):
        user = interaction.user

        # 対戦者以外無効
        if user.id not in (self.p1.id, self.p2.id):
            return

        # 既に選んでいる
        if user.id in self.choice:
            return await interaction.response.send_message("すでに選択済みです。", ephemeral=True)

        self.choice[user.id] = hand

        await interaction.response.send_message(f"{HAND_EMOJI[hand]} を選びました！", ephemeral=True)

        # 両者選択済みなら即終了
        if len(self.choice) == 2:
            await self.finish()

    @discord.ui.button(emoji="✊", style=discord.ButtonStyle.primary)
    async def rock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.set_choice(interaction, "rock")

    @discord.ui.button(emoji="✌️", style=discord.ButtonStyle.primary)
    async def scissors(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.set_choice(interaction, "scissors")

    @discord.ui.button(emoji="✋", style=discord.ButtonStyle.primary)
    async def paper(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.set_choice(interaction, "paper")

class ScratchView(discord.ui.View):
    def __init__(self, user, ctx):
        super().__init__(timeout=None)
        self.user = user
        self.ctx = ctx
        self.message = None
        self.revealed = False

        # ★ 2% の確率で当たりを設定する
        if random.random() < 0.02:
            self.win_index = random.randint(0, 8)  # 当たりあり
        else:
            self.win_index = None  # ★ 当たりなし（全ハズレ）

        for row in range(3):
            for col in range(5):
                pos = row * 5 + col

                if col == 0 or col == 4:
                    btn = discord.ui.Button(
                        emoji="<:space:1416299781869015081>",
                        style=discord.ButtonStyle.primary,
                        disabled=True,
                        custom_id=f"blue-{pos}"
                    )
                    self.add_item(btn)
                    continue

                scratch_index = (row * 3) + (col - 1)

                btn = discord.ui.Button(
                    emoji="<:space:1416299781869015081>",
                    style=discord.ButtonStyle.secondary,
                    disabled=False,
                    custom_id=f"scratch-{scratch_index}"
                )
                btn.callback = self.make_callback(scratch_index)
                self.add_item(btn)

    def make_callback(self, index):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user.id:
                return

            if self.revealed:
                return

            # =======================
            # 当たり（2%で存在する）
            # =======================
            if self.win_index is not None and index == self.win_index:
                self.revealed = True

                btn = [c for c in self.children if c.custom_id == f"scratch-{index}"][0]
                btn.emoji = "💰"
                btn.style = discord.ButtonStyle.success
                btn.disabled = True

                # 青の枠を全部緑に
                for c in self.children:
                    if "blue" in c.custom_id:
                        c.style = discord.ButtonStyle.success

                # 全ボタン無効化
                for c in self.children:
                    c.disabled = True

                # 報酬付与
                reward = random.randint(6000, 10000)
                await update_balance(self.ctx.guild.id, self.user.id, wallet_delta=reward)

                return await interaction.response.edit_message(
                    content=f"{self.user.mention}は{format_coin(reward)}を獲得しました！",
                    view=self
                )

            # =======================
            # ハズレ（当たり無し or 違う場所）
            # =======================
            btn = [c for c in self.children if c.custom_id == f"scratch-{index}"][0]
            btn.emoji = "❌"
            btn.disabled = True

            await interaction.response.edit_message(
                content="削ってみよう！",
                view=self
            )

        return callback

# 各ボタンの callback を割り当て
def apply_callbacks(view: ScratchView):
    for item in view.children:
        async def callback(interaction, btn=item):
            await view.button_callback(interaction, btn)
        item.callback = callback

class JankenCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="rps", description="じゃんけんで対戦します。")
    @app_commands.rename(opponent="対戦相手", amount="賭け金")
    async def janken(self, ctx, opponent: discord.Member, amount: int):
        if amount <= 0:
            return await ctx.reply("賭け金は 1 以上にしてください。")

        if opponent.id == ctx.author.id:
            return await ctx.reply("自分と対戦はできません。")

        # --- 所持金チェック ---
        p1_data = await get_user(ctx.guild.id, ctx.author.id)
        p2_data = await get_user(ctx.guild.id, opponent.id)

        if p1_data["wallet"] < amount:
            return await ctx.reply("十分な賭け金を持っていません。")
        if p2_data["wallet"] < amount:
            return await ctx.reply("対戦相手は十分な賭け金を持っていません。")

        # --- 招待ビュー ---
        invite_view = JankenInviteView(ctx, opponent)
        msg = await ctx.reply(
            f"{ctx.author.mention}が{opponent.mention}とじゃんけんをしようとしています。\n対戦しますか？\n賭け金：{format_coin(amount)}",
            view=invite_view
        )
        invite_view.message = msg

        await invite_view.wait()

        if invite_view.accepted is not True:
            return

        await msg.edit(content="準備中…", view=None)
        await asyncio.sleep(2)

        # --- 選択ビュー（賭け金含む） ---
        select_view = JankenSelectView(ctx.author, opponent, amount, ctx)
        await msg.edit(content="じゃんけん…", view=select_view)
        select_view.message = msg

class ScratchCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="scratchcard", description="スクラッチカードを削って高額賞金をゲットしよう！")
    async def scratch(self, ctx: commands.Context):

        COST = 5000

        user_data = await get_user(ctx.guild.id, ctx.author.id)
        if user_data["wallet"] < COST:
            return await ctx.reply("所持金が不足しています。")

        # 支払い
        await update_balance(ctx.guild.id, ctx.author.id, wallet_delta=-COST)
        await inc_stat(ctx.guild.id, ctx.author.id, "scratch")

        view = ScratchView(ctx.author, ctx)

        msg = await ctx.reply(
            f"削ってみよう！",
            view=view
        )
        view.message = msg

class TicTacToeCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="tictac", description="三目並べをします。")
    @app_commands.rename(opponent="対戦相手", mode="モード")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="通常", value="normal"),
            app_commands.Choice(name="4コマモード", value="four"),
        ]
    )
    async def tictac(self, ctx: commands.Context, opponent: discord.Member, mode: str = "normal"):
        mode = mode.lower()
        if opponent.id == ctx.author.id:
            return await ctx.reply("自分とは対戦できません。", ephemeral=True)
        if mode == "normal":
            invite_mode = ""
        else:
            invite_mode = "(4コマのみ)"
        invite_view = TTTInviteView(ctx.author, opponent)
        msg = await ctx.reply(f"{ctx.author.mention}が{opponent.mention}と三目並べ{invite_mode}をしようとしています。\n対戦しますか？", view=invite_view)
        invite_view.message = msg

        await invite_view.wait()
        if invite_view.accepted is not True:
            # 拒否 or timeout (invite_view handles messaging)
            return

        # create game view
        view = TicTacToeView(ctx, ctx.author, opponent, mode)
        status = f"{ctx.author.display_name} vs {opponent.display_name} — 次の手:{view.turn_player.mention}({view.symbol[view.turn_player.id]})"

        game_msg = await msg.edit(content=status, view=view)
        view.message = game_msg

async def setup(bot):
    await bot.add_cog(LotteryCog(bot))
    await bot.add_cog(JankenCog(bot))
    await bot.add_cog(ScratchCog(bot))
    await bot.add_cog(TicTacToeCog(bot))
