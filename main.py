import asyncio
import os
import sys
import logging
import subprocess
import psutil
import psycopg2
from psycopg2 import sql
import hashlib
import json
import zipfile
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
import aiohttp
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv('BOT_TOKEN')
OWNER_ID_STR = os.getenv('OWNER_ID')
ADMIN_ID_STR = os.getenv('ADMIN_ID')
YOUR_USERNAME = os.getenv('YOUR_USERNAME')
UPDATE_CHANNEL = os.getenv('UPDATE_CHANNEL')

# PostgreSQL Connection
DATABASE_URL = os.getenv('DATABASE_URL')

if not TOKEN:
    logger.error("BOT_TOKEN not found in environment variables!")
    raise ValueError("BOT_TOKEN is required. Please set it in .env file or environment variables.")

if not OWNER_ID_STR or not ADMIN_ID_STR:
    logger.error("OWNER_ID or ADMIN_ID not found in environment variables!")
    raise ValueError("OWNER_ID and ADMIN_ID are required. Please set them in .env file.")

if not DATABASE_URL:
    logger.error("DATABASE_URL not found in environment variables!")
    raise ValueError("DATABASE_URL is required. Please set it in environment variables.")

try:
    OWNER_ID = int(OWNER_ID_STR)
    ADMIN_ID = int(ADMIN_ID_STR)
except ValueError:
    logger.error("OWNER_ID or ADMIN_ID must be valid integers!")
    raise

YOUR_USERNAME = YOUR_USERNAME or '@LUFFY_49'
UPDATE_CHANNEL = UPDATE_CHANNEL or 'https://t.me/LUFFY_HACKER'

BASE_DIR = Path(__file__).parent.absolute()
UPLOAD_BOTS_DIR = BASE_DIR / 'upload_bots'
IROTECH_DIR = BASE_DIR / 'inf'

FREE_USER_LIMIT = 20
SUBSCRIBED_USER_LIMIT = 50
ADMIN_LIMIT = 999
OWNER_LIMIT = float('inf')

UPLOAD_BOTS_DIR.mkdir(exist_ok=True)
IROTECH_DIR.mkdir(exist_ok=True)

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

bot_scripts = {}
user_subscriptions = {}
user_files = {}
user_favorites = {}
banned_users = set()
active_users = set()
admin_ids = {ADMIN_ID, OWNER_ID}
bot_locked = False
bot_stats = {'total_uploads': 0, 'total_downloads': 0, 'total_runs': 0}

def get_db_connection():
    """Get PostgreSQL connection"""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        raise

