# -*- coding: utf-8 -*-
"""
Telegram TTS & Voice Cloning Bot
Powered by VieNeu-TTS v3 Turbo (ONNX CPU)
"""

import os
import sys
import io
import time
import json
import sqlite3
import logging
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List

# Đảm bảo UTF-8 cho Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Thêm thư mục src vào sys.path
BASE_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(BASE_DIR / "src"))

import requests
import soundfile as sf
import numpy as np

# Cấu hình logging
logging.basicConfig(
    format="%(asctime)s - [%(levelname)s] - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("TTSBot")

# Tự động nạp biến môi trường từ file .env (nếu có)
env_file = BASE_DIR / ".env"
if env_file.exists():
    try:
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
    except Exception as e:
        logger.warning(f"Lỗi đọc file .env: {e}")

# --- HẰNG SỐ & TOKEN ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
DB_PATH = BASE_DIR / "bot_users.db"
CLONES_DIR = BASE_DIR / "user_clones"
CLONES_DIR.mkdir(parents=True, exist_ok=True)
TEMP_AUDIO_DIR = BASE_DIR / "temp_audio"
TEMP_AUDIO_DIR.mkdir(parents=True, exist_ok=True)

def start_health_server():
    """Khởi chạy mini web server phục vụ health check cho Hugging Face Spaces / Koyeb / Render 24/7."""
    from http.server import HTTPServer, BaseHTTPRequestHandler
    port = int(os.environ.get("PORT", "7860"))

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            html = (
                "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Telegram TTS Bot</title></head>"
                "<body style='font-family:sans-serif;text-align:center;padding-top:60px;background:#0f172a;color:#f8fafc;'>"
                "<h1>🤖 Telegram VieNeu TTS Bot 24/7</h1>"
                "<p style='color:#38bdf8;font-size:1.2rem;'>Trạng thái: 🟢 Hoạt động ổn định (Running)</p>"
                "<p style='color:#94a3b8;'>Powered by VieNeu-TTS v3 Turbo ONNX</p>"
                "</body></html>"
            )
            self.wfile.write(html.encode("utf-8"))

        def log_message(self, format, *args):
            pass

    try:
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        logger.info(f"🌐 Health Check Web Server đã chạy tại port {port} (hỗ trợ Hugging Face / Koyeb 24/7)")
    except Exception as e:
        logger.warning(f"Không thể khởi động Web Server tại port {port}: {e}")


# Danh sách 20 giọng đọc tiếng Việt của v3 Turbo
AVAILABLE_VOICES = [
    "Minh Đức", "Phạm Tuyên", "Thái Sơn", "Xuân Vĩnh", "Thanh Bình",
    "Trúc Ly", "Ngọc Linh", "Đoan Trang", "Mai Anh", "Thục Đoan",
    "Minh Triết", "Thùy Dung", "Quang Sơn", "Ngọc Trân", "Mỹ Duyên",
    "Quỳnh Anh", "Đức Trí", "Kim Thanh", "Ngọc Huyền", "Adam"
]
DEFAULT_VOICE = "Minh Đức"

# Khóa xử lý đồng thời để tránh xung đột ONNX session
tts_lock = threading.Lock()
tts_engine = None

# ─────────────────────────────────────────────────────────────
# 1. DATABASE SQLITE QUẢN LÝ NGƯỜI DÙNG
# ─────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            current_voice TEXT DEFAULT 'Minh Đức',
            use_clone INTEGER DEFAULT 0,
            clone_audio_path TEXT DEFAULT NULL,
            total_requests INTEGER DEFAULT 0,
            created_at TEXT,
            last_active TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            text_snippet TEXT,
            voice_mode TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()
    logger.info("✅ Database SQLite đã sẵn sàng.")

def get_or_create_user(user_id: int, username: str = "", full_name: str = "") -> Dict[str, Any]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row is None:
        cur.execute("""
            INSERT INTO users (user_id, username, full_name, current_voice, use_clone, clone_audio_path, total_requests, created_at, last_active)
            VALUES (?, ?, ?, ?, 0, NULL, 0, ?, ?)
        """, (user_id, username or "", full_name or "", DEFAULT_VOICE, now, now))
        conn.commit()
        cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
    else:
        cur.execute("UPDATE users SET username = ?, full_name = ?, last_active = ? WHERE user_id = ?",
                    (username or row["username"], full_name or row["full_name"], now, user_id))
        conn.commit()
    
    user_data = dict(row)
    conn.close()
    return user_data

def update_user_voice(user_id: int, voice_name: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET current_voice = ?, use_clone = 0, last_active = ? WHERE user_id = ?",
                (voice_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id))
    conn.commit()
    conn.close()

def update_user_clone_voice(user_id: int, audio_path: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET clone_audio_path = ?, use_clone = 1, last_active = ? WHERE user_id = ?",
                (audio_path, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id))
    conn.commit()
    conn.close()

def set_user_clone_mode(user_id: int, use_clone: bool):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET use_clone = ?, last_active = ? WHERE user_id = ?",
                (1 if use_clone else 0, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id))
    conn.commit()
    conn.close()

