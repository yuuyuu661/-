import os
import logging
import json
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime
import asyncio

import discord
from discord.ext import commands, tasks
from discord import app_commands

# ===================== 環境変数 =====================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")  # 必須
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
GUILD_IDS = [int(x.strip()) for x in os.getenv("GUILD_IDS", "").split(",") if x.strip().isdigit()]
DATA_DIR = os.getenv("DATA_DIR", ".")  # Railway Shared Disk を /data にマウント推奨
# 共通バナー画像（Embed最下部に表示）。環境変数優先・未設定なら固定URLを使用
BANNER_IMAGE_URL = os.getenv("BANNER_IMAGE_URL", "https://example.com/your-fixed-banner.png")

# ===== コマンド使用をこのロール所持者に限定 =====
ROLE_LIMIT_ID = 1398724601256874014

# ===================== ログ設定 =====================
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="(%(asctime)s) [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("jumpbot")

# ===================== 簡易データ永続化 =====================
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "data.json")

def load_data() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.exception("Failed to load data.json: %s", e)
        return {}

def save_data(data: Dict[str, Any]) -> None:
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.exception("Failed to save data.json: %s", e)

DB = load_data()
DB.setdefault("jump_sets", [])  # 自動更新対象レコードの配列

# ===================== Intents / Bot =====================
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True  # VC人数取得に必要
intents.message_content = False  # テキスト内容は不要

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ===================== 権限チェック（指定ロール必須） =====================
def role_required(role_id: int):
    async def predicate(interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        return any(r.id == role_id for r in interaction.user.roles)
    return app_commands.check(predicate)

# ===================== ユーティリティ =====================
def channel_jump_url(guild_id: int, channel_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}"

def split_rows(buttons: List[discord.ui.Button], per_row: int = 5) -> List[List[discord.ui.Button]]:
    return [buttons[i:i+per_row] for i in range(0, len(buttons), per_row)]

def resolve_channel(guild: discord.Guild, cid: int) -> Optional[discord.abc.GuildChannel]:
    return guild.get_channel(cid)

def is_under_category(ch: discord.abc.GuildChannel, category_id: int) -> bool:
    try:
        return getattr(ch, "category_id", None) == category_id
    except:
        return False

def vc_member_count(ch: discord.abc.GuildChannel) -> int:
    if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
        return len(ch.members)
    return 0

def label_for_channel(ch: discord.abc.GuildChannel) -> str:
    if isinstance(ch, discord.TextChannel):
        return f"#{ch.name}"
    if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
        n = vc_member_count(ch)
        return f"🔊 {ch.name} ({n})"
    return ch.name

def build_buttons_for(guild: discord.Guild, channel_ids: List[int]) -> Tuple[str, List[List[discord.ui.Button]], List[int], List[int]]:
    buttons: List[discord.ui.Button] = []
    ok_ids: List[int] = []
    ng_ids: List[int] = []

    for cid in channel_ids:
        ch = resolve_channel(guild, cid)
        if not ch:
            ng_ids.append(cid)
            continue
        url = channel_jump_url(guild.id, cid)
        label = label_for_channel(ch)
        buttons.append(discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=url))
        ok_ids.append(cid)

    rows = split_rows(buttons, per_row=5)[:5]  # 1メッセージ最大25ボタン
    return ("", rows, ok_ids, ng_ids)

async def edit_jump_message(guild: discord.Guild, channel_id: int, message_id: int, channel_ids: List[int]) -> bool:
    ch = guild.get_channel(channel_id)
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        return False
    try:
        msg = await ch.fetch_message(message_id)
    except:
        return False

    _, rows, _, _ = build_buttons_for(guild, channel_ids)
    try:
        await msg.edit(view=make_view_from_rows(rows))  # Embedは据え置き、ボタンだけ更新
        return True
    except Exception as e:
        log.warning("Failed to edit message %s: %s", message_id, e)
        return False

def make_view_from_rows(rows: List[List[discord.ui.Button]]) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for row in rows:
        for b in row:
            v.add_item(b)
    return v

def add_jump_set_record(guild_id: int, message_channel_id: int, message_id: int, channel_ids: List[int], category_id: int, description: str):
    DB["jump_sets"].append({
        "guild_id": guild_id,
        "message_channel_id": message_channel_id,
        "message_id": message_id,
        "channel_ids": channel_ids,
        "category_id": category_id,
        "description": description,
        "created_at": datetime.utcnow().isoformat()
    })
    save_data(DB)

def remove_jump_set_record(message_id: int) -> bool:
    before = len(DB["jump_sets"])
    DB["jump_sets"] = [x for x in DB["jump_sets"] if x.get("message_id") != message_id]
    after = len(DB["jump_sets"])
    if before != after:
        save_data(DB)
        return True
    return False

# ===================== on_ready & 同期 =====================
@bot.event
async def on_ready():
    owner = (bot.application and bot.application.owner)
    log.info("Bot connected as %s (owner: %s)", bot.user, getattr(owner, "name", "unknown"))

    if GUILD_IDS:
        for gid in GUILD_IDS:
            try:
                synced = await tree.sync(guild=discord.Object(id=gid))
                log.info("[Guild %s] Synced %d commands", gid, len(synced))
            except Exception as e:
                log.exception("Sync failed for guild %s: %s", gid, e)
    else:
        try:
            synced = await tree.sync()
            log.info("[Global] Synced %d commands", len(synced))
        except Exception as e:
            log.exception("Global sync failed: %s", e)

    if not refresh_jump_messages.is_running():
        refresh_jump_messages.start()

# ===================== エラーハンドラ =====================
@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    from discord.app_commands import CheckFailure
    if isinstance(error, CheckFailure):
        msg = "このコマンドを実行できる権限がありません。"
    else:
        msg = "エラーが発生しました。入力や権限を確認してください。"
        log.exception("Slash command error: %s", error)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except:
        pass