def migrate_db():
    logger.info("Running database migrations...")
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        # Check and add columns if needed
        c.execute("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name='user_files' AND column_name='upload_date'
        """)
        if not c.fetchone():
            logger.info("Adding upload_date column to user_files table...")
            c.execute('ALTER TABLE user_files ADD COLUMN upload_date TEXT')
            logger.info("upload_date column added successfully.")
        
        c.execute("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name='active_users' AND column_name='join_date'
        """)
        if not c.fetchone():
            logger.info("Adding join_date column to active_users table...")
            c.execute('ALTER TABLE active_users ADD COLUMN join_date TEXT')
            logger.info("join_date column added successfully.")
        
        c.execute("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name='active_users' AND column_name='last_active'
        """)
        if not c.fetchone():
            logger.info("Adding last_active column to active_users table...")
            c.execute('ALTER TABLE active_users ADD COLUMN last_active TEXT')
            logger.info("last_active column added successfully.")
        
        conn.commit()
        conn.close()
        logger.info("Database migrations completed successfully.")
    except Exception as e:
        logger.error(f"Database migration error: {e}", exc_info=True)

def init_db():
    logger.info("Initializing database...")
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                     (user_id INTEGER PRIMARY KEY, expiry TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_files
                     (user_id INTEGER, file_name TEXT, file_type TEXT, upload_date TEXT,
                      PRIMARY KEY (user_id, file_name))''')
        c.execute('''CREATE TABLE IF NOT EXISTS active_users
                     (user_id INTEGER PRIMARY KEY, join_date TEXT, last_active TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS admins
                     (user_id INTEGER PRIMARY KEY)''')
        c.execute('''CREATE TABLE IF NOT EXISTS banned_users
                     (user_id INTEGER PRIMARY KEY, banned_date TEXT, reason TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS favorites
                     (user_id INTEGER, file_name TEXT, PRIMARY KEY (user_id, file_name))''')
        c.execute('''CREATE TABLE IF NOT EXISTS bot_stats
                     (stat_name TEXT PRIMARY KEY, stat_value INTEGER)''')
        
        c.execute('INSERT INTO admins (user_id) VALUES (%s) ON CONFLICT DO NOTHING', (OWNER_ID,))
        if ADMIN_ID != OWNER_ID:
            c.execute('INSERT INTO admins (user_id) VALUES (%s) ON CONFLICT DO NOTHING', (ADMIN_ID,))
        
        for stat in ['total_uploads', 'total_downloads', 'total_runs']:
            c.execute('INSERT INTO bot_stats (stat_name, stat_value) VALUES (%s, 0) ON CONFLICT DO NOTHING', (stat,))
        
        conn.commit()
        conn.close()
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Database initialization error: {e}", exc_info=True)

def load_data():
    logger.info("Loading data from database...")
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        c.execute('SELECT user_id, expiry FROM subscriptions')
        for user_id, expiry in c.fetchall():
            try:
                user_subscriptions[user_id] = {'expiry': datetime.fromisoformat(expiry)}
            except ValueError:
                logger.warning(f"Invalid expiry date for user {user_id}")
        
        c.execute('SELECT user_id, file_name, file_type FROM user_files')
        for user_id, file_name, file_type in c.fetchall():
            if user_id not in user_files:
                user_files[user_id] = []
            user_files[user_id].append((file_name, file_type))
        
        c.execute('SELECT user_id FROM active_users')
        active_users.update(user_id for (user_id,) in c.fetchall())
        
        c.execute('SELECT user_id FROM admins')
        admin_ids.update(user_id for (user_id,) in c.fetchall())
        
        c.execute('SELECT user_id FROM banned_users')
        banned_users.update(user_id for (user_id,) in c.fetchall())
        
        c.execute('SELECT user_id, file_name FROM favorites')
        for user_id, file_name in c.fetchall():
            if user_id not in user_favorites:
                user_favorites[user_id] = []
            user_favorites[user_id].append(file_name)
        
        c.execute('SELECT stat_name, stat_value FROM bot_stats')
        for stat_name, stat_value in c.fetchall():
            bot_stats[stat_name] = stat_value
        
        conn.close()
        logger.info(f"Data loaded: {len(active_users)} users, {len(banned_users)} banned, {len(admin_ids)} admins.")
    except Exception as e:
        logger.error(f"Error loading data: {e}", exc_info=True)

init_db()
migrate_db()
load_data()

def get_user_file_limit(user_id):
    if user_id == OWNER_ID: return OWNER_LIMIT
    if user_id in admin_ids: return ADMIN_LIMIT
    if user_id in user_subscriptions and user_subscriptions[user_id]['expiry'] > datetime.now():
        return SUBSCRIBED_USER_LIMIT
    return FREE_USER_LIMIT

def get_main_keyboard(user_id):
    if user_id in admin_ids:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Updates", url=UPDATE_CHANNEL)],
            [InlineKeyboardButton(text="📤 Upload File", callback_data="upload_file"),
             InlineKeyboardButton(text="📁 My Files", callback_data="check_files")],
            [InlineKeyboardButton(text="⭐ Favorites", callback_data="my_favorites"),
             InlineKeyboardButton(text="🔍 Search Files", callback_data="search_files")],
            [InlineKeyboardButton(text="⚡ Bot Speed", callback_data="bot_speed"),
             InlineKeyboardButton(text="📊 My Stats", callback_data="statistics")],
            [InlineKeyboardButton(text="ℹ️ Help & Info", callback_data="help_info"),
             InlineKeyboardButton(text="🎯 Features", callback_data="all_features")],
            [InlineKeyboardButton(text="👨‍💼 Admin Panel", callback_data="admin_panel"),
             InlineKeyboardButton(text="💬 Contact", url=f"https://t.me/{YOUR_USERNAME.replace('@', '')}")]
        ])
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Updates Channel", url=UPDATE_CHANNEL)],
            [InlineKeyboardButton(text="📤 Upload File", callback_data="upload_file"),
             InlineKeyboardButton(text="📁 My Files", callback_data="check_files")],
            [InlineKeyboardButton(text="⭐ Favorites", callback_data="my_favorites"),
             InlineKeyboardButton(text="🔍 Search Files", callback_data="search_files")],
            [InlineKeyboardButton(text="⚡ Bot Speed", callback_data="bot_speed"),
             InlineKeyboardButton(text="📊 My Stats", callback_data="statistics")],
            [InlineKeyboardButton(text="ℹ️ Help & Info", callback_data="help_info"),
             InlineKeyboardButton(text="🎯 Features", callback_data="all_features")],
            [InlineKeyboardButton(text="💬 Contact", url=f"https://t.me/{YOUR_USERNAME.replace('@', '')}")]
        ])
    return keyboard

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    
    if user_id in banned_users:
        await message.answer("❌ You are banned from using this bot!")
        return
    
    if user_id not in active_users:
        active_users.add(user_id)
        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute('INSERT INTO active_users (user_id, join_date, last_active) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET last_active = %s',
                     (user_id, datetime.now().isoformat(), datetime.now().isoformat(), datetime.now().isoformat()))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error saving user: {e}")
    
    text = f"""
╔═══════════════════════════════════╗
    🚀 WELCOME TO FILE HOST BOT 🚀
╚═══════════════════════════════════╝

👋 Hi {message.from_user.first_name}!

📁 Upload, manage, and run your scripts easily!

🎯 <b>Quick Features:</b>
✅ Upload Python/JavaScript files
✅ Run scripts with real-time output
✅ Manage favorites
✅ Premium access available

📢 Updates: {UPDATE_CHANNEL}

Ready to get started? 👇
"""
    
    await message.answer(text, reply_markup=get_main_keyboard(user_id), parse_mode="HTML")

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(call: types.CallbackQuery):
    user_id = call.from_user.id
    text = "🏠 <b>Main Menu</b>"
    await call.message.edit_text(text, reply_markup=get_main_keyboard(user_id), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "upload_file")
async def upload_file_start(call: types.CallbackQuery):
    user_id = call.from_user.id
    limit = get_user_file_limit(user_id)
    current_files = len(user_files.get(user_id, []))
    
    text = f"""
📤 <b>UPLOAD FILE</b>

📊 Your Limit: {current_files}/{limit}

📝 Supported formats:
✅ .py (Python)
✅ .js (JavaScript)
✅ .zip (Archive)

Send your file now! 📁
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await call.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await call.answer()

@dp.message(F.document)
async def handle_file_upload(message: types.Message):
    user_id = message.from_user.id
    
    if user_id in banned_users:
        await message.answer("❌ You are banned!")
        return
    
    limit = get_user_file_limit(user_id)
    current_files = len(user_files.get(user_id, []))
    
    if current_files >= limit:
        await message.answer(f"❌ File limit reached! ({current_files}/{limit})")
        return
    
    file = message.document
    file_name = file.file_name
    
    if not file_name.endswith(('.py', '.js', '.zip')):
        await message.answer("❌ Invalid file format! Supported: .py, .js, .zip")
        return
    
    try:
        file_info = await bot.get_file(file.file_id)
        file_path = file_info.file_path
        
        local_file_path = UPLOAD_BOTS_DIR / file_name
        await bot.download_file(file_path, destination=local_file_path)
        
        file_type = file_name.split('.')[-1]
        
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('INSERT INTO user_files (user_id, file_name, file_type, upload_date) VALUES (%s, %s, %s, %s)',
                 (user_id, file_name, file_type, datetime.now().isoformat()))
        
        stat_value = c.execute('SELECT stat_value FROM bot_stats WHERE stat_name = %s', ('total_uploads',))
        c.execute('UPDATE bot_stats SET stat_value = stat_value + 1 WHERE stat_name = %s', ('total_uploads',))
        
        conn.commit()
        conn.close()
        
        if user_id not in user_files:
            user_files[user_id] = []
        user_files[user_id].append((file_name, file_type))
        bot_stats['total_uploads'] += 1
        
        icon = "🐍" if file_type == "py" else "🟨" if file_type == "js" else "📦"
        text = f"""
✅ <b>File Uploaded!</b>

{icon} File: <code>{file_name}</code>
📊 Size: {file.file_size / 1024:.2f} KB
⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

Ready to use! 🚀
"""
        await message.answer(text, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Upload error: {e}")
        await message.answer(f"❌ Upload failed: {str(e)}")

@dp.callback_query(F.data == "check_files")
async def check_files(call: types.CallbackQuery):
    user_id = call.from_user.id
    user_file_list = user_files.get(user_id, [])
    
    if not user_file_list:
        text = "📁 <b>MY FILES</b>\n\n❌ No files uploaded yet!"
        back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Upload File", callback_data="upload_file")],
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
        ])
        await call.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
        await call.answer()
        return
    
    text = f"📁 <b>MY FILES ({len(user_file_list)})</b>\n\n"
    
    keyboard = []
    for file_name, file_type in user_file_list:
        icon = "🐍" if file_type == "py" else "🟨" if file_type == "js" else "📦"
        text += f"{icon} <code>{file_name}</code>\n"
        keyboard.append([InlineKeyboardButton(text=f"⚙️ {file_name}", callback_data=f"file_menu_{file_name}")])
    
    keyboard.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")])
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard)
    await call.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data.startswith("file_menu_"))