def record_history(user_id: int, text: str, voice_mode: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    snippet = text[:100] + ("..." if len(text) > 100 else "")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("INSERT INTO history (user_id, text_snippet, voice_mode, created_at) VALUES (?, ?, ?, ?)",
                (user_id, snippet, voice_mode, now))
    cur.execute("UPDATE users SET total_requests = total_requests + 1, last_active = ? WHERE user_id = ?",
                (now, user_id))
    conn.commit()
    conn.close()

# ─────────────────────────────────────────────────────────────
# 2. KHỞI TẠO MODEL TTS VIENEU V3 TURBO
# ─────────────────────────────────────────────────────────────

def get_tts_engine():
    global tts_engine
    if tts_engine is None:
        with tts_lock:
            if tts_engine is None:
                logger.info("⏳ Đang nạp mô hình VieNeu-TTS v3 Turbo ONNX...")
                from vieneu import Vieneu
                tts_engine = Vieneu(mode="v3turbo")
                logger.info("✅ VieNeu-TTS v3 Turbo nạp thành công!")
    return tts_engine

def synthesize_text(text: str, voice_name: str = DEFAULT_VOICE, clone_path: Optional[str] = None) -> str:
    """Tổng hợp văn bản thành file WAV và trả về đường dẫn file."""
    engine = get_tts_engine()
    out_filename = TEMP_AUDIO_DIR / f"tts_{int(time.time() * 1000)}_{os.urandom(4).hex()}.wav"

    with tts_lock:
        if clone_path and os.path.exists(clone_path):
            logger.info(f"🎙️ [Voice Cloning] Sinh âm thanh với clone_audio: {clone_path}")
            wav = engine.infer(text, ref_audio=clone_path)
        else:
            logger.info(f"🗣️ [Preset Voice] Sinh âm thanh với giọng: {voice_name}")
            wav = engine.infer(text, voice=voice_name)
        
        engine.save(wav, str(out_filename))
    
    return str(out_filename)

# ─────────────────────────────────────────────────────────────
# 3. TELEGRAM API CLIENT GIAO TIẾP TRỰC TIẾP
# ─────────────────────────────────────────────────────────────

class TelegramBot:
    def __init__(self, token: str):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()

    def request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        url = f"{self.base_url}/{endpoint}"
        try:
            resp = self.session.request(method, url, timeout=kwargs.pop("timeout", 45), **kwargs)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Lỗi gọi API {endpoint}: {e}")
            return {"ok": False, "description": str(e)}

    def get_me(self) -> Dict[str, Any]:
        return self.request("GET", "getMe")

    def get_updates(self, offset: Optional[int] = None, timeout: int = 30) -> List[Dict[str, Any]]:
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        res = self.request("GET", "getUpdates", params=params, timeout=timeout + 15)
        return res.get("result", []) if res.get("ok") else []

    def send_message(self, chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None, parse_mode: str = "Markdown") -> Dict[str, Any]:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        res = self.request("POST", "sendMessage", json=payload)
        # Fallback nếu Markdown lỗi định dạng
        if not res.get("ok") and parse_mode:
            payload.pop("parse_mode", None)
            return self.request("POST", "sendMessage", json=payload)
        return res

    def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None, parse_mode: str = "Markdown") -> Dict[str, Any]:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        res = self.request("POST", "editMessageText", json=payload)
        if not res.get("ok") and parse_mode:
            payload.pop("parse_mode", None)
            return self.request("POST", "editMessageText", json=payload)
        return res

    def send_chat_action(self, chat_id: int, action: str = "record_voice"):
        return self.request("POST", "sendChatAction", json={"chat_id": chat_id, "action": action})

    def send_voice(self, chat_id: int, voice_path: str, caption: Optional[str] = None) -> Dict[str, Any]:
        url = f"{self.base_url}/sendVoice"
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        with open(voice_path, "rb") as f:
            files = {"voice": (os.path.basename(voice_path), f, "audio/ogg")}
            try:
                resp = self.session.post(url, data=data, files=files, timeout=60)
                return resp.json()
            except Exception as e:
                logger.error(f"Lỗi gửi voice: {e}")
                return {"ok": False, "description": str(e)}

    def send_audio(self, chat_id: int, audio_path: str, caption: Optional[str] = None, title: str = "VieNeu Voice") -> Dict[str, Any]:
        url = f"{self.base_url}/sendAudio"
        data = {"chat_id": chat_id, "title": title, "performer": "VieNeu TTS"}
        if caption:
            data["caption"] = caption
        with open(audio_path, "rb") as f:
            files = {"audio": (os.path.basename(audio_path), f, "audio/wav")}
            try:
                resp = self.session.post(url, data=data, files=files, timeout=60)
                return resp.json()
            except Exception as e:
                logger.error(f"Lỗi gửi audio: {e}")
                return {"ok": False, "description": str(e)}

    def get_file(self, file_id: str) -> Optional[Dict[str, Any]]:
        res = self.request("GET", "getFile", params={"file_id": file_id})
        return res.get("result") if res.get("ok") else None

    def download_file(self, file_path: str, destination: Path) -> bool:
        download_url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        try:
            resp = self.session.get(download_url, stream=True, timeout=60)
            resp.raise_for_status()
            with open(destination, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True
        except Exception as e:
            logger.error(f"Lỗi tải file {file_path}: {e}")
            return False

    def answer_callback_query(self, callback_query_id: str, text: Optional[str] = None, show_alert: bool = False):
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
            payload["show_alert"] = show_alert
        return self.request("POST", "answerCallbackQuery", json=payload)

# ─────────────────────────────────────────────────────────────
# 4. GIAO DIỆN NÚT BẤM (INLINE KEYBOARDS)
# ─────────────────────────────────────────────────────────────

def make_voice_keyboard(current_voice: str, page: int = 0) -> Dict[str, Any]:
    """Tạo bàn phím chọn giọng đọc, 6 giọng mỗi trang."""
    PAGE_SIZE = 6
    total_pages = (len(AVAILABLE_VOICES) + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, total_pages - 1))
    
    start_idx = page * PAGE_SIZE
    end_idx = min(start_idx + PAGE_SIZE, len(AVAILABLE_VOICES))
    page_voices = AVAILABLE_VOICES[start_idx:end_idx]

    keyboard = []
    # Xếp 2 nút trên 1 hàng
    row = []
    for v in page_voices:
        label = f"✅ {v}" if v == current_voice else f"🗣️ {v}"
        row.append({"text": label, "callback_data": f"set_voice:{v}"})
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    # Hàng điều hướng phân trang
    nav_row = []
    if page > 0:
        nav_row.append({"text": "⬅️ Trước", "callback_data": f"voice_page:{page - 1}"})
    nav_row.append({"text": f"📄 {page + 1}/{total_pages}", "callback_data": "noop"})
    if page < total_pages - 1:
        nav_row.append({"text": "Tiếp ➡️", "callback_data": f"voice_page:{page + 1}"})
    keyboard.append(nav_row)

    # Nút về Menu chính
    keyboard.append([{"text": "🔙 Quay lại Menu", "callback_data": "main_menu"}])
    return {"inline_keyboard": keyboard}