# ===================== コマンド（Embed＋ボタン／ロール制限） =====================
@tree.command(name="make_buttons", description="カテゴリのパネル（金色）＋チャンネルへ飛ぶボタンを生成（VCは人数表示）")
@app_commands.describe(
    category_id="カテゴリID（数値）",
    description="上に表示する説明文（自由入力）",
    channel_ids="カテゴリ内のチャンネルIDをカンマ区切り（例: 111,222,333）"
)
@role_required(ROLE_LIMIT_ID)
async def make_buttons(interaction: discord.Interaction, category_id: str, description: str, channel_ids: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    if not guild:
        return await interaction.followup.send("サーバ内で実行してください。", ephemeral=True)

    # 入力チェック
    try:
        cat_id = int(category_id.strip())
    except:
        return await interaction.followup.send("category_id は数値で指定してください。", ephemeral=True)

    raw_ids = [x.strip() for x in channel_ids.split(",") if x.strip()]
    try:
        ids = [int(x) for x in raw_ids]
    except:
        return await interaction.followup.send("channel_ids に数値以外が含まれています。", ephemeral=True)

    category = guild.get_channel(cat_id)
    if not isinstance(category, discord.CategoryChannel):
        return await interaction.followup.send("指定の category_id はカテゴリではありません。", ephemeral=True)

    # カテゴリ配下のみ
    filtered, skipped = [], []
    for cid in ids:
        ch = resolve_channel(guild, cid)
        if ch and is_under_category(ch, cat_id):
            filtered.append(cid)
        else:
            skipped.append(cid)
    if not filtered:
        return await interaction.followup.send("指定カテゴリ内の有効なチャンネルIDがありません。", ephemeral=True)

    # ボタン生成
    _, rows, ok_ids, ng_ids = build_buttons_for(guild, filtered)
    view = make_view_from_rows(rows)

    # ===== 見た目（Embed パネル：金色＋最下部に共通バナー） =====
    embed = discord.Embed(
        title=f"カテゴリ：{category.name}",
        description=description or "\u200b",
        color=discord.Color.gold()  # 左端のカラーバーを金色に
    )
    # 一番下に共通の飾り画像
    if BANNER_IMAGE_URL:
        embed.set_image(url=BANNER_IMAGE_URL)

    # 送信（公開）
    try:
        msg = await interaction.channel.send(embed=embed, view=view)
    except Exception as e:
        log.exception("Failed to send jump buttons: %s", e)
        return await interaction.followup.send("メッセージ送信に失敗しました。Botの送信権限を確認してください。", ephemeral=True)

    # 自動更新に登録
    add_jump_set_record(guild.id, interaction.channel.id, msg.id, ok_ids, cat_id, description)

    skipped_all = sorted(set(skipped + ng_ids))
    note = f"\n⚠️ カテゴリ外/無効のIDをスキップ：{', '.join(map(str, skipped_all))}" if skipped_all else ""
    await interaction.followup.send(f"✅ 生成しました。（message_id: {msg.id}）{note}", ephemeral=True)

@tree.command(name="buttons_refresh", description="ジャンプボタンの人数表示を手動更新")
@role_required(ROLE_LIMIT_ID)
async def buttons_refresh(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    if not guild:
        return await interaction.followup.send("サーバ内で実行してください。", ephemeral=True)

    count = 0
    for rec in DB.get("jump_sets", []):
        if rec.get("guild_id") != guild.id:
            continue
        ok = await edit_jump_message(
            guild,
            rec["message_channel_id"],
            rec["message_id"],
            rec["channel_ids"]
        )
        if ok:
            count += 1
    await interaction.followup.send(f"更新しました：{count} 件", ephemeral=True)

@tree.command(name="buttons_remove", description="自動更新の対象から外します")
@app_commands.describe(message_id="対象メッセージID（数値）")
@role_required(ROLE_LIMIT_ID)
async def buttons_remove(interaction: discord.Interaction, message_id: str):
    try:
        mid = int(message_id.strip())
    except:
        return await interaction.response.send_message("message_id は数値で指定してください。", ephemeral=True)
    ok = remove_jump_set_record(mid)
    if ok:
        await interaction.response.send_message(f"対象から外しました（message_id: {mid}）。必要ならメッセージを手動で削除してください。", ephemeral=True)
    else:
        await interaction.response.send_message("対象が見つかりませんでした。", ephemeral=True)

# ===================== 自動更新ループ（15秒ごと） =====================
@tasks.loop(seconds=15.0)
async def refresh_jump_messages():
    if not bot.is_ready():
        return
    for rec in list(DB.get("jump_sets", [])):
        guild = bot.get_guild(rec.get("guild_id"))
        if not guild:
            continue
        await edit_jump_message(
            guild,
            rec["message_channel_id"],
            rec["message_id"],
            rec["channel_ids"]
        )

@refresh_jump_messages.before_loop
async def before_refresh_loop():
    await bot.wait_until_ready()

# ===================== FastAPI（任意：Railway Health Check） =====================
try:
    import threading
    from fastapi import FastAPI
    import uvicorn

    api = FastAPI()

    @api.get("/")
    def root():
        return {"status": "ok"}

    def run_api():
        port = int(os.getenv("PORT", "8080"))
        uvicorn.run(api, host="0.0.0.0", port=port, log_level="warning")

    threading.Thread(target=run_api, daemon=True).start()
except Exception as e:
    log.warning("FastAPI init skipped: %s", e)

# ===================== 起動 =====================
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        log.error("環境変数 DISCORD_TOKEN が未設定です。")
        raise SystemExit(1)
    bot.run(DISCORD_TOKEN)
