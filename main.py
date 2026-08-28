import asyncio
import hashlib
import json
import time
import os
import base64
from collections import deque
from datetime import datetime
from typing import List, Dict, Set, Optional
from pathlib import Path
import shutil

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# ============================================
# КОНФИГУРАЦИЯ
# ============================================
MAX_MESSAGES = 100
RATE_LIMIT_SECONDS = 1
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.pdf', '.doc', '.docx', '.txt', '.zip', '.rar'}

# Директории
BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
AVATAR_DIR = BASE_DIR / "avatars"
DATA_DIR = BASE_DIR / "data"

# Создаём директории
for dir_path in [UPLOAD_DIR, AVATAR_DIR, DATA_DIR]:
    dir_path.mkdir(exist_ok=True)

# ============================================
# ХРАНЕНИЕ ДАННЫХ
# ============================================
class UserProfile:
    def __init__(self, nickname: str):
        self.nickname = nickname
        self.password_hash = None  # Будет установлен при создании
        self.avatar = None  # Имя файла аватара
        self.info = ""
        self.phone = ""
        self.email = ""
        self.created_at = time.time()
        self.last_seen = time.time()
    
    def to_dict(self):
        return {
            "nickname": self.nickname,
            "has_password": self.password_hash is not None,
            "avatar": self.avatar,
            "info": self.info,
            "phone": self.phone,
            "email": self.email,
            "created_at": self.created_at,
            "last_seen": self.last_seen
        }

class ChatState:
    def __init__(self):
        self.messages: deque = deque(maxlen=MAX_MESSAGES)
        self.active_connections: Dict[WebSocket, Dict] = {}
        self.nicknames: Set[str] = set()
        self.users: Dict[str, UserProfile] = {}  # nickname -> UserProfile
        self.file_counter = 0
        
        # Загружаем данные если есть
        self.load_data()
        
        # Добавляем приветственное сообщение
        if not self.messages:
            self.add_message("System", "🚀 Добро пожаловать в Global Chat!", is_system=True)

    def add_message(self, nickname: str, text: str, is_system: bool = False, file_info: Optional[Dict] = None):
        """Добавляет сообщение в историю"""
        message = {
            "id": f"msg_{int(time.time() * 1000)}_{len(self.messages)}",
            "nickname": nickname,
            "text": text,
            "timestamp": time.time(),
            "is_system": is_system,
            "file": file_info
        }
        self.messages.append(message)
        self.save_data()
        return message

    def get_messages(self) -> List[Dict]:
        return list(self.messages)

    def get_online_count(self) -> int:
        return len(self.active_connections)

    def get_online_users(self) -> List[str]:
        return [data["nickname"] for data in self.active_connections.values()]

    def is_rate_limited(self, websocket: WebSocket) -> bool:
        if websocket not in self.active_connections:
            return False
        last_time = self.active_connections[websocket].get("last_message_time", 0)
        current_time = time.time()
        if current_time - last_time < RATE_LIMIT_SECONDS:
            return True
        self.active_connections[websocket]["last_message_time"] = current_time
        return False

    def get_user_profile(self, nickname: str) -> Optional[UserProfile]:
        return self.users.get(nickname)

    def create_user(self, nickname: str, password: Optional[str] = None) -> UserProfile:
        profile = UserProfile(nickname)
        if password:
            profile.password_hash = hashlib.sha256(password.encode()).hexdigest()
        self.users[nickname] = profile
        self.save_data()
        return profile

    def verify_password(self, nickname: str, password: str) -> bool:
        profile = self.users.get(nickname)
        if not profile or not profile.password_hash:
            return False
        return profile.password_hash == hashlib.sha256(password.encode()).hexdigest()

    def update_profile(self, nickname: str, **kwargs):
        profile = self.users.get(nickname)
        if profile:
            for key, value in kwargs.items():
                if hasattr(profile, key):
                    setattr(profile, key, value)
            self.save_data()
            return True
        return False

    def save_data(self):
        """Сохраняет данные в JSON"""
        data = {
            "messages": list(self.messages),
            "users": {
                nick: {
                    "nickname": profile.nickname,
                    "password_hash": profile.password_hash,
                    "avatar": profile.avatar,
                    "info": profile.info,
                    "phone": profile.phone,
                    "email": profile.email,
                    "created_at": profile.created_at,
                    "last_seen": profile.last_seen
                }
                for nick, profile in self.users.items()
            },
            "file_counter": self.file_counter
        }
        try:
            with open(DATA_DIR / "chat_data.json", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving data: {e}")

    def load_data(self):
        """Загружает данные из JSON"""
        try:
            with open(DATA_DIR / "chat_data.json", "r", encoding="utf-8") as f:
                data = json.load(f)
            
            # Загружаем сообщения
            self.messages = deque(data.get("messages", []), maxlen=MAX_MESSAGES)
            
            # Загружаем пользователей
            for nick, user_data in data.get("users", {}).items():
                profile = UserProfile(nick)
                profile.password_hash = user_data.get("password_hash")
                profile.avatar = user_data.get("avatar")
                profile.info = user_data.get("info", "")
                profile.phone = user_data.get("phone", "")
                profile.email = user_data.get("email", "")
                profile.created_at = user_data.get("created_at", time.time())
                profile.last_seen = user_data.get("last_seen", time.time())
                self.users[nick] = profile
            
            self.file_counter = data.get("file_counter", 0)
            print(f"Loaded {len(self.messages)} messages and {len(self.users)} users")
        except FileNotFoundError:
            print("No saved data found, starting fresh")
        except Exception as e:
            print(f"Error loading data: {e}")

# Глобальное состояние
chat_state = ChatState()

# ============================================
# FASTAPI ПРИЛОЖЕНИЕ
# ============================================
app = FastAPI(title="Global Chat Pro", description="Мощный веб-мессенджер с файлами и профилями")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Монтируем статические файлы
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")
app.mount("/avatars", StaticFiles(directory=str(AVATAR_DIR)), name="avatars")

# ============================================
# API ЭНДПОИНТЫ
# ============================================
@app.get("/", response_class=HTMLResponse)
async def get_chat_page():
    return HTML_TEMPLATE

@app.post("/api/upload")
async def upload_file(
    file: UploadFile = File(...),
    nickname: str = Form(...)
):
    """Загрузка файла"""
    # Проверка размера
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 10MB)")
    
    # Проверка расширения
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="File type not allowed")
    
    # Генерируем уникальное имя
    chat_state.file_counter += 1
    safe_filename = f"file_{chat_state.file_counter}_{int(time.time())}_{file.filename}"
    file_path = UPLOAD_DIR / safe_filename
    
    # Сохраняем файл
    with open(file_path, "wb") as f:
        f.write(content)
    
    file_info = {
        "filename": file.filename,
        "saved_name": safe_filename,
        "size": len(content),
        "type": file.content_type,
        "extension": ext,
        "url": f"/uploads/{safe_filename}"
    }
    
    # Проверяем, является ли файл изображением
    if ext in {'.jpg', '.jpeg', '.png', '.gif', '.webp'}:
        file_info["is_image"] = True
    
    chat_state.save_data()
    return JSONResponse(file_info)