def make_main_keyboard(user_data: Dict[str, Any]) -> Dict[str, Any]:
    """Tạo menu chính với các nút điều khiển chức năng."""
    use_clone = bool(user_data.get("use_clone"))
    has_clone = bool(user_data.get("clone_audio_path") and os.path.exists(user_data["clone_audio_path"]))
    cur_voice = user_data.get("current_voice", DEFAULT_VOICE)

    keyboard = [
        [
            {"text": f"🎙️ Giọng: {cur_voice}", "callback_data": "voice_page:0"},
            {"text": "🧬 Chế độ Clone: " + ("BẬT ✅" if use_clone else "TẮT ❌"), "callback_data": "toggle_clone"}
        ],
        [
            {"text": "📁 Upload mẫu giọng mới", "callback_data": "guide_clone"},
            {"text": "👤 Thông tin của tôi", "callback_data": "user_info"}
        ],
        [
            {"text": "📖 Hướng dẫn sử dụng", "callback_data": "guide_full"}
        ]
    ]
    return {"inline_keyboard": keyboard}

# ─────────────────────────────────────────────────────────────
# 5. XỬ LÝ SỰ KIỆN VÀ TIN NHẮN
# ─────────────────────────────────────────────────────────────

def handle_start_command(bot: TelegramBot, chat_id: int, user_data: Dict[str, Any]):
    full_name = user_data.get("full_name", "Bạn")
    cur_voice = user_data.get("current_voice", DEFAULT_VOICE)
    use_clone = bool(user_data.get("use_clone"))
    has_clone = bool(user_data.get("clone_audio_path") and os.path.exists(user_data.get("clone_audio_path", "")))

    clone_status = "Đang kích hoạt (sử dụng giọng clone của bạn)" if use_clone else ("Đã có mẫu lưu trữ" if has_clone else "Chưa có mẫu giọng")

    msg = (
        f"👋 *Xin chào {full_name}!*\n\n"
        f"🤖 Chào mừng bạn đến với *Bot Chuyển Văn Bản Sang Giọng Nói (VieNeu TTS)*!\n\n"
        f"⚙️ *Cài đặt hiện tại của bạn:*\n"
        f"• 🗣️ *Giọng đọc mặc định:* `{cur_voice}`\n"
        f"• 🧬 *Trạng thái Clone:* `{clone_status}`\n"
        f"• 📊 *Số lượt đã tạo:* `{user_data.get('total_requests', 0)}`\n\n"
        f"✨ *Bạn có thể làm gì?*\n"
        f"1️⃣ *Gửi văn bản:* Chỉ cần nhắn tin chữ bất kỳ, bot sẽ đọc và gửi lại file âm thanh.\n"
        f"2️⃣ *Gửi file tài liệu:* Up file `.txt` hoặc `.docx`, bot sẽ tự động đọc cả file.\n"
        f"3️⃣ *Clone giọng nói:* Giữ mic ghi âm (voice) hoặc gửi file âm thanh (`.mp3`, `.wav`) 5-10 giây để bot clone giọng của bạn!"
    )
    bot.send_message(chat_id, msg, reply_markup=make_main_keyboard(user_data))

