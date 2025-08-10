
import os
import logging
import sqlite3
from typing import Dict, Set, Optional, List

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# ----------------- CONFIG -----------------
# TOKEN: 推荐用环境变量 BOT_TOKEN（不会写到仓库里）
BOT_TOKEN = os.environ.get("BOT_TOKEN") or "PUT_YOUR_BOT_TOKEN_HERE"
# 管理员用户名（不带 @），可以放多个
ADMIN_USERNAMES: List[str] = ["ap114514666"]

# sqlite db path for persistent ban list
DB_PATH = "bot_state.db"

# ----------------- LOGGING -----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ----------------- IN-MEMORY STATE -----------------
pending_requests: Set[int] = set()       # user ids waiting approval
active_sessions: Set[int] = set()        # user ids connected
admin_msgid_to_user: Dict[int, int] = {} # admin_msg_id -> user_id mapping
user_last_admin_msgid: Dict[int, int] = {}  # user -> last admin message id

# resolved numeric admin ids (may be empty until resolved or registered)
numeric_admin_ids: Set[int] = set()

# ----------------- SQLITE HELPERS (ban list) -----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS banned (user_id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

def ban_user_db(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO banned(user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def unban_user_db(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM banned WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def is_banned_db(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM banned WHERE user_id=? LIMIT 1", (user_id,))
    r = c.fetchone()
    conn.close()
    return r is not None

# ----------------- KEYBOARDS -----------------
def user_main_keyboard(is_pending: bool, is_active: bool) -> InlineKeyboardMarkup:
    if is_active:
        kb = [[InlineKeyboardButton("🔚 结束聊天", callback_data="user_end")]]
    elif is_pending:
        kb = [[InlineKeyboardButton("⏳ 取消申请", callback_data="user_cancel")]]
    else:
        kb = [[InlineKeyboardButton("📨 申请与管理员连接", callback_data="user_apply")]]
    return InlineKeyboardMarkup(kb)

def admin_panel_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton("📥 查看申请", callback_data="admin_view_pending"),
            InlineKeyboardButton("📋 活动会话", callback_data="admin_view_active"),
        ],
        [
            InlineKeyboardButton("📤 主动连接（/connect）", callback_data="admin_hint_connect"),
            InlineKeyboardButton("🔧 管理帮助", callback_data="admin_help"),
        ],
    ]
    return InlineKeyboardMarkup(kb)

def pending_item_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 同意", callback_data=f"admin_accept:{user_id}"),
            InlineKeyboardButton("❌ 拒绝", callback_data=f"admin_reject:{user_id}"),
        ]
    ])

def active_item_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔚 结束该会话", callback_data=f"admin_end:{user_id}"),
            InlineKeyboardButton("🚫 封禁该用户", callback_data=f"admin_ban:{user_id}"),
        ]
    ])

# ----------------- HELPERS -----------------
def username_is_admin(username: Optional[str]) -> bool:
    if not username:
        return False
    return username.lower() in {u.lower() for u in ADMIN_USERNAMES}

def is_admin_update(update: Update) -> bool:
    u = update.effective_user
    if not u:
        return False
    # check numeric id too
    if u.id in numeric_admin_ids:
        return True
    return username_is_admin(u.username)

async def resolve_admin_usernames_to_ids(app):
    """尝试将 ADMIN_USERNAMES 解析为 numeric ids via get_chat('@username')"""
    resolved = set()
    for name in ADMIN_USERNAMES:
        try:
            chat = await app.bot.get_chat(f"@{name}")
            resolved.add(chat.id)
            logger.info(f"Resolved @{name} -> {chat.id}")
        except Exception:
            logger.warning(f"无法解析 @{name}（管理员可能尚未与 bot 对话）")
    return resolved

async def notify_admins_new_request(user_id: int, username: Optional[str], context: ContextTypes.DEFAULT_TYPE):
    text = f"📌 新请求：用户 {'@'+username if username else user_id}\\nID: `{user_id}`\\n是否同意？"
    for name in ADMIN_USERNAMES:
        try:
            await context.bot.send_message(chat_id=f"@{name}", text=text, reply_markup=pending_item_kb(user_id), parse_mode="Markdown")
        except Exception:
            logger.exception(f"无法发送申请通知给 @{name}")
    # also attempt to notify numeric admins if resolved
    for aid in numeric_admin_ids:
        try:
            await context.bot.send_message(chat_id=aid, text=text, reply_markup=pending_item_kb(user_id), parse_mode="Markdown")
        except Exception:
            pass