async def file_menu(call: types.CallbackQuery):
    user_id = call.from_user.id
    file_name = call.data.replace("file_menu_", "")
    
    text = f"""
⚙️ <b>FILE OPTIONS</b>

📄 File: <code>{file_name}</code>

Select an action:
"""
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Run", callback_data=f"run_file_{file_name}")],
        [InlineKeyboardButton(text="⭐ Add to Favorites", callback_data=f"add_fav_{file_name}")],
        [InlineKeyboardButton(text="🗑️ Delete", callback_data=f"delete_file_{file_name}")],
        [InlineKeyboardButton(text="📁 My Files", callback_data="check_files")]
    ])
    
    await call.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data.startswith("run_file_"))
async def run_file(call: types.CallbackQuery):
    user_id = call.from_user.id
    file_name = call.data.replace("run_file_", "")
    
    file_path = UPLOAD_BOTS_DIR / file_name
    
    if not file_path.exists():
        await call.answer("❌ File not found!")
        return
    
    try:
        status_msg = await call.message.answer(f"▶️ Running <code>{file_name}</code>...\n\n⏳ Please wait...", parse_mode="HTML")
        
        if file_name.endswith('.py'):
            result = subprocess.run(['python3', str(file_path)], capture_output=True, text=True, timeout=30)
        elif file_name.endswith('.js'):
            result = subprocess.run(['node', str(file_path)], capture_output=True, text=True, timeout=30)
        else:
            await call.answer("❌ File type not supported for execution!")
            return
        
        output = result.stdout or result.stderr or "No output"
        
        if len(output) > 4000:
            output = output[:4000] + "\n\n... (output truncated)"
        
        text = f"""
✅ <b>EXECUTION COMPLETE</b>

📄 File: <code>{file_name}</code>

<b>Output:</b>
<code>{output}</code>
"""
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Run Again", callback_data=f"run_file_{file_name}")],
            [InlineKeyboardButton(text="📁 My Files", callback_data="check_files")]
        ])
        
        await status_msg.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        
        bot_stats['total_runs'] += 1
        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute('UPDATE bot_stats SET stat_value = stat_value + 1 WHERE stat_name = %s', ('total_runs',))
            conn.commit()
            conn.close()
        except:
            pass
        
    except subprocess.TimeoutExpired:
        await call.message.answer("❌ Script execution timeout (30 seconds)!")
    except Exception as e:
        logger.error(f"Execution error: {e}")
        await call.message.answer(f"❌ Error running script: {str(e)}")

