import os
import logging
import json
from typing import Optional, Dict, Any, List, Tuple
import asyncio
from datetime import datetime

import discord
from discord.ext import commands, tasks
from discord import app_commands

# ===================== 環境変数 =====================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")  # 必須
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
GUILD_IDS = [int(x.strip()) for x in os.getenv("GUILD_IDS", "").split(",") if x.strip().isdigit()]
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0"))  # 管理ロール（任意）
DATA_DIR = os.getenv("DATA_DIR", ".")  # Railway Shared Disk を /data でマウント推奨

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
DB.setdefault("jump_sets", [])  # 追従更新対象のメッセージ群

# ===================== Intents / Bot =====================
intents = discord.Intents.default()
intents.message_content = False  # 必要なら True
intents.members = True
intents.guilds = True
intents.voice_states = True  # VC人数の更新に必須

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ===================== 権限ヘルパ =====================
def is_admin_or_owner(interaction: discord.Interaction) -> bool:
    if interaction.client.application and interaction.user.id == interaction.client.application.owner.id:
        return True
    if ADMIN_ROLE_ID and isinstance(interaction.user, discord.Member):
        if ADMIN_ROLE_ID in [r.id for r in interaction.user.roles]:
            return True
    return False

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
    if isinstance(ch, discord.VoiceChannel):
        return len(ch.members)
    if isinstance(ch, discord.StageChannel):
        # ステージは audience + speakers などあるが単純合計
        return len(ch.members)
    return 0

def label_for_channel(ch: discord.abc.GuildChannel) -> str:
    if isinstance(ch, discord.TextChannel):
        return f"#{ch.name}"
    if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
        n = vc_member_count(ch)
        return f"🔊 {ch.name} ({n})"
    # 他（Forumなど）は名前のみ
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

    # Discordは1メッセージ最大5行×各行5ボタン＝25ボタンまで
    rows = split_rows(buttons, per_row=5)[:5]
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
        await msg.edit(view=make_view_from_rows(rows))
        return True
    except Exception as e:
        log.warning("Failed to edit message %s in #%s: %s", message_id, getattr(ch, "name", ch.id), e)
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
    app = bot.application
    if not app:
        await bot.wait_until_ready()
    owner = (bot.application and bot.application.owner)
    log.info("Bot connected as %s (owner: %s)", bot.user, getattr(owner, "name", "unknown"))

    # ギルド即時同期
    if GUILD_IDS:
        for gid in GUILD_IDS:
            try:
                g = discord.Object(id=gid)
                synced = await tree.sync(guild=g)
                log.info("[Guild %s] Synced %d commands", gid, len(synced))
            except Exception as e:
                log.exception("Sync failed for guild %s: %s", gid, e)
    else:
        try:
            synced = await tree.sync()
            log.info("[Global] Synced %d commands", len(synced))
        except Exception as e:
            log.exception("Global sync failed: %s", e)

    # 更新ループ起動
    if not refresh_jump_messages.is_running():
        refresh_jump_messages.start()

# ===================== エラーハンドラ =====================
@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.exception("Slash command error: %s", error)
    msg = "エラーが発生しました。権限や入力内容を確認してください。"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except:
        pass

# ===================== コマンド群 =====================
@tree.command(name="make_buttons", description="カテゴリ内のチャンネルへ飛ぶボタンを生成（VCは人数を表示）")
@app_commands.describe(
    category_id="カテゴリID（数値）",
    description="説明文（自由入力）",
    channel_ids="チャンネルIDをカンマ区切りで（例: 111,222,333）"
)
async def make_buttons(interaction: discord.Interaction, category_id: str, description: str, channel_ids: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    if not guild:
        return await interaction.followup.send("サーバ内で実行してください。", ephemeral=True)

    # 入力パース
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

    # そのカテゴリ配下のみ許可
    filtered: List[int] = []
    skipped: List[int] = []
    for cid in ids:
        ch = resolve_channel(guild, cid)
        if ch and is_under_category(ch, cat_id):
            filtered.append(cid)
        else:
            skipped.append(cid)

    if not filtered:
        return await interaction.followup.send("指定カテゴリ内の有効なチャンネルIDがありません。", ephemeral=True)

    # ボタン構築
    _, rows, ok_ids, ng_ids = build_buttons_for(guild, filtered)
    view = make_view_from_rows(rows)

    # メッセージ送信（公開）
    try:
        msg = await interaction.channel.send(content=description, view=view)
    except Exception as e:
        log.exception("Failed to send jump buttons: %s", e)
        return await interaction.followup.send("メッセージ送信に失敗しました。Botの送信権限を確認してください。", ephemeral=True)

    # 追従更新登録
    add_jump_set_record(guild.id, interaction.channel.id, msg.id, ok_ids, cat_id, description)

    # 結果通知
    note = ""
    if skipped or ng_ids:
        all_skipped = sorted(set(skipped + ng_ids))
        note = f"\n⚠️ カテゴリ外/無効のIDをスキップ：{', '.join(map(str, all_skipped))}"
    await interaction.followup.send(f"✅ 生成しました。追従更新に登録済みです。（message_id: {msg.id}）{note}", ephemeral=True)

@tree.command(name="buttons_refresh", description="ジャンプボタン表示（VC人数）を手動更新")
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

@tree.command(name="buttons_remove", description="追従更新の対象から外します（必要ならメッセージは手動で削除）")
@app_commands.describe(message_id="対象メッセージID（数値）")
async def buttons_remove(interaction: discord.Interaction, message_id: str):
    if not is_admin_or_owner(interaction):
        return await interaction.response.send_message("権限がありません。", ephemeral=True)
    try:
        mid = int(message_id.strip())
    except:
        return await interaction.response.send_message("message_id は数値で指定してください。", ephemeral=True)

    ok = remove_jump_set_record(mid)
    if ok:
        await interaction.response.send_message(f"対象から外しました（message_id: {mid}）。必要ならメッセージを手動で削除してください。", ephemeral=True)
    else:
        await interaction.response.send_message("対象が見つかりませんでした。", ephemeral=True)

# ===================== 自動更新ループ（約15秒ごと） =====================
@tasks.loop(seconds=15.0)
async def refresh_jump_messages():
    if not bot.is_ready():
        return
    # ギルドごとに処理
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
    # ここで save_data は不要（編集のみ）。レコードが壊れていたら手動remove促す。

@refresh_jump_messages.before_loop
async def before_refresh_loop():
    await bot.wait_until_ready()

# ===================== 参考：招待URL =====================
def build_invite_url(client_id: int, permissions: int = 2147568640) -> str:
    scopes = "bot+applications.commands"
    return (
        f"https://discord.com/oauth2/authorize"
        f"?client_id={client_id}&permissions={permissions}&integration_type=0&scope={scopes}"
    )

@tree.command(name="invite_url", description="このBotの招待URLを表示（管理者）")
async def invite_url_cmd(interaction: discord.Interaction):
    if not is_admin_or_owner(interaction):
        return await interaction.response.send_message("権限がありません。", ephemeral=True)
    client_id = interaction.client.application.id
    url = build_invite_url(client_id)
    await interaction.response.send_message(f"招待URL：{url}", ephemeral=True)

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