def handle_voice_message(bot: TelegramBot, chat_id: int, user_id: int, voice_obj: Dict[str, Any], user_data: Dict[str, Any]):
    """Xử lý khi người dùng gửi tin nhắn thoại hoặc file âm thanh để clone."""
    file_id = voice_obj.get("file_id")
    if not file_id:
        bot.send_message(chat_id, "❌ Không thể đọc file âm thanh này.")
        return

    bot.send_message(chat_id, "⏳ *Đang tải mẫu âm thanh và phân tích đặc trưng giọng nói của bạn...* Vui lòng chờ vài giây...")
    bot.send_chat_action(chat_id, "record_voice")

    file_info = bot.get_file(file_id)
    if not file_info or not file_info.get("file_path"):
        bot.send_message(chat_id, "❌ Lỗi khi lấy thông tin file từ Telegram.")
        return

    temp_input = TEMP_AUDIO_DIR / f"raw_{user_id}_{int(time.time())}.tmp"
    saved_clone_path = CLONES_DIR / f"user_{user_id}_clone.wav"

    if not bot.download_file(file_info["file_path"], temp_input):
        bot.send_message(chat_id, "❌ Tải file âm thanh thất bại.")
        return

    try:
        # Đọc dữ liệu âm thanh và chuẩn hóa thành WAV mono float32
        wav_data, sr = sf.read(str(temp_input), dtype="float32", always_2d=False)
        if getattr(wav_data, "ndim", 1) > 1:
            wav_data = wav_data.mean(axis=1)
        
        # Lưu thành file wav chuẩn
        sf.write(str(saved_clone_path), wav_data, sr)
        update_user_clone_voice(user_id, str(saved_clone_path))

        user_data = get_or_create_user(user_id)
        bot.send_message(
            chat_id,
            "🎉 *Clone giọng nói thành công!*\n\n"
            "✅ Mẫu giọng của bạn đã được lưu vào hồ sơ cá nhân.\n"
            "👉 *Chế độ Clone đã được TỰ ĐỘNG BẬT.*\n"
            "Từ bây giờ, khi bạn gửi văn bản hoặc file, bot sẽ đọc bằng chính giọng nói này của bạn!\n\n"
            "_(Nhấn nút bên dưới nếu bạn muốn quay lại dùng giọng mẫu của hệ thống)_",
            reply_markup=make_main_keyboard(user_data)
        )
    except Exception as e:
        logger.error(f"Lỗi trích xuất clone audio: {e}")
        bot.send_message(chat_id, f"❌ Có lỗi khi xử lý âm thanh: {e}")
    finally:
        if temp_input.exists():
            try:
                temp_input.unlink()
            except Exception:
                pass