@dp.callback_query(F.data.startswith("delete_file_"))
async def delete_file(call: types.CallbackQuery):
    user_id = call.from_user.id
    file_name = call.data.replace("delete_file_", "")
    
    file_path = UPLOAD_BOTS_DIR / file_name
    
    try:
        if file_path.exists():
            file_path.unlink()
        
        if user_id in user_files:
            user_files[user_id] = [(f, t) for f, t in user_files[user_id] if f != file_name]
        
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('DELETE FROM user_files WHERE user_id = %s AND file_name = %s', (user_id, file_name))
        c.execute('DELETE FROM favorites WHERE user_id = %s AND file_name = %s', (user_id, file_name))
        conn.commit()
        conn.close()
        
        if user_id in user_favorites and file_name in user_favorites[user_id]:
            user_favorites[user_id].remove(file_name)
        
        await call.answer("✅ File deleted!")
        await call.message.edit_text("📁 <b>MY FILES</b>\n\n✅ File deleted successfully!", 
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📁 My Files", callback_data="check_files")]]), 
                                     parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Delete error: {e}")
        await call.answer(f"❌ Error: {str(e)}")

@dp.callback_query(F.data.startswith("add_fav_"))
async def add_favorite(call: types.CallbackQuery):
    user_id = call.from_user.id
    file_name = call.data.replace("add_fav_", "")
    
    try:
        if user_id not in user_favorites:
            user_favorites[user_id] = []
        
        if file_name not in user_favorites[user_id]:
            user_favorites[user_id].append(file_name)
            
            conn = get_db_connection()
            c = conn.cursor()
            c.execute('INSERT INTO favorites (user_id, file_name) VALUES (%s, %s) ON CONFLICT DO NOTHING', (user_id, file_name))
            conn.commit()
            conn.close()
            
            await call.answer("⭐ Added to favorites!")
        else:
            await call.answer("Already in favorites!")
            
    except Exception as e:
        logger.error(f"Favorite error: {e}")
        await call.answer(f"❌ Error: {str(e)}")

@dp.callback_query(F.data == "my_favorites")
async def my_favorites(call: types.CallbackQuery):
    user_id = call.from_user.id
    fav_list = user_favorites.get(user_id, [])
    
    if not fav_list:
        text = "⭐ <b>FAVORITES</b>\n\n❌ No favorites yet!"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📁 My Files", callback_data="check_files")],
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
        ])
    else:
        text = f"⭐ <b>FAVORITES ({len(fav_list)})</b>\n\n"
        
        for file_name in fav_list:
            file_type = file_name.split('.')[-1]
            icon = "🐍" if file_type == "py" else "🟨" if file_type == "js" else "📦"
            text += f"{icon} <code>{file_name}</code>\n"
        
        keyboard_list = []
        for file_name in fav_list:
            keyboard_list.append([InlineKeyboardButton(text=f"⚙️ {file_name}", callback_data=f"file_menu_{file_name}")])
        
        keyboard_list.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_list)
    
    await call.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "search_files")