@app.post("/api/profile/update")
async def update_profile(
    nickname: str = Form(...),
    field: str = Form(...),
    value: str = Form(...)
):
    """Обновление профиля"""
    profile = chat_state.get_user_profile(nickname)
    if not profile:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Специальная обработка для пароля
    if field == "password":
        if value:
            profile.password_hash = hashlib.sha256(value.encode()).hexdigest()
        else:
            profile.password_hash = None
    elif field == "avatar":
        # Обработка аватара отдельно
        pass
    else:
        if hasattr(profile, field):
            setattr(profile, field, value)
    
    chat_state.save_data()
    return JSONResponse({"success": True, "profile": profile.to_dict()})

@app.post("/api/profile/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    nickname: str = Form(...)
):
    """Загрузка аватара"""
    profile = chat_state.get_user_profile(nickname)
    if not profile:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Проверка размера (макс 2MB для аватара)
    content = await file.read()
    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Avatar too large (max 2MB)")
    
    # Проверка что это изображение
    ext = Path(file.filename).suffix.lower()
    if ext not in {'.jpg', '.jpeg', '.png', '.gif', '.webp'}:
        raise HTTPException(status_code=400, detail="Avatar must be an image")
    
    # Удаляем старый аватар
    if profile.avatar:
        old_path = AVATAR_DIR / profile.avatar
        if old_path.exists():
            old_path.unlink()
    
    # Сохраняем новый
    avatar_name = f"avatar_{nickname}_{int(time.time())}{ext}"
    avatar_path = AVATAR_DIR / avatar_name
    with open(avatar_path, "wb") as f:
        f.write(content)
    
    profile.avatar = avatar_name
    chat_state.save_data()
    
    return JSONResponse({"success": True, "avatar": f"/avatars/{avatar_name}"})

@app.get("/api/profile/{nickname}")
async def get_profile(nickname: str):
    """Получение профиля пользователя"""
    profile = chat_state.get_user_profile(nickname)
    if not profile:
        raise HTTPException(status_code=404, detail="User not found")
    return JSONResponse(profile.to_dict())

@app.get("/api/users")
async def get_users():
    """Список всех пользователей"""
    users = [profile.to_dict() for profile in chat_state.users.values()]
    return JSONResponse(users)