def handle_document_message(bot: TelegramBot, chat_id: int, user_id: int, doc_obj: Dict[str, Any], user_data: Dict[str, Any]):
    """Xử lý khi người dùng upload file văn bản (.txt, .docx)."""
    file_name = doc_obj.get("file_name", "document.txt").lower()
    file_id = doc_obj.get("file_id")

    if not (file_name.endswith(".txt") or file_name.endswith(".docx")):
        bot.send_message(chat_id, "⚠️ Bot hiện hỗ trợ file văn bản `.txt` hoặc `.docx`. Vui lòng gửi định dạng này nhé!")
        return

    bot.send_message(chat_id, f"📄 Đã nhận file `{file_name}`. Đang trích xuất nội dung văn bản...")
    file_info = bot.get_file(file_id)
    if not file_info or not file_info.get("file_path"):
        bot.send_message(chat_id, "❌ Lỗi khi lấy file từ Telegram.")
        return

    temp_doc = TEMP_AUDIO_DIR / f"doc_{user_id}_{int(time.time())}_{file_name}"
    if not bot.download_file(file_info["file_path"], temp_doc):
        bot.send_message(chat_id, "❌ Tải file văn bản thất bại.")
        return

    extracted_text = ""
    try:
        if file_name.endswith(".txt"):
            # Thử giải mã utf-8, utf-16, cp1258
            content = temp_doc.read_bytes()
            for enc in ["utf-8", "utf-16", "utf-8-sig", "cp1258", "latin-1"]:
                try:
                    extracted_text = content.decode(enc).strip()
                    break
                except Exception:
                    continue
        elif file_name.endswith(".docx"):
            import docx
            doc = docx.Document(str(temp_doc))
            paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
            extracted_text = "\n".join(paragraphs)
    except Exception as e:
        logger.error(f"Lỗi đọc file văn bản: {e}")
        bot.send_message(chat_id, f"❌ Không thể đọc nội dung file: {e}")
        return
    finally:
        if temp_doc.exists():
            try:
                temp_doc.unlink()
            except Exception:
                pass

    if not extracted_text:
        bot.send_message(chat_id, "⚠️ File văn bản của bạn không có nội dung chữ nào!")
        return

    # Giới hạn độ dài tối đa cho 1 file văn bản để đảm bảo tốc độ
    MAX_FILE_CHARS = 4000
    truncated = False
    if len(extracted_text) > MAX_FILE_CHARS:
        extracted_text = extracted_text[:MAX_FILE_CHARS]
        truncated = True

    info_note = f" (Đã đọc {len(extracted_text)} ký tự" + (", cắt ngắn phần vượt 4000 ký tự)" if truncated else ")")
    bot.send_message(chat_id, f"📖 Đã đọc xong văn bản{info_note}! Bắt đầu chuyển thành giọng nói...")
    handle_text_synthesis(bot, chat_id, user_id, extracted_text, user_data)