async def search_files_start(call: types.CallbackQuery):
    text = """
🔍 <b>SEARCH FILES</b>

Send me a filename to search:
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📁 My Files", callback_data="check_files")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await call.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "bot_speed")
async def bot_speed(call: types.CallbackQuery):
    import time
    start = time.time()
    
    text = f"""
⚡ <b>BOT SPEED TEST</b>

✅ Response Time: {(time.time() - start) * 1000:.2f}ms
🟢 Status: Online
📊 Uptime: Active
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await call.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "statistics")
async def statistics(call: types.CallbackQuery):
    user_id = call.from_user.id
    user_file_count = len(user_files.get(user_id, []))
    user_fav_count = len(user_favorites.get(user_id, []))
    is_premium = user_id in user_subscriptions and user_subscriptions[user_id]['expiry'] > datetime.now()
    
    text = f"""
📊 <b>YOUR STATISTICS</b>

<b>👤 USER INFO:</b>
🆔 User ID: <code>{user_id}</code>
👤 Name: {call.from_user.full_name}
📦 Files: {user_file_count}/{get_user_file_limit(user_id)}
⭐ Favorites: {user_fav_count}
💎 Status: {'Premium ✨' if is_premium else 'Free 🆓'}

━━━━━━━━━━━━━━━━━━━━
📈 <b>USAGE:</b>
📤 Total Uploads: {bot_stats.get('total_uploads', 0)}
📥 Total Downloads: {bot_stats.get('total_downloads', 0)}
▶️ Total Runs: {bot_stats.get('total_runs', 0)}
"""
    
    if user_id in admin_ids:
        text += f"\n━━━━━━━━━━━━━━━━━━━━\n👑 <b>ADMIN STATS:</b>\n"
        text += f"👥 Total Users: {len(active_users)}\n"
        text += f"📁 Total Files: {sum(len(files) for files in user_files.values())}\n"
        text += f"🚫 Banned Users: {len(banned_users)}"
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await call.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "help_info")
async def help_info(call: types.CallbackQuery):
    text = """
ℹ️ <b>HELP & INFO</b>

<b>🎯 HOW TO USE:</b>

1️⃣ <b>Upload Files:</b>
   Click 'Upload File' → Send .py, .js, or .zip

2️⃣ <b>Run Scripts:</b>
   Go to 'My Files' → Click 'Run'

3️⃣ <b>Manage Files:</b>
   Add to favorites ⭐
   Delete unwanted files

4️⃣ <b>Search:</b>
   Use /search [filename]

━━━━━━━━━━━━━━━━━━━━
<b>💡 COMMANDS:</b>
/start - Start bot
/help - This help
/stats - Statistics
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await call.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "all_features")
async def all_features(call: types.CallbackQuery):
    text = """
