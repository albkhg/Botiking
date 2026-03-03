import os
import random
import logging
import asyncio
import sqlite3
import json
import hashlib
import socks
import socket
from datetime import datetime, timedelta
from cryptography.fernet import Fernet
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, ConversationHandler
from telegram.error import TelegramError
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO

# ==================== KONFIGURIMI ====================
ADMIN_ID = 8258210468
BOT_TOKEN = "8404790685:AAFN9VQ7v-gWXPDCeteQ5zGVWhb2vSbg_YA"
DATABASE_FILE = "bot_data.db"
ENCRYPTION_KEY = Fernet.generate_key()
cipher = Fernet(ENCRYPTION_KEY)
CAPTCHA_ATTEMPTS_MAX = 3
CAPTCHA_TIMEOUT = 300
REFERRAL_BONUS = 1  # Sa "pikë" merr referuesi për çdo mik të sjellë

# Tor proxy
socks.set_default_proxy(socks.SOCKS5, "127.0.0.1", 9050)
socket.socket = socks.socksocket

# ==================== DATABASE SETUP ====================
def init_db():
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    
    # Tabelat ekzistuese...
    c.execute('''CREATE TABLE IF NOT EXISTS captcha_attempts
                 (id INTEGER PRIMARY KEY, user_id INTEGER, chat_id TEXT, attempts INTEGER DEFAULT 0, solved INTEGER DEFAULT 0, timestamp TEXT)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS group_links
                 (id INTEGER PRIMARY KEY, group_name TEXT, invite_link TEXT, description TEXT, added_by INTEGER, timestamp TEXT)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS captcha_images
                 (id INTEGER PRIMARY KEY, filename TEXT, correct_answer TEXT, hint TEXT)''')
    
    # TABELA E RE: Referime
    c.execute('''CREATE TABLE IF NOT EXISTS referrals
                 (id INTEGER PRIMARY KEY, 
                  referrer_id INTEGER, 
                  referred_id INTEGER UNIQUE,
                  referred_username TEXT,
                  timestamp TEXT,
                  bonus_given INTEGER DEFAULT 0)''')
    
    # TABELA E RE: Përdoruesit dhe statistikat e referimeve
    c.execute('''CREATE TABLE IF NOT EXISTS user_stats
                 (user_id INTEGER PRIMARY KEY,
                  username TEXT,
                  first_name TEXT,
                  referral_code TEXT UNIQUE,
                  total_referrals INTEGER DEFAULT 0,
                  friends_list TEXT DEFAULT '[]',
                  join_date TEXT,
                  last_active TEXT)''')
    
    # TABELA E RE: Access log - kush ka hyr pa referral
    c.execute('''CREATE TABLE IF NOT EXISTS access_log
                 (id INTEGER PRIMARY KEY,
                  user_id INTEGER,
                  access_type TEXT,  -- 'direct' ose 'referral'
                  referrer_id INTEGER DEFAULT NULL,
                  timestamp TEXT)''')
    
    # Insert sample images if not exist
    c.execute("SELECT COUNT(*) FROM captcha_images")
    if c.fetchone()[0] == 0:
        sample_images = [
            ("albkings_logo.jpg", "albkings", "Logo i grupit të parë shqiptar"),
            ("albkings_screenshot.png", "albkings", "Screenshot nga grupi më i madh"),
            ("albanian_flag.jpg", "shqiptari", "Flamuri kombëtar")
        ]
        c.executemany("INSERT INTO captcha_images (filename, correct_answer, hint) VALUES (?, ?, ?)", sample_images)
    
    conn.commit()
    conn.close()

def get_user_stats(user_id):
    """Merr statistikat e një përdoruesi."""
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT * FROM user_stats WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result