def handle_text_synthesis(bot: TelegramBot, chat_id: int, user_id: int, text: str, user_data: Dict[str, Any]):
    """Tổng hợp văn bản thành âm thanh và gửi về cho người dùng."""
    text = text.strip()
    if not text:
        return

    use_clone = bool(user_data.get("use_clone"))
    clone_path = user_data.get("clone_audio_path") if use_clone else None
    cur_voice = user_data.get("current_voice", DEFAULT_VOICE)

    mode_label = "🧬 Giọng Clone cá nhân" if (use_clone and clone_path and os.path.exists(clone_path)) else f"🗣️ Giọng {cur_voice}"

    bot.send_chat_action(chat_id, "record_voice")
    status_msg = bot.send_message(chat_id, f"⏳ *Đang tạo giọng nói ({mode_label})...* Vui lòng chờ...")
    status_msg_id = status_msg.get("result", {}).get("message_id")

    try:
        out_wav = synthesize_text(text, voice_name=cur_voice, clone_path=clone_path)
        
        # Ghi lại lịch sử vào database
        record_history(user_id, text, mode_label)

        caption = f"🎙️ {mode_label}\n📝 Văn bản: {text[:80]}{'...' if len(text) > 80 else ''}"
        
        # Gửi file audio
        bot.send_chat_action(chat_id, "upload_voice")
        res = bot.send_voice(chat_id, out_wav, caption=caption)
        if not res.get("ok"):
            bot.send_audio(chat_id, out_wav, caption=caption, title=f"VieNeu_{cur_voice}")

        # Xóa tin nhắn trạng thái chờ
        if status_msg_id:
            bot.request("POST", "deleteMessage", json={"chat_id": chat_id, "message_id": status_msg_id})

    except Exception as e:
        logger.error(f"Lỗi khi tổng hợp giọng nói: {e}", exc_info=True)
        bot.send_message(chat_id, f"❌ Quá trình tạo giọng nói gặp sự cố: {e}")
    finally:
        # Xóa file tạm
        if 'out_wav' in locals() and os.path.exists(out_wav):
            try:
                os.remove(out_wav)
            except Exception:
                pass