🎯 <b>ALL FEATURES</b>

✅ File Upload System
   • Python (.py)
   • JavaScript (.js)
   • ZIP Archives (.zip)

✅ Script Execution
   • Real-time output
   • Error handling
   • 30-second timeout

✅ File Management
   • Upload/Delete
   • Favorites ⭐
   • Search functionality

✅ User System
   • Free tier (20 files)
   • Premium tier (50 files)
   • Admin controls

✅ Statistics
   • Personal stats
   • Bot-wide stats
   • Usage tracking

✅ Admin Panel
   • User management
   • Broadcast messages
   • Bot control
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await call.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "admin_panel")
async def admin_panel(call: types.CallbackQuery):
    user_id = call.from_user.id
    
    if user_id not in admin_ids:
        await call.answer("❌ Admin only!", show_alert=True)
        return
    
    text = """
👨‍💼 <b>ADMIN PANEL</b>

Select an action:
"""
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Bot Stats", callback_data="admin_stats")],
        [InlineKeyboardButton(text="👥 Users", callback_data="admin_users")],
        [InlineKeyboardButton(text="🚫 Ban User", callback_data="admin_ban")],
        [InlineKeyboardButton(text="📢 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await call.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(call: types.CallbackQuery):
    text = f"""
📊 <b>BOT STATISTICS</b>

👥 Total Users: {len(active_users)}
📁 Total Files: {sum(len(files) for files in user_files.values())}
🚫 Banned Users: {len(banned_users)}
💎 Premium Users: {sum(1 for uid in user_subscriptions if user_subscriptions[uid]['expiry'] > datetime.now())}

━━━━━━━━━━━━━━━━━━━━
📈 <b>USAGE:</b>
📤 Total Uploads: {bot_stats.get('total_uploads', 0)}
📥 Total Downloads: {bot_stats.get('total_downloads', 0)}
▶️ Total Runs: {bot_stats.get('total_runs', 0)}
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👨‍💼 Admin Panel", callback_data="admin_panel")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await call.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "admin_users")
async def admin_users(call: types.CallbackQuery):
    text = f"""
👥 <b>USER MANAGEMENT</b>

📊 Active Users: {len(active_users)}
🚫 Banned Users: {len(banned_users)}
👑 Admins: {len(admin_ids)}

Use /ban [USER_ID] to ban users
Use /unban [USER_ID] to unban users
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👨‍💼 Admin Panel", callback_data="admin_panel")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await call.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "admin_ban")
async def admin_ban_start(call: types.CallbackQuery):
    text = """
🚫 <b>BAN USER</b>

Use the command:
/ban [USER_ID] [reason]

Example:
/ban 123456789 Spamming
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👨‍💼 Admin Panel", callback_data="admin_panel")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await call.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(call: types.CallbackQuery):
    text = """
📢 <b>BROADCAST MESSAGE</b>

Use the command:
/broadcast Your message here

Example:
/broadcast System maintenance at 2 PM!
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👨‍💼 Admin Panel", callback_data="admin_panel")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await call.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await call.answer()

@dp.message(Command("ban"))
async def cmd_ban_user(message: types.Message):
    if message.from_user.id not in admin_ids:
        await message.answer("❌ Permission denied!")
        return
    
    try:
        args = message.text.split(maxsplit=2)
        if len(args) < 2:
            await message.answer("Usage: /ban USER_ID [reason]")
            return
        
        ban_user_id = int(args[1])
        reason = args[2] if len(args) > 2 else "No reason provided"
        
        banned_users.add(ban_user_id)
        
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('INSERT INTO banned_users (user_id, banned_date, reason) VALUES (%s, %s, %s)',
                 (ban_user_id, datetime.now().isoformat(), reason))
        conn.commit()
        conn.close()
        
        await message.answer(f"✅ User <code>{ban_user_id}</code> has been banned!\n📝 Reason: {reason}", parse_mode="HTML")
        
    except ValueError:
        await message.answer("❌ Invalid USER_ID!")
    except Exception as e:
        logger.error(f"Error banning user: {e}")
        await message.answer(f"❌ Error: {str(e)}")

@dp.message(Command("unban"))
async def cmd_unban_user(message: types.Message):
    if message.from_user.id not in admin_ids:
        await message.answer("❌ Permission denied!")
        return
    
    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("Usage: /unban USER_ID")
            return
        
        unban_user_id = int(args[1])
        
        if unban_user_id not in banned_users:
            await message.answer(f"❌ User {unban_user_id} is not banned!")
            return
        
        banned_users.remove(unban_user_id)
        
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('DELETE FROM banned_users WHERE user_id = %s', (unban_user_id,))
        conn.commit()
        conn.close()
        
        await message.answer(f"✅ User <code>{unban_user_id}</code> has been unbanned!", parse_mode="HTML")
        
    except ValueError:
        await message.answer("❌ Invalid USER_ID!")
    except Exception as e:
        logger.error(f"Error unbanning user: {e}")
        await message.answer(f"❌ Error: {str(e)}")

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if message.from_user.id not in admin_ids:
        await message.answer("❌ Permission denied!")
        return
    
    try:
        broadcast_text = message.text.replace("/broadcast", "", 1).strip()
        
        if not broadcast_text:
            await message.answer("Usage: /broadcast Your message here")
            return
        
        sent_count = 0
        failed_count = 0
        
        status_msg = await message.answer(f"📢 Broadcasting to {len(active_users)} users...")
        
        for user_id in active_users:
            if user_id in banned_users:
                continue
            
            try:
                await bot.send_message(user_id, f"📢 <b>Announcement:</b>\n\n{broadcast_text}", parse_mode="HTML")
                sent_count += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"Failed to send to {user_id}: {e}")
                failed_count += 1
        
        await status_msg.edit_text(
            f"✅ <b>Broadcast Complete!</b>\n\n"
            f"✅ Sent: {sent_count}\n"
            f"❌ Failed: {failed_count}",
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error broadcasting: {e}")
        await message.answer(f"❌ Error: {str(e)}")

@dp.message(Command("search"))
async def cmd_search_files(message: types.Message):
    user_id = message.from_user.id
    
    try:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            await message.answer("Usage: /search filename")
            return
        
        search_term = args[1].lower()
        user_file_list = user_files.get(user_id, [])
        
        matches = [f for f in user_file_list if search_term in f[0].lower()]
        
        if not matches:
            await message.answer(f"🔍 No files found matching '<code>{search_term}</code>'", parse_mode="HTML")
            return
        
        text = f"🔍 <b>Search Results ({len(matches)}):</b>\n\n"
        
        for file_name, file_type in matches:
            icon = "🐍" if file_type == "py" else "🟨" if file_type == "js" else "📦"
            text += f"{icon} <code>{file_name}</code>\n"
        
        await message.answer(text, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Search error: {e}")
        await message.answer(f"❌ Error: {str(e)}")

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    text = """
╔═══════════════════════╗
    ℹ️ <b>HELP & INFO</b> ℹ️
╚═══════════════════════╝

<b>🎯 HOW TO USE:</b>

1️⃣ <b>Upload Files:</b>
   • Click 'Upload File'
   • Send your .py, .js, or .zip file
   • File will be saved automatically

2️⃣ <b>Run Scripts:</b>
   • Go to 'My Files'
   • Click 'Run' on any file
   • Monitor script execution

3️⃣ <b>Manage Files:</b>
   • View all files in 'My Files'
   • Add to favorites with ⭐
   • Delete unwanted files

4️⃣ <b>Search:</b>
   • Use /search [filename]
   • Quick file lookup

━━━━━━━━━━━━━━━━━━━━
<b>💡 COMMANDS:</b>

/start - Start the bot
/help - Show this help
/search - Search files
/stats - Your statistics
/premium - Premium info

<b>Need help? Contact owner! 💬</b>
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Features", callback_data="all_features")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await message.answer(text, reply_markup=back_keyboard, parse_mode="HTML")

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    user_id = message.from_user.id
    user_file_count = len(user_files.get(user_id, []))
    user_fav_count = len(user_favorites.get(user_id, []))
    is_premium = user_id in user_subscriptions and user_subscriptions[user_id]['expiry'] > datetime.now()
    
    text = f"""
╔═══════════════════════╗
    📊 <b>YOUR STATISTICS</b> 📊
╚═══════════════════════╝

<b>👤 USER INFO:</b>

🆔 User ID: <code>{user_id}</code>
👤 Name: {message.from_user.full_name}
📦 Files Uploaded: {user_file_count}/{get_user_file_limit(user_id)}
⭐ Favorites: {user_fav_count}
💎 Account: {'Premium ✨' if is_premium else 'Free 🆓'}
🚀 Running: {sum(1 for k in bot_scripts if k.startswith(f"{user_id}_"))}

━━━━━━━━━━━━━━━━━━━━
📈 <b>USAGE:</b>

📤 Uploads: {bot_stats.get('total_uploads', 0)}
📥 Downloads: {bot_stats.get('total_downloads', 0)}
▶️ Script Runs: {bot_stats.get('total_runs', 0)}

{'✅ Bot Status: Active' if not bot_locked else '🔒 Bot: Maintenance'}
"""
    
    if user_id in admin_ids:
        text += f"\n━━━━━━━━━━━━━━━━━━━━\n👑 <b>ADMIN STATS:</b>\n"
        text += f"👥 Total Users: {len(active_users)}\n"
        text += f"📁 Total Files: {sum(len(files) for files in user_files.values())}\n"
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await message.answer(text, reply_markup=back_keyboard, parse_mode="HTML")

async def web_server():
    app = web.Application()
    
    async def handle(request):
        return web.Response(text="🚀 Advanced File Host Bot - Powered by Aiogram!")
    
    app.router.add_get('/', handle)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.getenv('PORT', 5000)))
    await site.start()
    logger.info(f"🌐 Web server started on port {os.getenv('PORT', 5000)}")

async def main():
    logger.info("🚀 Starting Advanced File Host Bot...")
    
    asyncio.create_task(web_server())
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