def create_user_stats(user_id, username, first_name):
    """Krijon statistikat për një përdorues të ri."""
    referral_code = hashlib.md5(f"{user_id}{username}{datetime.now()}".encode()).hexdigest()[:10]
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute('''INSERT OR IGNORE INTO user_stats 
                 (user_id, username, first_name, referral_code, join_date, last_active) 
                 VALUES (?, ?, ?, ?, ?, ?)''',
              (user_id, username, first_name, referral_code, datetime.now().isoformat(), datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return referral_code

def add_referral(referrer_id, referred_id, referred_username):
    """Shton një referral të ri dhe përditëson listën e miqve."""
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    
    # Shto në tabelën referrals
    c.execute('''INSERT OR IGNORE INTO referrals 
                 (referrer_id, referred_id, referred_username, timestamp, bonus_given) 
                 VALUES (?, ?, ?, ?, ?)''',
              (referrer_id, referred_id, referred_username, datetime.now().isoformat(), 1))
    
    # Përditëso total_referrals për referrer
    c.execute("UPDATE user_stats SET total_referrals = total_referrals + 1 WHERE user_id = ?", (referrer_id,))
    
    # Përditëso friends_list për referrer
    c.execute("SELECT friends_list FROM user_stats WHERE user_id = ?", (referrer_id,))
    result = c.fetchone()
    if result:
        friends_list = json.loads(result[0])
        friends_list.append({
            'user_id': referred_id,
            'username': referred_username,
            'joined': datetime.now().isoformat()
        })
        c.execute("UPDATE user_stats SET friends_list = ? WHERE user_id = ?", (json.dumps(friends_list), referrer_id))
    
    conn.commit()
    conn.close()

def can_access_without_referral(user_id):
    """Kontrollon nëse përdoruesi mund të hyjë pa referral."""
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    
    # Admin gjithmonë mundet
    if user_id == ADMIN_ID:
        conn.close()
        return True
    
    # Kontrollo sa herë ka hyrë pa referral
    c.execute('''SELECT COUNT(*) FROM access_log 
                 WHERE user_id = ? AND access_type = 'direct' 
                 AND date(timestamp) = date('now')''', (user_id,))
    count = c.fetchone()[0]
    
    conn.close()
    
    # Lejo vetëm 0 herë në ditë pa referral
    return count < 1  # Ndrysho në 0 për të bllokuar krejt pa referral

def log_access(user_id, access_type, referrer_id=None):
    """Logon aksesin e përdoruesit."""
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute('''INSERT INTO access_log (user_id, access_type, referrer_id, timestamp) 
                 VALUES (?, ?, ?, ?)''',
              (user_id, access_type, referrer_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_referral_info(referral_code):
    """Merr informacion për një kod referral."""
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id, username, first_name FROM user_stats WHERE referral_code = ?", (referral_code,))
    result = c.fetchone()
    conn.close()
    return result

def get_friends_list(user_id):
    """Merr listën e miqve të sjellë nga përdoruesi."""
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT friends_list FROM user_stats WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    if result and result[0]:
        return json.loads(result[0])
    return []

# ==================== CAPTCHA FUNCTIONS ====================
def get_random_captcha():
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT filename, correct_answer FROM captcha_images ORDER BY RANDOM() LIMIT 1")
    result = c.fetchone()
    conn.close()
    return result if result else ("default.jpg", "albkings")

def generate_dynamic_captcha():
    img = Image.new('RGB', (400, 200), color=(random.randint(0,255), random.randint(0,255), random.randint(0,255)))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 36)
    except:
        font = ImageFont.load_default()
    
    questions = [
        "Cili grup është i pari në Shqiptari?",
        "Grupi më i madh shqiptar në Telegram?",
        "Albkings apo ndonjë tjetër?"
    ]
    question = random.choice(questions)
    d.text((10, 10), question, fill=(255,255,255), font=font)
    
    bio = BytesIO()
    img.save(bio, 'JPEG')
    bio.seek(0)
    return bio, "albkings"

async def send_captcha(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id, chat_id):
    use_dynamic = random.choice([True, False])
    if use_dynamic:
        image_bytes, correct_answer = generate_dynamic_captcha()
    else:
        filename, correct_answer = get_random_captcha()
        image_path = os.path.join("captcha_images", filename)
        if not os.path.exists(image_path):
            image_bytes, correct_answer = generate_dynamic_captcha()
        else:
            with open(image_path, 'rb') as f:
                image_bytes = f.read()

    context.user_data['captcha_answer'] = correct_answer.lower().strip()
    context.user_data['captcha_time'] = datetime.now()
    context.user_data['captcha_attempts'] = 0

    await context.bot.send_photo(
        chat_id=chat_id,
        photo=image_bytes,
        caption="🔐 **Verifikim CAPTCHA**\n\nPër të vazhduar, përgjigjuni pyetjes:\n**Cili grup është i pari në Shqiptari në Telegram?**\n\nShkruani emrin e grupit (p.sh., Albkings).",
        parse_mode='Markdown'
    )

    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO captcha_attempts (user_id, chat_id, timestamp) VALUES (?, ?, ?)",
              (user_id, chat_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()

async def verify_captcha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_answer = update.message.text.lower().strip()
    correct_answer = context.user_data.get('captcha_answer')
    captcha_time = context.user_data.get('captcha_time')
    attempts = context.user_data.get('captcha_attempts', 0)

    if captcha_time and (datetime.now() - captcha_time) > timedelta(seconds=CAPTCHA_TIMEOUT):
        await update.message.reply_text("⏰ Koha e CAPTCHA-s ka kaluar. Dërgoni /start për të provuar përsëri.")
        context.user_data.clear()
        return

    if not correct_answer:
        await update.message.reply_text("❌ Nuk ka CAPTCHA aktive. Dërgoni /start për të filluar.")
        return

    if user_answer in ['albkings', 'alb kings', 'albking', 'albkings']:
        await update.message.reply_text("✅ **Verifikimi i suksesshëm!**", parse_mode='Markdown')
        
        # Kontrollo nëse ka ardhur përmes referralit
        referrer_id = context.user_data.get('referrer_id')
        if referrer_id:
            # Shto referral
            add_referral(referrer_id, user_id, update.effective_user.username or "pa_username")
            await update.message.reply_text(f"🌟 Jeni regjistruar përmes referralit të {context.user_data.get('referrer_name', 'përdoruesit')}!")
        
        # Dërgo linket
        await send_group_links(update, context)
        
        context.user_data.clear()
        
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute("UPDATE captcha_attempts SET solved=1 WHERE user_id=? AND solved=0", (user_id,))
        conn.commit()
        conn.close()
    else:
        attempts += 1
        context.user_data['captcha_attempts'] = attempts
        if attempts >= CAPTCHA_ATTEMPTS_MAX:
            await update.message.reply_text("❌ **Shumë përpjekje të gabuara!** Qasja e bllokuar përkohësisht.")
            context.user_data.clear()
        else:
            await update.message.reply_text(f"❌ Përgjigje e gabuar. Përpjekjet e mbetura: {CAPTCHA_ATTEMPTS_MAX - attempts}\n\nProvoni përsëri:")

async def send_group_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT group_name, invite_link, description FROM group_links ORDER BY id DESC")
    links = c.fetchall()
    conn.close()

    if links:
        message = "🔗 **Linket e grupeve private:**\n\n"
        for group_name, invite_link, description in links:
            message += f"• **{group_name}**: [Link]({invite_link})\n  {description}\n\n"
    else:
        message = "📭 Nuk ka grupe të shtuara ende nga admini."

    await update.message.reply_text(message, parse_mode='Markdown', disable_web_page_preview=True)

# ==================== REFERRAL COMMANDS ====================
async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ref - Shfaq kodin e referralit dhe statistikat."""
    user = update.effective_user
    user_id = user.id
    
    # Krijo statistikat nëse nuk ekzistojnë
    stats = get_user_stats(user_id)
    if not stats:
        referral_code = create_user_stats(user_id, user.username, user.first_name)
    else:
        referral_code = stats[4]  # Indeksi i referral_code
    
    total_refs = stats[5] if stats else 0
    friends = get_friends_list(user_id)
    
    # Krijo linkun e referralit
    bot_username = (await context.bot.get_me()).username
    referral_link = f"https://t.me/{bot_username}?start={referral_code}"
    
    message = f"👥 **Referimet Tuaja**\n\n"
    message += f"🔑 **Kodi juaj:** `{referral_code}`\n"
    message += f"🔗 **Linku juaj:** {referral_link}\n\n"
    message += f"📊 **Statistikat:**\n"
    message += f"• Total i referimeve: **{total_refs}**\n"
    message += f"• Miqtë e sjellë: **{len(friends)}**\n\n"
    
    if friends:
        message += "**Lista e miqve:**\n"
        for i, friend in enumerate(friends[-10:], 1):  # Shfaq 10 të fundit
            username = friend.get('username', 'pa_username')
            joined = friend.get('joined', 'e panjohur')[:10]
            message += f"{i}. @{username} - {joined}\n"
    
    await update.message.reply_text(message, parse_mode='Markdown', disable_web_page_preview=True)

async def friends_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/friends - Shfaq listën e miqve të sjellë."""
    user_id = update.effective_user.id
    friends = get_friends_list(user_id)
    
    if not friends:
        await update.message.reply_text("📭 Nuk keni sjellë asnjë mik akoma. Përdorni /ref për të marrë linkun tuaj të referralit!")
        return
    
    message = "👥 **Miqtë që keni sjellë:**\n\n"
    for i, friend in enumerate(friends, 1):
        username = friend.get('username', 'pa_username')
        joined = friend.get('joined', 'e panjohur')[:10]
        message += f"{i}. @{username} - {joined}\n"
    
    await update.message.reply_text(message, parse_mode='Markdown')

# ==================== MODIFIED START HANDLER ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    user_id = user.id
    args = context.args  # Argumentet pas /start (p.sh., /start referral_code)
    
    # Krijo statistikat për përdoruesin nëse nuk ekzistojnë
    create_user_stats(user_id, user.username, user.first_name)
    
    # Kontrollo nëse ka referral code në args
    referrer_info = None
    if args and args[0]:
        referral_code = args[0]
        referrer_info = get_referral_info(referral_code)
        
        if referrer_info and referrer_info[0] != user_id:  # Nëse nuk është vetja
            context.user_data['referrer_id'] = referrer_info[0]
            context.user_data['referrer_name'] = referrer_info[2] or referrer_info[1] or "përdorues"
            log_access(user_id, 'referral', referrer_info[0])
            
            await update.message.reply_text(
                f"👋 Mirë se vini, {user.first_name}!\n"
                f"✨ Jeni sjellë nga {context.user_data['referrer_name']}!\n"
                f"Verifikoni CAPTCHA për të vazhduar dhe për të marrë linket."
            )
        else:
            # Referral code i pavlefshëm ose i vetes
            await update.message.reply_text("⚠️ Kod referrali i pavlefshëm. Vazhdoni pa referral.")
            log_access(user_id, 'direct')
    else:
        # Pa referral code - kontrollo nëse mundet pa referral
        if not can_access_without_referral(user_id):
            await update.message.reply_text(
                "🚫 **Qasja e kufizuar**\n\n"
                "Për të marrë linket e grupeve private, duhet të vini përmes një referrali.\n\n"
                "Kërkoni një mik që është tashmë në grup t'ju dërgojë linkun e tij të referralit.\n"
                "Ai mund të përdorë /ref për të marrë linkun personal."
            )
            log_access(user_id, 'direct_denied')
            return
        else:
            log_access(user_id, 'direct')
            await update.message.reply_text(
                f"Mirë se vini, {user.first_name}!\n"
                f"Për të marrë linket e grupeve private, duhet të verifikoni që jeni njeri."
            )
    
    # Kontrollo nëse e ka kaluar CAPTCHA më parë
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT solved FROM captcha_attempts WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,))
    result = c.fetchone()
    conn.close()

    if result and result[0] == 1:
        await update.message.reply_text(f"✅ Mirë se vini përsëri, {user.first_name}!")
        await send_group_links(update, context)
    else:
        await send_captcha(update, context, user_id, chat_id)

# ==================== ADMIN COMMANDS ====================
async def admin_add_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ I paautorizuar.")
        return

    try:
        parts = update.message.text.split(maxsplit=2)
        if len(parts) < 3:
            await update.message.reply_text("Përdorimi: /addlink Emri_Grupit link_uuid Përshkrimi")
            return
        group_name = parts[1]
        invite_link = parts[2].split()[0]
        description = ' '.join(parts[2].split()[1:]) if len(parts[2].split()) > 1 else "Pa përshkrim"

        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute("INSERT INTO group_links (group_name, invite_link, description, added_by, timestamp) VALUES (?, ?, ?, ?, ?)",
                  (group_name, invite_link, description, ADMIN_ID, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"✅ Linku për {group_name} u shtua.")
    except Exception as e:
        await update.message.reply_text(f"Gabim: {e}")

async def admin_list_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return
    await send_group_links(update, context)

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Statistika të përgjithshme për admin."""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return
    
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    
    # Total përdoruesish
    c.execute("SELECT COUNT(DISTINCT user_id) FROM user_stats")
    total_users = c.fetchone()[0]
    
    # Total referrals
    c.execute("SELECT COUNT(*) FROM referrals")
    total_refs = c.fetchone()[0]
    
    # Referuesit më të mirë
    c.execute('''SELECT u.username, u.first_name, u.total_referrals 
                 FROM user_stats u 
                 ORDER BY u.total_referrals DESC 
                 LIMIT 5''')
    top_referrers = c.fetchall()
    
    # Akseset sot
    c.execute("SELECT COUNT(*) FROM access_log WHERE date(timestamp) = date('now')")
    accesses_today = c.fetchone()[0]
    
    conn.close()
    
    message = "📊 **Statistikat e Botit**\n\n"
    message += f"👥 Total përdorues: **{total_users}**\n"
    message += f"🔄 Total referime: **{total_refs}**\n"
    message += f"📈 Akses sot: **{accesses_today}**\n\n"
    message += "🏆 **Referuesit më të mirë:**\n"
    
    for i, (username, first_name, total) in