def handle_callback_query(bot: TelegramBot, cb: Dict[str, Any]):
    """Xử lý khi người dùng bấm các nút Inline Keyboard."""
    cb_id = cb["id"]
    from_user = cb["from"]
    user_id = from_user["id"]
    data = cb.get("data", "")
    message = cb.get("message")
    chat_id = message["chat"]["id"] if message else user_id
    message_id = message["message_id"] if message else None

    user_data = get_or_create_user(user_id, from_user.get("username", ""), from_user.get("first_name", ""))

    if data == "noop":
        bot.answer_callback_query(cb_id)
        return

    if data.startswith("voice_page:"):
        page = int(data.split(":")[1])
        cur_voice = user_data.get("current_voice", DEFAULT_VOICE)
        text = "🗣️ *Chọn một giọng đọc tiếng Việt bên dưới:*\n(Có tổng cộng 20 giọng nam và nữ)"
        kb = make_voice_keyboard(cur_voice, page=page)
        if message_id:
            bot.edit_message_text(chat_id, message_id, text, reply_markup=kb)
        else:
            bot.send_message(chat_id, text, reply_markup=kb)
        bot.answer_callback_query(cb_id)
        return

    if data.startswith("set_voice:"):
        chosen_voice = data.split(":", 1)[1]
        update_user_voice(user_id, chosen_voice)
        bot.answer_callback_query(cb_id, f"✅ Đã chọn giọng: {chosen_voice}")
        user_data = get_or_create_user(user_id)
        
        # Cập nhật lại bàn phím chọn giọng
        kb = make_voice_keyboard(chosen_voice, page=0)
        text = f"✅ *Đã đổi thành công sang giọng: `{chosen_voice}`!*\n\nBây giờ bạn chỉ cần gửi tin nhắn văn bản, bot sẽ đọc bằng giọng này."
        if message_id:
            bot.edit_message_text(chat_id, message_id, text, reply_markup=kb)
        return

    if data == "toggle_clone":
        has_clone = bool(user_data.get("clone_audio_path") and os.path.exists(user_data["clone_audio_path"]))
        if not has_clone:
            bot.answer_callback_query(cb_id, "⚠️ Bạn chưa tải lên mẫu giọng nào! Hãy gửi 1 tin nhắn thoại để clone.", show_alert=True)
            return

        new_state = not bool(user_data.get("use_clone"))
        set_user_clone_mode(user_id, new_state)
        user_data = get_or_create_user(user_id)
        
        state_str = "BẬT (đang dùng giọng clone)" if new_state else "TẮT (quay về giọng hệ thống)"
        bot.answer_callback_query(cb_id, f"Đã {state_str} chế độ Clone!")
        
        msg = f"⚙️ *Chế độ Clone giọng nói hiện tại:* `{state_str}`"
        if message_id:
            bot.edit_message_text(chat_id, message_id, msg, reply_markup=make_main_keyboard(user_data))
        return

    if data == "main_menu":
        full_name = user_data.get("full_name", "Bạn")
        cur_voice = user_data.get("current_voice", DEFAULT_VOICE)
        use_clone = bool(user_data.get("use_clone"))
        clone_status = "BẬT ✅" if use_clone else "TẮT ❌"

        msg = (
            f"🏠 *MENU CHÍNH*\n\n"
            f"• 🗣️ *Giọng đang dùng:* `{cur_voice}`\n"
            f"• 🧬 *Clone giọng:* `{clone_status}`\n"
            f"• 📊 *Số lượt đã tạo:* `{user_data.get('total_requests', 0)}`\n\n"
            f"👇 Chọn một chức năng bên dưới hoặc gửi tin nhắn chữ để nghe đọc ngay:"
        )
        if message_id:
            bot.edit_message_text(chat_id, message_id, msg, reply_markup=make_main_keyboard(user_data))
        bot.answer_callback_query(cb_id)
        return

    if data == "user_info":
        cur_voice = user_data.get("current_voice", DEFAULT_VOICE)
        use_clone = bool(user_data.get("use_clone"))
        has_clone = bool(user_data.get("clone_audio_path") and os.path.exists(user_data.get("clone_audio_path", "")))

        info_msg = (
            f"👤 *THÔNG TIN TÀI KHOẢN*\n\n"
            f"• 🆔 *User ID:* `{user_id}`\n"
            f"• 👤 *Họ tên:* {user_data.get('full_name')}\n"
            f"• 🗣️ *Giọng đọc chọn trước:* `{cur_voice}`\n"
            f"• 🧬 *Mẫu Clone:* {'Đã lưu mẫu âm thanh' if has_clone else 'Chưa có'}\n"
            f"• ⚡ *Đang dùng Clone:* {'CÓ' if use_clone else 'KHÔNG'}\n"
            f"• 📈 *Tổng số lần tạo audio:* `{user_data.get('total_requests', 0)}`\n"
            f"• 📅 *Ngày bắt đầu:* `{user_data.get('created_at')}`\n"
            f"• 🕒 *Hoạt động gần nhất:* `{user_data.get('last_active')}`"
        )
        kb = {"inline_keyboard": [[{"text": "🔙 Quay lại", "callback_data": "main_menu"}]]}
        if message_id:
            bot.edit_message_text(chat_id, message_id, info_msg, reply_markup=kb)
        bot.answer_callback_query(cb_id)
        return

    if data == "guide_clone":
        guide_msg = (
            "🧬 *HƯỚNG DẪN CLONE GIỌNG NÓI*\n\n"
            "1️⃣ Nhấn giữ nút Microphone trên Telegram và nói một đoạn từ *5 đến 15 giây* (nói rõ ràng, tự nhiên, hạn chế tiếng ồn).\n"
            "2️⃣ Hoặc gửi một file âm thanh (`.mp3`, `.wav`, `.m4a`) chứa giọng nói cần clone.\n"
            "3️⃣ Bot sẽ tự động trích xuất và lưu giọng đọc đó cho riêng tài khoản của bạn.\n"
            "4️⃣ Sau khi lưu xong, mọi văn bản bạn gửi sẽ được đọc bằng chính giọng nói đó!"
        )
        kb = {"inline_keyboard": [[{"text": "🔙 Quay lại", "callback_data": "main_menu"}]]}
        if message_id:
            bot.edit_message_text(chat_id, message_id, guide_msg, reply_markup=kb)
        bot.answer_callback_query(cb_id)
        return

    if data == "guide_full":
        guide_msg = (
            "📖 *HƯỚNG DẪN ĐẦY ĐỦ CÁC TÍNH NĂNG*\n\n"
            "1. 💬 *Đọc văn bản:* Gõ bất kỳ đoạn chữ nào gửi vào chat, bot sẽ chuyển thành giọng nói ngay.\n"
            "2. 📄 *Đọc file:* Gửi file `.txt` hoặc `.docx`, bot sẽ trích xuất toàn bộ văn bản và đọc ra file âm thanh hoàn chỉnh.\n"
            "3. 🗣️ *Đổi giọng đọc:* Bấm nút *Chọn Giọng* để chọn trong 20 giọng Việt khác nhau.\n"
            "4. 🧬 *Clone giọng:* Gửi tin nhắn thoại để đọc bằng giọng của bạn.\n"
            "5. 💾 *Lưu trữ:* Mọi cài đặt của bạn được lưu trữ vĩnh viễn trên máy chủ, khi quay lại không cần cài đặt lại."
        )
        kb = {"inline_keyboard": [[{"text": "🔙 Quay lại", "callback_data": "main_menu"}]]}
        if message_id:
            bot.edit_message_text(chat_id, message_id, guide_msg, reply_markup=kb)
        bot.answer_callback_query(cb_id)
        return