# ============================================
# HTML ФРОНТЕНД (упрощённая версия с новыми функциями)
# ============================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Global Chat Pro</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
    <style>
        /* ... (стили из предыдущей версии с дополнениями) ... */
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; height: 100vh; overflow: hidden; }
        
        /* Адаптация существующих стилей ... */
        .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.9); z-index: 1000; justify-content: center; align-items: center; }
        .modal.active { display: flex; }
        .modal-content { max-width: 90%; max-height: 90%; }
        .modal-close { position: fixed; top: 20px; right: 30px; color: white; font-size: 40px; cursor: pointer; z-index: 1001; }
        .file-attachment { display: inline-flex; align-items: center; gap: 0.5rem; background: rgba(255,255,255,0.05); padding: 0.5rem 1rem; border-radius: 0.5rem; margin-top: 0.25rem; cursor: pointer; transition: all 0.3s; }
        .file-attachment:hover { background: rgba(255,255,255,0.1); }
        .file-attachment i { font-size: 1.2rem; }
        .message .avatar { width: 40px; height: 40px; border-radius: 9999px; display: flex; align-items: center; justify-content: center; font-weight: 600; font-size: 0.875rem; flex-shrink: 0; color: white; text-transform: uppercase; background-size: cover; background-position: center; position: relative; }
        .avatar-image { width: 100%; height: 100%; border-radius: 9999px; object-fit: cover; }
        .profile-modal .modal-content { background: #1e293b; color: #f1f5f9; padding: 2rem; border-radius: 1rem; max-width: 600px; width: 90%; max-height: 90vh; overflow-y: auto; }
        .profile-modal .form-group { margin-bottom: 1rem; }
        .profile-modal label { display: block; color: #94a3b8; font-size: 0.875rem; margin-bottom: 0.25rem; }
        .profile-modal input, .profile-modal textarea { width: 100%; background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1); color: #f1f5f9; padding: 0.5rem 0.75rem; border-radius: 0.5rem; }
        .profile-modal input:focus, .profile-modal textarea:focus { outline: none; border-color: #60a5fa; }
        .profile-modal .avatar-upload { display: flex; align-items: center; gap: 1rem; }
        .profile-modal .current-avatar { width: 80px; height: 80px; border-radius: 9999px; object-fit: cover; background: #334155; }
        .profile-modal .btn { padding: 0.5rem 1.5rem; border-radius: 0.5rem; border: none; cursor: pointer; font-weight: 500; transition: all 0.3s; }
        .btn-primary { background: linear-gradient(135deg, #60a5fa, #a78bfa); color: white; }
        .btn-secondary { background: #334155; color: #f1f5f9; }
        .btn-danger { background: #ef4444; color: white; }
        .btn-sm { padding: 0.25rem 0.75rem; font-size: 0.875rem; }
        .file-input-wrapper { position: relative; overflow: hidden; display: inline-block; }
        .file-input-wrapper input[type=file] { position: absolute; left: 0; top: 0; opacity: 0; width: 100%; height: 100%; cursor: pointer; }
        .message-image { max-width: 300px; max-height: 300px; border-radius: 0.5rem; cursor: pointer; margin-top: 0.25rem; transition: transform 0.2s; }
        .message-image:hover { transform: scale(1.02); }
    </style>
</head>
<body>
    <!-- Login Screen -->
    <div id="login-screen">
        <div class="flex flex-col items-center justify-center min-h-screen bg-gradient-to-br from-slate-900 to-slate-800">
            <div class="bg-white/5 backdrop-blur-lg border border-white/10 p-10 rounded-2xl w-full max-w-md shadow-2xl">
                <h1 class="text-4xl font-bold bg-gradient-to-r from-blue-400 to-purple-400 bg-clip-text text-transparent text-center mb-2">💬 Global Chat</h1>
                <p class="text-slate-400 text-center mb-8">Присоединяйтесь к общему чату</p>
                <input type="text" id="nickname-input" placeholder="Введите никнейм..." maxlength="20" class="w-full bg-white/10 border border-white/20 text-slate-100 px-4 py-3 rounded-xl focus:outline-none focus:border-blue-400 focus:ring-2 focus:ring-blue-400/20">
                <div id="login-error" class="text-red-400 text-sm mt-2 hidden"></div>
                <button id="join-btn" class="w-full mt-4 bg-gradient-to-r from-blue-400 to-purple-400 text-white font-semibold py-3 rounded-xl hover:shadow-lg hover:shadow-blue-400/30 transition-all">Войти в чат</button>
                <div class="mt-4 text-center">
                    <button id="show-register-btn" class="text-slate-400 text-sm hover:text-slate-200 transition-colors">🔑 Зарегистрироваться</button>
                </div>
            </div>
        </div>
    </div>

    <!-- Регистрация -->
    <div id="register-screen" style="display:none;">
        <div class="flex flex-col items-center justify-center min-h-screen bg-gradient-to-br from-slate-900 to-slate-800">
            <div class="bg-white/5 backdrop-blur-lg border border-white/10 p-10 rounded-2xl w-full max-w-md shadow-2xl">
                <h2 class="text-3xl font-bold text-white text-center mb-2">📝 Регистрация</h2>
                <p class="text-slate-400 text-center mb-6">Создайте аккаунт для дополнительных функций</p>
                <input type="text" id="reg-nickname" placeholder="Никнейм" maxlength="20" class="w-full bg-white/10 border border-white/20 text-slate-100 px-4 py-3 rounded-xl focus:outline-none focus:border-blue-400 mb-3">
                <input type="password" id="reg-password" placeholder="Пароль (опционально)" class="w-full bg-white/10 border border-white/20 text-slate-100 px-4 py-3 rounded-xl focus:outline-none focus:border-blue-400 mb-3">
                <input type="email" id="reg-email" placeholder="Email (опционально)" class="w-full bg-white/10 border border-white/20 text-slate-100 px-4 py-3 rounded-xl focus:outline-none focus:border-blue-400 mb-3">
                <div id="reg-error" class="text-red-400 text-sm mt-2 hidden"></div>
                <button id="register-btn" class="w-full bg-gradient-to-r from-blue-400 to-purple-400 text-white font-semibold py-3 rounded-xl hover:shadow-lg hover:shadow-blue-400/30 transition-all">Создать аккаунт</button>
                <button id="back-to-login-btn" class="w-full mt-3 text-slate-400 text-sm hover:text-slate-200 transition-colors">← Назад</button>
            </div>
        </div>
    </div>

    <!-- Chat Screen -->
    <div id="chat-screen" style="display:none;">
        <div class="chat-header bg-slate-800/95 backdrop-blur-lg border-b border-white/5 px-6 py-4 flex justify-between items-center flex-wrap gap-2">
            <div class="flex items-center gap-3">
                <span class="text-slate-100 text-xl font-semibold">💬 Global Chat</span>
                <span class="online-badge bg-green-500/20 text-green-400 text-xs px-3 py-1 rounded-full" id="online-count">🟢 0</span>
            </div>
            <div class="flex items-center gap-3">
                <span class="text-slate-400 text-sm">Вы: <span class="text-slate-100 font-medium" id="current-nickname">—</span></span>
                <button id="profile-btn" class="text-slate-400 hover:text-slate-200 transition-colors" title="Профиль"><i class="fas fa-user-circle text-xl"></i></button>
                <button id="leave-btn" class="text-red-400 hover:text-red-300 text-sm px-3 py-1 rounded-full border border-red-400/30 hover:border-red-400/60 transition-all">Выйти</button>
            </div>
        </div>

        <div class="connection-status" id="connection-status">⚠️ Нет соединения с сервером...</div>

        <div class="messages-container flex-1 overflow-y-auto px-6 py-4 space-y-2" id="messages-container"></div>

        <div class="input-area bg-slate-800/95 backdrop-blur-lg border-t border-white/5 px-6 py-3 flex gap-3">
            <div class="flex gap-2">
                <button id="attach-btn" class="text-slate-400 hover:text-slate-200 transition-colors" title="Прикрепить файл"><i class="fas fa-paperclip text-xl"></i></button>
                <button id="image-btn" class="text-slate-400 hover:text-slate-200 transition-colors" title="Прикрепить изображение"><i class="fas fa-image text-xl"></i></button>
            </div>
            <input type="text" id="message-input" placeholder="Введите сообщение..." disabled class="flex-1 bg-white/5 border border-white/10 text-slate-100 px-4 py-2 rounded-xl focus:outline-none focus:border-blue-400">
            <button id="send-btn" disabled class="bg-gradient-to-r from-blue-400 to-purple-400 text-white px-6 py-2 rounded-xl font-semibold hover:shadow-lg hover:shadow-blue-400/30 transition-all disabled:opacity-50 disabled:cursor-not-allowed">Отправить</button>
        </div>
    </div>

    <!-- Модальное окно для просмотра изображений -->
    <div class="modal" id="image-modal">
        <span class="modal-close" id="modal-close">&times;</span>
        <img class="modal-content" id="modal-image">
    </div>

    <!-- Модальное окно профиля -->
    <div class="modal profile-modal" id="profile-modal">
        <div class="modal-content">
            <h2 class="text-2xl font-bold text-white mb-4">👤 Профиль</h2>
            <div class="form-group avatar-upload">
                <img id="profile-avatar" class="current-avatar" src="" alt="Avatar">
                <div class="flex flex-col gap-2">
                    <div class="file-input-wrapper">
                        <button class="btn btn-primary btn-sm">📷 Сменить аватар</button>
                        <input type="file" id="avatar-input" accept="image/*">
                    </div>
                    <span class="text-xs text-slate-500">Максимум 2MB</span>
                </div>
            </div>
            <div class="form-group">
                <label>Информация о себе</label>
                <textarea id="profile-info" rows="3" placeholder="Расскажите о себе..."></textarea>
            </div>
            <div class="form-group">
                <label>Телефон</label>
                <input type="tel" id="profile-phone" placeholder="+7 (XXX) XXX-XX-XX">
            </div>
            <div class="form-group">
                <label>Email</label>
                <input type="email" id="profile-email" placeholder="email@example.com">
            </div>
            <div class="form-group">
                <label>Пароль</label>
                <input type="password" id="profile-password" placeholder="Введите новый пароль (оставьте пустым для удаления)">
                <span class="text-xs text-slate-500">Оставьте пустым, чтобы удалить пароль</span>
            </div>
            <div class="flex gap-3 mt-4">
                <button id="profile-save-btn" class="btn btn-primary">💾 Сохранить</button>
                <button id="profile-close-btn" class="btn btn-secondary">Закрыть</button>
            </div>
            <div id="profile-message" class="mt-2 text-sm hidden"></div>
        </div>
    </div>

    <!-- Скрытые input для загрузки файлов -->
    <input type="file" id="file-input" style="display:none" multiple>
    <input type="file" id="image-input" style="display:none" accept="image/*" multiple>

    <script>
        // ============================================
        // СОСТОЯНИЕ КЛИЕНТА
        // ============================================
        const state = {
            ws: null,
            nickname: '',
            connected: false,
            reconnecting: false,
            reconnectAttempts: 0,
            maxReconnectAttempts: 10,
            profile: null,
        };

        // ============================================
        // DOM ЭЛЕМЕНТЫ
        // ============================================
        const $ = (id) => document.getElementById(id);
        const loginScreen = $('login-screen');
        const registerScreen = $('register-screen');
        const chatScreen = $('chat-screen');
        const nicknameInput = $('nickname-input');
        const joinBtn = $('join-btn');
        const loginError = $('login-error');
        const messagesContainer = $('messages-container');
        const messageInput = $('message-input');
        const sendBtn = $('send-btn');
        const currentNickname = $('current-nickname');
        const onlineCount = $('online-count');
        const connectionStatus = $('connection-status');
        const leaveBtn = $('leave-btn');
        const attachBtn = $('attach-btn');
        const imageBtn = $('image-btn');
        const fileInput = $('file-input');
        const imageInput = $('image-input');
        const profileBtn = $('profile-btn');
        const profileModal = $('profile-modal');
        const profileCloseBtn = $('profile-close-btn');
        const profileSaveBtn = $('profile-save-btn');
        const profileAvatar = $('profile-avatar');
        const profileInfo = $('profile-info');
        const profilePhone = $('profile-phone');
        const profileEmail = $('profile-email');
        const profilePassword = $('profile-password');
        const profileMessage = $('profile-message');
        const avatarInput = $('avatar-input');
        const imageModal = $('image-modal');
        const modalImage = $('modal-image');
        const modalClose = $('modal-close');
        const showRegisterBtn = $('show-register-btn');
        const registerBtn = $('register-btn');
        const backToLoginBtn = $('back-to-login-btn');
        const regNickname = $('reg-nickname');
        const regPassword = $('reg-password');
        const regEmail = $('reg-email');
        const regError = $('reg-error');

        // ============================================
        // УТИЛИТЫ
        // ============================================
        function getColorFromString(str) {
            let hash = 0;
            for (let i = 0; i < str.length; i++) {
                hash = str.charCodeAt(i) + ((hash << 5) - hash);
            }
            return `hsl(${Math.abs(hash) % 360}, 70%, 60%)`;
        }

        function formatTime(timestamp) {
            const date = new Date(timestamp * 1000);
            return date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
        }

        function formatFileSize(bytes) {
            if (bytes < 1024) return bytes + ' B';
            if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
            return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
        }

        function scrollToBottom() {
            messagesContainer.scrollTop = messagesContainer.scrollHeight;
        }

        function getFileIcon(extension) {
            const icons = {
                '.pdf': 'fa-file-pdf',
                '.doc': 'fa-file-word',
                '.docx': 'fa-file-word',
                '.txt': 'fa-file-alt',
                '.zip': 'fa-file-archive',
                '.rar': 'fa-file-archive',
                '.jpg': 'fa-file-image',
                '.jpeg': 'fa-file-image',
                '.png': 'fa-file-image',
                '.gif': 'fa-file-image',
                '.webp': 'fa-file-image',
            };
            return icons[extension] || 'fa-file';
        }

        function getFileColor(extension) {
            const colors = {
                '.pdf': '#ef4444',
                '.doc': '#3b82f6',
                '.docx': '#3b82f6',
                '.txt': '#8b5cf6',
                '.zip': '#f59e0b',
                '.rar': '#f59e0b',
                '.jpg': '#10b981',
                '.jpeg': '#10b981',
                '.png': '#10b981',
                '.gif': '#10b981',
                '.webp': '#10b981',
            };
            return colors[extension] || '#64748b';
        }

        // ============================================
        // ОТРИСОВКА СООБЩЕНИЙ
        // ============================================
        function renderMessage(message) {
            const div = document.createElement('div');
            div.className = `message flex gap-3 animate-fadeIn ${message.is_system ? 'system' : ''}`;
            
            // Аватар
            const avatar = document.createElement('div');
            avatar.className = 'avatar flex-shrink-0';
            
            if (message.is_system) {
                avatar.textContent = '🔔';
                avatar.style.background = '#334155';
            } else {
                // Проверяем есть ли аватар у пользователя
                const profile = state.profile && state.profile.nickname === message.nickname ? state.profile : null;
                if (profile && profile.avatar) {
                    const img = document.createElement('img');
                    img.className = 'avatar-image';
                    img.src = profile.avatar;
                    avatar.appendChild(img);
                } else {
                    const initial = message.nickname.charAt(0).toUpperCase();
                    avatar.textContent = initial;
                    avatar.style.background = getColorFromString(message.nickname);
                }
            }
            div.appendChild(avatar);

            // Контент
            const content = document.createElement('div');
            content.className = 'content flex-1 min-w-0';

            // Имя и время
            const header = document.createElement('div');
            header.className = 'flex items-center gap-2 flex-wrap';
            
            if (!message.is_system) {
                const nameSpan = document.createElement('span');
                nameSpan.className = 'font-semibold text-sm';
                nameSpan.textContent = message.nickname;
                nameSpan.style.color = getColorFromString(message.nickname);
                header.appendChild(nameSpan);
            }

            const timeSpan = document.createElement('span');
            timeSpan.className = 'text-slate-500 text-xs';
            timeSpan.textContent = formatTime(message.timestamp);
            header.appendChild(timeSpan);
            content.appendChild(header);

            // Текст
            const textSpan = document.createElement('div');
            textSpan.className = `text-slate-100 ${message.is_system ? 'text-slate-400 italic' : ''}`;
            textSpan.textContent = message.text;
            content.appendChild(textSpan);

            // Файл
            if (message.file) {
                const fileDiv = document.createElement('div');
                fileDiv.className = 'file-attachment';
                
                if (message.file.is_image) {
                    // Отображение изображения
                    const img = document.createElement('img');
                    img.className = 'message-image';
                    img.src = message.file.url;
                    img.alt = message.file.filename;
                    img.onclick = () => openImageModal(message.file.url);
                    fileDiv.appendChild(img);
                } else {
                    // Отображение файла
                    const icon = document.createElement('i');
                    icon.className = `fas ${getFileIcon(message.file.extension)}`;
                    icon.style.color = getFileColor(message.file.extension);
                    fileDiv.appendChild(icon);
                    
                    const info = document.createElement('span');
                    info.className = 'text-sm text-slate-300';
                    info.textContent = `${message.file.filename} (${formatFileSize(message.file.size)})`;
                    fileDiv.appendChild(info);
                    
                    const downloadBtn = document.createElement('a');
                    downloadBtn.href = message.file.url;
                    downloadBtn.download = message.file.filename;
                    downloadBtn.className = 'text-blue-400 hover:text-blue-300 text-sm ml-2';
                    downloadBtn.innerHTML = '<i class="fas fa-download"></i>';
                    fileDiv.appendChild(downloadBtn);
                    
                    fileDiv.onclick = () => {
                        window.open(message.file.url, '_blank');
                    };
                }
                
                content.appendChild(fileDiv);
            }

            div.appendChild(content);
            return div;
        }

        function renderMessages(messages) {
            messagesContainer.innerHTML = '';
            messages.forEach(msg => {
                messagesContainer.appendChild(renderMessage(msg));
            });
            scrollToBottom();
        }

        function appendMessage(message) {
            messagesContainer.appendChild(renderMessage(message));
            scrollToBottom();
        }

        // ============================================
        // МОДАЛЬНОЕ ОКНО ДЛЯ ИЗОБРАЖЕНИЙ
        // ============================================
        function openImageModal(url) {
            modalImage.src = url;
            imageModal.classList.add('active');
        }

        modalClose.onclick = () => imageModal.classList.remove('active');
        imageModal.onclick = (e) => {
            if (e.target === imageModal) imageModal.classList.remove('active');
        };

        // ============================================
        // WEBSOCKET
        // ============================================
        function getWebSocketUrl() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            return `${protocol}//${window.location.host}/ws`;
        }

        function connectWebSocket() {
            if (state.ws && state.ws.readyState === WebSocket.OPEN) return;

            state.reconnecting = true;
            updateConnectionStatus(false);

            try {
                state.ws = new WebSocket(getWebSocketUrl());
            } catch (e) {
                console.error('WebSocket connection error:', e);
                scheduleReconnect();
                return;
            }

            state.ws.onopen = function() {
                console.log('WebSocket connected');
                state.connected = true;
                state.reconnecting = false;
                state.reconnectAttempts = 0;
                updateConnectionStatus(true);
                enableChat(true);
                sendMessage({ type: 'join', nickname: state.nickname });
            };

            state.ws.onmessage = function(event) {
                try {
                    const data = JSON.parse(event.data);
                    handleWebSocketMessage(data);
                } catch (e) {
                    console.error('Failed to parse message:', e);
                }
            };

            state.ws.onclose = function() {
                console.log('WebSocket closed');
                state.connected = false;
                updateConnectionStatus(false);
                enableChat(false);
                scheduleReconnect();
            };

            state.ws.onerror = function(error) {
                console.error('WebSocket error:', error);
            };
        }

        function scheduleReconnect() {
            if (state.reconnectAttempts >= state.maxReconnectAttempts) return;
            state.reconnectAttempts++;
            const delay = Math.min(1000 * Math.pow(1.5, state.reconnectAttempts), 30000);
            setTimeout(() => {
                if (!state.connected) connectWebSocket();
            }, delay);
        }

        function sendMessage(data) {
            if (state.ws && state.ws.readyState === WebSocket.OPEN) {
                state.ws.send(JSON.stringify(data));
                return true;
            }
            return false;
        }

        function handleWebSocketMessage(data) {
            switch (data.type) {
                case 'history':
                    renderMessages(data.messages);
                    break;
                case 'message':
                    appendMessage(data.message);
                    break;
                case 'online_count':
                    updateOnlineCount(data.count);
                    break;
                case 'error':
                    console.error('Server error:', data.message);
                    showNotification(data.message, 'error');
                    break;
                default:
                    console.log('Unknown message type:', data.type);
            }
        }

        // ============================================
        // ЗАГРУЗКА ФАЙЛОВ
        // ============================================
        async function uploadFiles(files, isImage = false) {
            if (!files || files.length === 0) return;

            for (const file of files) {
                const formData = new FormData();
                formData.append('file', file);
                formData.append('nickname', state.nickname);

                try {
                    const response = await fetch('/api/upload', {
                        method: 'POST',
                        body: formData
                    });

                    if (!response.ok) {
                        const error = await response.json();
                        showNotification(`Ошибка загрузки: ${error.detail}`, 'error');
                        continue;
                    }

                    const fileInfo = await response.json();
                    // Отправляем сообщение с файлом
                    sendMessage({
                        type: 'message',
                        text: isImage ? '📷 Изображение' : `📎 ${fileInfo.filename}`,
                        file: fileInfo
                    });

                } catch (error) {
                    console.error('Upload error:', error);
                    showNotification('Ошибка загрузки файла', 'error');
                }
            }
        }

        // ============================================
        // ПРОФИЛЬ
        // ============================================
        async function loadProfile(nickname) {
            try {
                const response = await fetch(`/api/profile/${encodeURIComponent(nickname)}`);
                if (response.ok) {
                    state.profile = await response.json();
                    updateProfileUI();
                }
            } catch (error) {
                console.error('Error loading profile:', error);
            }
        }

        function updateProfileUI() {
            if (!state.profile) return;
            profileAvatar.src = state.profile.avatar || 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="80" height="80"%3E%3Ccircle cx="40" cy="40" r="40" fill="%23334155"/%3E%3Ctext x="40" y="45" text-anchor="middle" fill="%23f1f5f9" font-size="30" font-weight="bold"%3E' + state.nickname.charAt(0).toUpperCase() + '%3C/text%3E%3C/svg%3E';
            profileInfo.value = state.profile.info || '';
            profilePhone.value = state.profile.phone || '';
            profileEmail.value = state.profile.email || '';
        }

        async function saveProfile() {
            const updates = {
                info: profileInfo.value,
                phone: profilePhone.value,
                email: profileEmail.value,
                password: profilePassword.value
            };

            let success = true;
            for (const [field, value] of Object.entries(updates)) {
                if (field === 'password' && value === '') continue;
                
                const formData = new FormData();
                formData.append('nickname', state.nickname);
                formData.append('field', field);
                formData.append('value', value);

                try {
                    const response = await fetch('/api/profile/update', {
                        method: 'POST',
                        body: formData
                    });
                    if (!response.ok) {
                        success = false;
                        const error = await response.json();
                        showProfileMessage(error.detail || 'Ошибка сохранения', 'error');
                    }
                } catch (error) {
                    success = false;
                    showProfileMessage('Ошибка сохранения', 'error');
                }
            }

            if (success) {
                showProfileMessage('✅ Профиль сохранён!', 'success');
                await loadProfile(state.nickname);
                profilePassword.value = '';
            }
        }

        async function uploadAvatar(file) {
            const formData = new FormData();
            formData.append('file', file);
            formData.append('nickname', state.nickname);

            try {
                const response = await fetch('/api/profile/avatar', {
                    method: 'POST',
                    body: formData
                });

                if (!response.ok) {
                    const error = await response.json();
                    showProfileMessage(error.detail || 'Ошибка загрузки аватара', 'error');
                    return;
                }

                const result = await response.json();
                state.profile.avatar = result.avatar;
                updateProfileUI();
                showProfileMessage('✅ Аватар обновлён!', 'success');
                
                // Обновляем отображение в чате
                const messages = messagesContainer.querySelectorAll('.message .avatar');
                // (обновление всех аватаров - сложно, перезагрузим сообщения)
                // В реальном приложении лучше отправлять событие через WebSocket

            } catch (error) {
                console.error('Avatar upload error:', error);
                showProfileMessage('Ошибка загрузки аватара', 'error');
            }
        }

        function showProfileMessage(text, type) {
            profileMessage.textContent = text;
            profileMessage.className = `mt-2 text-sm ${type === 'error' ? 'text-red-400' : 'text-green-400'}`;
            profileMessage.style.display = 'block';
            setTimeout(() => {
                profileMessage.style.display = 'none';
            }, 5000);
        }

        function showNotification(text, type = 'info') {
            // Простое уведомление - можно улучшить
            console.log(`[${type}]`, text);
        }

        // ============================================
        // UI ОБНОВЛЕНИЯ
        // ============================================
        function updateConnectionStatus(connected) {
            if (connected) {
                connectionStatus.className = 'connection-status connected bg-green-500/10 text-green-400 border-b border-green-500/20 px-6 py-2 text-center text-sm font-medium';
                connectionStatus.textContent = '✅ Соединение установлено';
                setTimeout(() => {
                    connectionStatus.style.display = 'none';
                }, 3000);
            } else {
                connectionStatus.className = 'connection-status disconnected bg-red-500/10 text-red-400 border-b border-red-500/20 px-6 py-2 text-center text-sm font-medium';
                connectionStatus.textContent = '⚠️ Нет соединения с сервером...';
                connectionStatus.style.display = 'block';
            }
        }

        function enableChat(enabled) {
            messageInput.disabled = !enabled;
            sendBtn.disabled = !enabled;
            if (enabled) messageInput.focus();
        }

        function updateOnlineCount(count) {
            onlineCount.textContent = `🟢 ${count}`;
        }

        // ============================================
        // ВХОД И ВЫХОД
        // ============================================
        function joinChat(nickname) {
            state.nickname = nickname;
            currentNickname.textContent = nickname;

            loginScreen.style.display = 'none';
            registerScreen.style.display = 'none';
            chatScreen.style.display = 'flex';

            loadProfile(nickname);
            connectWebSocket();
        }

        function leaveChat() {
            if (state.ws) state.ws.close();
            state.connected = false;
            state.reconnecting = false;
            state.nickname = '';

            chatScreen.style.display = 'none';
            loginScreen.style.display = 'flex';
            nicknameInput.value = '';
            nicknameInput.focus();
        }

        // ============================================
        // РЕГИСТРАЦИЯ
        // ============================================
        async function registerUser() {
            const nickname = regNickname.value.trim();
            const password = regPassword.value;
            const email = regEmail.value.trim();

            if (!nickname) {
                regError.textContent = 'Введите никнейм';
                regError.style.display = 'block';
                return;
            }

            if (nickname.toLowerCase() === 'system') {
                regError.textContent = 'Этот никнейм зарезервирован';
                regError.style.display = 'block';
                return;
            }

            // Проверяем, существует ли пользователь
            try {
                const response = await fetch(`/api/profile/${encodeURIComponent(nickname)}`);
                if (response.ok) {
                    regError.textContent = 'Пользователь с таким никнеймом уже существует';
                    regError.style.display = 'block';
                    return;
                }
            } catch (e) {}

            // Создаём пользователя через WebSocket
            regError.style.display = 'none';
            
            // Входим с созданием профиля
            state.nickname = nickname;
            // Отправляем join с паролем
            connectWebSocket();
            // Ждём подключения и отправляем join с паролем
            const checkConnection = setInterval(() => {
                if (state.connected) {
                    clearInterval(checkConnection);
                    sendMessage({ 
                        type: 'join', 
                        nickname: nickname,
                        password: password || undefined,
                        email: email || undefined
                    });
                    // Переключаем экран
                    loginScreen.style.display = 'none';
                    registerScreen.style.display = 'none';
                    chatScreen.style.display = 'flex';
                    currentNickname.textContent = nickname;
                    loadProfile(nickname);
                }
            }, 100);
        }

        // ============================================
        // ОБРАБОТЧИКИ СОБЫТИЙ
        // ============================================
        joinBtn.addEventListener('click', () => {
            const nickname = nicknameInput.value.trim();
            if (!nickname) {
                loginError.textContent = 'Введите никнейм';
                loginError.style.display = 'block';
                return;
            }
            if (nickname.toLowerCase() === 'system') {
                loginError.textContent = 'Этот никнейм зарезервирован';
                loginError.style.display = 'block';
                return;
            }
            loginError.style.display = 'none';
            joinChat(nickname);
        });

        nicknameInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') joinBtn.click();
        });

        sendBtn.addEventListener('click', () => {
            const text = messageInput.value.trim();
            if (text) {
                sendMessage({ type: 'message', text: text });
                messageInput.value = '';
            }
        });

        messageInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendBtn.click();
            }
        });

        leaveBtn.addEventListener('click', leaveChat);

        attachBtn.addEventListener('click', () => fileInput.click());
        imageBtn.addEventListener('click', () => imageInput.click());

        fileInput.addEventListener('change', (e) => {
            if (e.target.files.length > 0) {
                uploadFiles(e.target.files, false);
                e.target.value = '';
            }
        });

        imageInput.addEventListener('change', (e) => {
            if (e.target.files.length > 0) {
                uploadFiles(e.target.files, true);
                e.target.value = '';
            }
        });

        // Профиль
        profileBtn.addEventListener('click', () => {
            profileModal.classList.add('active');
            updateProfileUI();
        });

        profileCloseBtn.addEventListener('click', () => {
            profileModal.classList.remove('active');
        });

        profileModal.addEventListener('click', (e) => {
            if (e.target === profileModal) profileModal.classList.remove('active');
        });

        profileSaveBtn.addEventListener('click', saveProfile);

        avatarInput.addEventListener('change', (e) => {
            if (e.target.files.length > 0) {
                uploadAvatar(e.target.files[0]);
                e.target.value = '';
            }
        });

        // Регистрация
        showRegisterBtn.addEventListener('click', () => {
            loginScreen.style.display = 'none';
            registerScreen.style.display = 'flex';
        });

        backToLoginBtn.addEventListener('click', () => {
            registerScreen.style.display = 'none';
            loginScreen.style.display = 'flex';
        });

        registerBtn.addEventListener('click', registerUser);

        regNickname.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') registerBtn.click();
        });

        regPassword.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') registerBtn.click();
        });

        // ============================================
        // ИНИЦИАЛИЗАЦИЯ
        // ============================================
        nicknameInput.focus();

        console.log('💬 Global Chat Pro loaded!');
        console.log('✨ Features: Files, Images, Profiles, User registration');
    </script>