# ----------------- COMMANDS -----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_admin_update(update):
        await update.message.reply_text("欢迎管理员。管理面板：", reply_markup=admin_panel_keyboard())
    else:
        is_pending = uid in pending_requests
        is_active = uid in active_sessions
        await update.message.reply_text("欢迎。点击下方按钮申请与管理员连接。", reply_markup=user_main_keyboard(is_pending, is_active))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin_update(update):
        txt = (
            "/start - 管理面板\n"
            "/connect <user_id> - 主动连接用户\n"
            "/end <user_id> - 结束某用户会话\n"
            "/ban <user_id> - 封禁用户\n"
            "/unban <user_id> - 解封用户\n"
            "/list - 列出活动/待处理\n"
            "/send <user_id> <消息> - 给某用户发消息\n"
            "/broadcast <消息> - 向所有活动用户广播\n"
            "/register_admin - 管理员私聊注册（仅在解析失败时使用）\n"
        )
        await update.message.reply_text(txt)
    else:
        await update.message.reply_text("使用 /start 并点击按钮申请与管理员连接。")

async def register_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员在与 bot 私聊时可用此命令注册自己的 numeric id（备用）"""
    if not username_is_admin(update.effective_user.username):
        await update.message.reply_text("仅允许预设用户名的管理员使用此命令（请确保你是管理员用户名）。")
        return
    numeric_admin_ids.add(update.effective_user.id)
    await update.message.reply_text(f"已注册管理员 id: {update.effective_user.id}")
    logger.info(f"管理员 {update.effective_user.username} 注册为 numeric id {update.effective_user.id}")

async def connect_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return
    if not context.args:
        await update.message.reply_text("用法：/connect <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id 必须是数字")
        return
    if is_banned_db(uid):
        await update.message.reply_text("该用户已被封禁，无法连接。")
        return
    pending_requests.discard(uid)
    active_sessions.add(uid)
    await update.message.reply_text(f"✅ 已主动与用户 {uid} 建立会话。")
    try:
        await context.bot.send_message(chat_id=uid, text="✅ 管理员已主动与你建立专属聊天通道。")
    except Exception:
        await update.message.reply_text("警告：向用户发送消息失败，用户可能未与 bot 对话过。")

async def end_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return
    if not context.args:
        await update.message.reply_text("用法：/end <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id 必须是数字")
        return
    if uid in active_sessions:
        active_sessions.discard(uid)
        try:
            await context.bot.send_message(chat_id=uid, text="⚠️ 管理员已结束本次会话。")
        except:
            pass
        await update.message.reply_text(f"已结束与用户 {uid} 的会话。")
    else:
        await update.message.reply_text("该用户当前没有活动会话。")

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return
    if not context.args:
        await update.message.reply_text("用法：/ban <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id 必须是数字")
        return
    ban_user_db(uid)
    pending_requests.discard(uid)
    active_sessions.discard(uid)
    try:
        await context.bot.send_message(chat_id=uid, text="🚫 你已被管理员封禁，无法与管理员聊天。")
    except:
        pass
    await update.message.reply_text(f"已封禁用户 {uid} 并断开任何会话。")

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return
    if not context.args:
        await update.message.reply_text("用法：/unban <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id 必须是数字")
        return
    unban_user_db(uid)
    await update.message.reply_text(f"已解封用户 {uid}。")

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return
    txt = f"🟢 活动会话（{len(active_sessions)}）：\n" + ("\n".join(map(str, active_sessions)) if active_sessions else "无")
    txt += f"\n\n⏳ 待处理申请（{len(pending_requests)}）：\n" + ("\n".join(map(str, pending_requests)) if pending_requests else "无")
    await update.message.reply_text(txt)

async def send_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("用法：/send <user_id> <消息>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id 必须是数字")
        return
    text = " ".join(context.args[1:])
    try:
        await context.bot.send_message(chat_id=uid, text=text)
        await update.message.reply_text("已发送。")
    except Exception as e:
        await update.message.reply_text(f"发送失败：{e}")

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return
    if not context.args:
        await update.message.reply_text("用法：/broadcast <消息>")
        return
    text = " ".join(context.args)
    count = 0
    for uid in list(active_sessions):
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            count += 1
        except:
            pass
    await update.message.reply_text(f"已向 {count} 个活动用户广播。")

# ----------------- CALLBACK HANDLER -----------------
async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    caller = query.from_user
    caller_uid = caller.id
    caller_username = caller.username

    # ---- user actions ----
    if data == "user_apply":
        if is_banned_db(caller_uid):
            await query.edit_message_text("你已被封禁，无法申请。")
            return
        if caller_uid in active_sessions:
            await query.edit_message_text("你已在会话中，点结束以断开。", reply_markup=user_main_keyboard(False, True))
            return
        if caller_uid in pending_requests:
            await query.edit_message_text("你已申请，请耐心等待。", reply_markup=user_main_keyboard(True, False))
            return
        pending_requests.add(caller_uid)
        await query.edit_message_text("✅ 已发送申请，请等待管理员确认。", reply_markup=user_main_keyboard(True, False))
        await notify_admins_new_request(caller_uid, caller_username, context)
        return

    if data == "user_cancel":
        if caller_uid in pending_requests:
            pending_requests.discard(caller_uid)
            await query.edit_message_text("已取消申请。", reply_markup=user_main_keyboard(False, False))
            # notify admins optionally
            for name in ADMIN_USERNAMES:
                try:
                    await context.bot.send_message(chat_id=f"@{name}", text=f"ℹ️ 用户 `{caller_uid}` 取消了申请。", parse_mode="Markdown")
                except:
                    pass
        else:
            await query.edit_message_text("你当前没有申请。", reply_markup=user_main_keyboard(False, False))
        return

    if data == "user_end":
        if caller_uid in active_sessions:
            active_sessions.discard(caller_uid)
            await query.edit_message_text("你已结束会话。", reply_markup=user_main_keyboard(False, False))
            for name in ADMIN_USERNAMES:
                try:
                    await context.bot.send_message(chat_id=f"@{name}", text=f"⚠️ 用户 `{caller_uid}` 已结束会话。", parse_mode="Markdown")
                except:
                    pass
        else:
            await query.edit_message_text("你当前没有会话。", reply_markup=user_main_keyboard(False, False))
        return

    # ---- admin actions ----
    if data == "admin_view_pending":
        if not is_admin_update(update):
            await query.edit_message_text("仅管理员可查看。")
            return
        if not pending_requests:
            await query.edit_message_text("当前没有待处理申请。", reply_markup=admin_panel_keyboard())
            return
        await query.edit_message_text("以下为待处理申请：", reply_markup=admin_panel_keyboard())
        for uid in list(pending_requests):
            txt = f"📌 申请用户 ID: `{uid}`"
            try:
                # send to this admin (by numeric if possible, else by username)
                target_chat = update.effective_user.id if update.effective_user.id in numeric_admin_ids else f"@{update.effective_user.username}"
                await context.bot.send_message(chat_id=target_chat, text=txt, reply_markup=pending_item_kb(uid), parse_mode="Markdown")
            except:
                logger.exception("向管理员发送 pending item 失败")
        return

    if data == "admin_view_active":
        if not is_admin_update(update):
            await query.edit_message_text("仅管理员可查看。")
            return
        if not active_sessions:
            await query.edit_message_text("当前没有活动会话。", reply_markup=admin_panel_keyboard())
            return
        await query.edit_message_text("活动会话列表：", reply_markup=admin_panel_keyboard())
        for uid in list(active_sessions):
            txt = f"🟢 活动用户 ID: `{uid}`"
            try:
                target_chat = update.effective_user.id if update.effective_user.id in numeric_admin_ids else f"@{update.effective_user.username}"
                await context.bot.send_message(chat_id=target_chat, text=txt, reply_markup=active_item_kb(uid), parse_mode="Markdown")
            except:
                logger.exception("向管理员发送 active item 失败")
        return

    if data.startswith("admin_accept:"):
        try:
            uid = int(data.split(":",1)[1])
        except:
            await query.edit_message_text("ID 格式错误")
            return
        if uid in pending_requests:
            pending_requests.discard(uid)
            active_sessions.add(uid)
            await query.edit_message_text(f"✅ 已同意用户 `{uid}` 的申请。", parse_mode="Markdown")
            try:
                await context.bot.send_message(chat_id=uid, text="✅ 管理员已同意你的申请，你现在已连接到管理员。")
            except:
                pass
            try:
                await context.bot.send_message(chat_id=caller_uid, text=f"🟢 已与用户 `{uid}` 建立连接。", parse_mode="Markdown")
            except:
                pass
        else:
            await query.edit_message_text("该用户不在申请队列或已被处理。")
        return

    if data.startswith("admin_reject:"):
        try:
            uid = int(data.split(":",1)[1])
        except:
            await query.edit_message_text("ID 格式错误")
            return
        if uid in pending_requests:
            pending_requests.discard(uid)
            await query.edit_message_text(f"❌ 已拒绝用户 `{uid}` 的申请。", parse_mode="Markdown")
            try:
                await context.bot.send_message(chat_id=uid, text="很抱歉，管理员拒绝了你的聊天申请。")
            except:
                pass
        else:
            await query.edit_message_text("该用户不在申请队列或已被处理。")
        return

    if data.startswith("admin_end:"):
        try:
            uid = int(data.split(":",1)[1])
        except:
            await query.edit_message_text("ID 格式错误")
            return
        if uid in active_sessions:
            active_sessions.discard(uid)
            await query.edit_message_text(f"🔚 已结束用户 `{uid}` 的会话。", parse_mode="Markdown")
            try:
                await context.bot.send_message(chat_id=uid, text="⚠️ 管理员已结束本次会话。")
            except:
                pass
        else:
            await query.edit_message_text("该用户当前没有活动会话。")
        return

    if data.startswith("admin_ban:"):
        try:
            uid = int(data.split(":",1)[1])
        except:
            await query.edit_message_text("ID 格式错误")
            return
        ban_user_db(uid)
        pending_requests.discard(uid)
        active_sessions.discard(uid)
        await query.edit_message_text(f"🚫 已封禁用户 `{uid}`。", parse_mode="Markdown")
        try:
            await context.bot.send_message(chat_id=uid, text="你已被管理员封禁，无法再申请或接收管理员消息。")
        except:
            pass
        return

    if data == "admin_hint_connect":
        await query.edit_message_text("提示：使用 /connect <user_id> 来主动连接用户（管理员无需用户申请）。", reply_markup=admin_panel_keyboard())
        return

    if data == "admin_help":
        await query.edit_message_text("管理员帮助：使用 /help 查看完整命令。", reply_markup=admin_panel_keyboard())
        return

    await query.answer(text="未识别的操作。")

# ----------------- MESSAGE RELAY -----------------
async def message_relay_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg: Message = update.effective_message
    sender_id = update.effective_user.id

    # ADMIN path: if admin replies to one of the admin-side messages (we mapped msg_id->user)
    if is_admin_update(update):
        reply = msg.reply_to_message
        if reply and reply.message_id in admin_msgid_to_user:
            target_user = admin_msgid_to_user[reply.message_id]
            try:
                copied = await msg.copy(chat_id=target_user)
                user_last_admin_msgid[target_user] = copied.message_id
                await msg.reply_text(f"已发送给用户 {target_user}")
            except Exception as e:
                logger.exception("admin -> user copy failed")
                await msg.reply_text(f"发送失败：{e}")
            return
        await msg.reply_text("要回复某个用户，请在管理面板查看活动会话并回复对应消息，或使用 /connect <user_id>。")
        return

    # USER path
    if is_banned_db(sender_id):
        await msg.reply_text("你已被封禁，无法使用该服务。")
        return

    if sender_id in active_sessions:
        try:
            # send to first numeric admin if available, else try username of first admin
            sent_to = None
            # try numeric admins first
            for aid in numeric_admin_ids:
                try:
                    copied = await msg.copy(chat_id=aid)
                    admin_msgid_to_user[copied.message_id] = sender_id
                    user_last_admin_msgid[sender_id] = copied.message_id
                    sent_to = aid
                    break
                except:
                    continue
            if sent_to is None:
                # fallback: try username list
                for name in ADMIN_USERNAMES:
                    try:
                        copied = await msg.copy(chat_id=f"@{name}")
                        admin_msgid_to_user[copied.message_id] = sender_id
                        user_last_admin_msgid[sender_id] = copied.message_id
                        sent_to = name
                        break
                    except:
                        continue
            if sent_to is None:
                await msg.reply_text("发送失败：管理员不可达。")
        except Exception:
            logger.exception("user -> admin copy failed")
            try:
                await msg.reply_text("发送失败，请稍后重试。")
            except:
                pass
        return

    if sender_id in pending_requests:
        await msg.reply_text("⏳ 你的申请正在等待管理员处理，请耐心等待或点击取消。", reply_markup=user_main_keyboard(is_pending=True, is_active=False))
        return

    await msg.reply_text("你当前尚未申请与管理员聊天。点击下面按钮申请：", reply_markup=user_main_keyboard(is_pending=False, is_active=False))
    return

# ----------------- STARTUP / MAIN -----------------
def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Try to resolve admin usernames to numeric ids on startup
    try:
        resolved = app.run_sync(resolve_admin_usernames_to_ids:=lambda app=app: None)  # placeholder to satisfy typing (we'll run proper coro below)
    except Exception:
        resolved = None

    async def _startup_resolve():
        nonlocal numeric_admin_ids
        res = await resolve_admin_usernames_to_ids(app)
        numeric_admin_ids.update(res)

    # schedule startup resolution before polling
    app.job_queue.run_once(lambda ctx: _startup_resolve(), when=0)

    # handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("register_admin", register_admin_cmd))
    app.add_handler(CommandHandler("connect", connect_cmd))
    app.add_handler(CommandHandler("end", end_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("send", send_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))

    app.add_handler(CallbackQueryHandler(callback_query_handler))
    app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), message_relay_handler))

    logger.info("Bot starting (polling)...")
    app.run_polling()

if __name__ == "__main__":
    main()