# ─────────────────────────────────────────────────────────────
# 6. VÒNG LẶP CHÍNH (LONG POLLING)
# ─────────────────────────────────────────────────────────────

def run_bot():
    logger.info("=" * 60)
    logger.info("      KHỞI ĐỘNG TELEGRAM TTS & VOICE CLONING BOT")
    logger.info("=" * 60)

    if not BOT_TOKEN:
        logger.error("❌ LỖI: Chưa tìm thấy BOT_TOKEN!")
        logger.error("👉 Hãy thêm BOT_TOKEN vào file .env hoặc biến môi trường (Secrets) để chạy.")
        return

    # Khởi chạy health check server cho các nền tảng đám mây 24/7
    start_health_server()

    # Khởi tạo DB
    init_db()

    # Kiểm tra model TTS trước
    logger.info("Đang kiểm tra mô hình VieNeu TTS...")
    get_tts_engine()

    bot = TelegramBot(BOT_TOKEN)
    me = bot.get_me()
    if not me.get("ok"):
        logger.error(f"❌ Không thể kết nối tới Bot Telegram! Chi tiết: {me}")
        return


    bot_info = me["result"]
    logger.info(f"✅ ĐÃ KẾT NỐI BOT THÀNH CÔNG:")
    logger.info(f"   • Tên bot:  {bot_info.get('first_name')}")
    logger.info(f"   • Username: @{bot_info.get('username')}")
    logger.info(f"   • ID:       {bot_info.get('id')}")
    logger.info("=" * 60)
    logger.info("👉 Bot đang chạy polling và sẵn sàng nhận tin nhắn từ người dùng!")
    logger.info("=" * 60)

    offset = None
    while True:
        try:
            updates = bot.get_updates(offset=offset, timeout=25)
            for update in updates:
                offset = update["update_id"] + 1

                # 1. Xử lý Callback Query (bấm nút)
                if "callback_query" in update:
                    logger.info(f"🔘 Nhận tương tác nút bấm: {update['callback_query'].get('data')}")
                    handle_callback_query(bot, update["callback_query"])
                    continue

                # 2. Xử lý Tin nhắn (Message)
                if "message" in update:
                    msg = update["message"]
                    chat_id = msg["chat"]["id"]
                    from_user = msg.get("from", {})
                    user_id = from_user.get("id")
                    if not user_id:
                        continue

                    user_data = get_or_create_user(
                        user_id,
                        from_user.get("username", ""),
                        from_user.get("first_name", "")
                    )

                    # Tin nhắn thoại (Voice)
                    if "voice" in msg:
                        logger.info(f"🎤 Nhận voice message từ user {user_id}")
                        handle_voice_message(bot, chat_id, user_id, msg["voice"], user_data)
                        continue

                    # File âm thanh (Audio)
                    if "audio" in msg:
                        logger.info(f"🎵 Nhận audio file từ user {user_id}")
                        handle_voice_message(bot, chat_id, user_id, msg["audio"], user_data)
                        continue

                    # File tài liệu (Document - .txt, .docx)
                    if "document" in msg:
                        doc_name = msg["document"].get("file_name", "")
                        logger.info(f"📄 Nhận document '{doc_name}' từ user {user_id}")
                        handle_document_message(bot, chat_id, user_id, msg["document"], user_data)
                        continue

                    # Tin nhắn văn bản (Text)
                    if "text" in msg:
                        text = msg["text"].strip()
                        logger.info(f"💬 Nhận text từ user {user_id}: '{text[:40]}'")
                        if text == "/start" or text == "/help":
                            handle_start_command(bot, chat_id, user_data)
                        elif text == "/voice" or text == "/giong":
                            kb = make_voice_keyboard(user_data.get("current_voice", DEFAULT_VOICE), page=0)
                            bot.send_message(chat_id, "🗣️ *Chọn giọng đọc tiếng Việt:*", reply_markup=kb)
                        elif text == "/myvoice":
                            handle_start_command(bot, chat_id, user_data)
                        else:
                            handle_text_synthesis(bot, chat_id, user_id, text, user_data)
                        continue

        except KeyboardInterrupt:
            logger.info("Đang dừng Bot...")
            break
        except Exception as e:
            logger.error(f"Lỗi trong vòng lặp chính: {e}")
            time.sleep(2)

if __name__ == "__main__":
    run_bot()