</body>
</html>
"""

# ============================================
# WEBSOCKET ЭНДПОИНТ (обновлённый)
# ============================================
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    nickname = None
    user_data = {"nickname": "Anonymous", "last_message_time": 0}
    
    try:
        # Ожидаем join сообщение
        data = await websocket.receive_text()
        try:
            join_data = json.loads(data)
            if join_data.get("type") == "join" and join_data.get("nickname"):
                nickname = join_data["nickname"].strip()[:20]
                
                if nickname.lower() == "system":
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "Никнейм 'System' зарезервирован"
                    }))
                    await websocket.close(code=1008)
                    return
                
                # Проверяем пароль если есть
                password = join_data.get("password")
                email = join_data.get("email")
                
                # Проверяем существование пользователя
                profile = chat_state.get_user_profile(nickname)
                
                if profile:
                    # Проверяем пароль
                    if profile.password_hash:
                        if not password or not chat_state.verify_password(nickname, password):
                            await websocket.send_text(json.dumps({
                                "type": "error",
                                "message": "Неверный пароль"
                            }))
                            await websocket.close(code=1008)
                            return
                else:
                    # Создаём нового пользователя
                    chat_state.create_user(nickname, password)
                    if email:
                        chat_state.update_profile(nickname, email=email)
                
                user_data["nickname"] = nickname
                profile.last_seen = time.time()
                chat_state.save_data()
            else:
                await websocket.close(code=1008)
                return
        except json.JSONDecodeError:
            await websocket.close(code=1008)
            return

        # Проверяем, что никнейм не занят в чате
        if nickname in chat_state.nicknames:
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": f"Никнейм '{nickname}' уже в чате"
            }))
            await websocket.close(code=1008)
            return

        # Регистрируем в чате
        chat_state.active_connections[websocket] = user_data
        chat_state.nicknames.add(nickname)
        
        # Отправляем историю
        await websocket.send_text(json.dumps({
            "type": "history",
            "messages": chat_state.get_messages()
        }))

        # Оповещаем о новом пользователе
        system_msg = chat_state.add_message(
            "System",
            f"👋 {nickname} присоединился к чату",
            is_system=True
        )
        await broadcast_message(system_msg)
        await broadcast_online_count()

        # Основной цикл
        while True:
            try:
                data = await websocket.receive_text()
                try:
                    message_data = json.loads(data)
                    if message_data.get("type") == "message":
                        text = message_data.get("text", "").strip()
                        file_info = message_data.get("file")
                        
                        if text or file_info:
                            if chat_state.is_rate_limited(websocket):
                                await websocket.send_text(json.dumps({
                                    "type": "error",
                                    "message": "Слишком много сообщений!"
                                }))
                                continue
                            
                            msg = chat_state.add_message(nickname, text, file_info=file_info)
                            await broadcast_message(msg)
                            
                except json.JSONDecodeError:
                    pass
                    
            except WebSocketDisconnect:
                break
            except Exception as e:
                print(f"Error in message loop: {e}")
                break

    except WebSocketDisconnect:
        pass
    finally:
        if websocket in chat_state.active_connections:
            del chat_state.active_connections[websocket]
        if nickname and nickname in chat_state.nicknames:
            chat_state.nicknames.remove(nickname)
        
        if nickname:
            system_msg = chat_state.add_message(
                "System",
                f"👋 {nickname} покинул чат",
                is_system=True
            )
            await broadcast_message(system_msg)
            await broadcast_online_count()

# ============================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================
async def broadcast_message(message: dict):
    if not chat_state.active_connections:
        return
    
    data = json.dumps({"type": "message", "message": message})
    disconnected = []
    
    for connection in chat_state.active_connections.keys():
        try:
            await connection.send_text(data)
        except Exception:
            disconnected.append(connection)
    
    for conn in disconnected:
        if conn in chat_state.active_connections:
            del chat_state.active_connections[conn]

async def broadcast_online_count():
    count = chat_state.get_online_count()
    data = json.dumps({"type": "online_count", "count": count})
    disconnected = []
    
    for connection in chat_state.active_connections.keys():
        try:
            await connection.send_text(data)
        except Exception:
            disconnected.append(connection)
    
    for conn in disconnected:
        if conn in chat_state.active_connections:
            del chat_state.active_connections[conn]

# ============================================
# ЗАПУСК
# ============================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
