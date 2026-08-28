import asyncio
import hashlib
import json
import time
import os
from collections import deque
from datetime import datetime
from typing import List, Dict, Set, Optional
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# ============================================
# КОНФИГУРАЦИЯ
# ============================================
MAX_MESSAGES = 100
RATE_LIMIT_SECONDS = 1
MAX_FILE_SIZE = 10 * 1024 * 1024
MIN_PASSWORD_LENGTH = 4
ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.pdf', '.doc', '.docx', '.txt', '.zip', '.rar'}

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
AVATAR_DIR = BASE_DIR / "avatars"
DATA_DIR = BASE_DIR / "data"

for dir_path in [UPLOAD_DIR, AVATAR_DIR, DATA_DIR]:
    dir_path.mkdir(exist_ok=True)

# ============================================
# ХРАНЕНИЕ ДАННЫХ
# ============================================
class UserProfile:
    def __init__(self, nickname: str):
        self.nickname = nickname
        self.password_hash = None
        self.avatar = None
        self.info = ""
        self.phone = ""
        self.email = ""
        self.created_at = time.time()
        self.last_seen = time.time()
        self.is_online = False
    
    def to_dict(self):
        return {
            "nickname": self.nickname,
            "has_password": self.password_hash is not None,
            "avatar": f"/avatars/{self.avatar}" if self.avatar else None,
            "info": self.info,
            "phone": self.phone,
            "email": self.email,
            "created_at": self.created_at,
            "last_seen": self.last_seen,
            "is_online": self.is_online
        }

class ChatState:
    def __init__(self):
        self.messages: deque = deque(maxlen=MAX_MESSAGES)
        self.active_connections: Dict[WebSocket, Dict] = {}
        self.nicknames: Set[str] = set()
        self.users: Dict[str, UserProfile] = {}
        self.file_counter = 0
        self.load_data()
        if not self.messages:
            self.add_message("System", "🚀 Добро пожаловать в Global Chat!", is_system=True)

    def add_message(self, nickname: str, text: str, is_system: bool = False, file_info: Optional[Dict] = None, message_id: Optional[str] = None):
        if message_id is None:
            message_id = f"msg_{int(time.time() * 1000)}_{len(self.messages)}"
        message = {
            "id": message_id,
            "nickname": nickname,
            "text": text,
            "timestamp": time.time(),
            "is_system": is_system,
            "file": file_info
        }
        self.messages.append(message)
        self.save_data()
        return message

    def delete_message(self, message_id: str, nickname: str) -> bool:
        """Удаляет сообщение, если оно принадлежит пользователю"""
        for i, msg in enumerate(self.messages):
            if msg.get("id") == message_id:
                if msg.get("nickname") == nickname:
                    # Помечаем как удалённое
                    self.messages[i]["text"] = "🗑️ Сообщение удалено"
                    self.messages[i]["deleted"] = True
                    self.save_data()
                    return True
                return False
        return False

    def get_messages(self) -> List[Dict]:
        return list(self.messages)

    def get_online_count(self) -> int:
        return len(self.active_connections)

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

    def create_user(self, nickname: str, password: str) -> UserProfile:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
        profile = UserProfile(nickname)
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

    def user_exists(self, nickname: str) -> bool:
        return nickname in self.users

    def save_data(self):
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
        try:
            with open(DATA_DIR / "chat_data.json", "r", encoding="utf-8") as f:
                data = json.load(f)
            self.messages = deque(data.get("messages", []), maxlen=MAX_MESSAGES)
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
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"Error loading data: {e}")

chat_state = ChatState()

# ============================================
# FASTAPI ПРИЛОЖЕНИЕ
# ============================================
app = FastAPI(title="Global Chat Pro")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")
app.mount("/avatars", StaticFiles(directory=str(AVATAR_DIR)), name="avatars")

# ============================================
# API ЭНДПОИНТЫ
# ============================================
@app.get("/", response_class=HTMLResponse)
async def get_chat_page():
    return HTML_TEMPLATE

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...), nickname: str = Form(...)):
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 10MB)")
    
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="File type not allowed")
    
    chat_state.file_counter += 1
    safe_filename = f"file_{chat_state.file_counter}_{int(time.time())}_{file.filename}"
    file_path = UPLOAD_DIR / safe_filename
    
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
    
    if ext in {'.jpg', '.jpeg', '.png', '.gif', '.webp'}:
        file_info["is_image"] = True
    
    chat_state.save_data()
    return JSONResponse(file_info)

@app.post("/api/profile/update")
async def update_profile(nickname: str = Form(...), field: str = Form(...), value: str = Form(...)):
    profile = chat_state.get_user_profile(nickname)
    if not profile:
        raise HTTPException(status_code=404, detail="User not found")
    
    if field == "password":
        if value:
            if len(value) < MIN_PASSWORD_LENGTH:
                raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
            profile.password_hash = hashlib.sha256(value.encode()).hexdigest()
        else:
            profile.password_hash = None
    elif hasattr(profile, field):
        setattr(profile, field, value)
    
    chat_state.save_data()
    return JSONResponse({"success": True, "profile": profile.to_dict()})

@app.post("/api/profile/avatar")
async def upload_avatar(file: UploadFile = File(...), nickname: str = Form(...)):
    profile = chat_state.get_user_profile(nickname)
    if not profile:
        raise HTTPException(status_code=404, detail="User not found")
    
    content = await file.read()
    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Avatar too large (max 2MB)")
    
    ext = Path(file.filename).suffix.lower()
    if ext not in {'.jpg', '.jpeg', '.png', '.gif', '.webp'}:
        raise HTTPException(status_code=400, detail="Avatar must be an image")
    
    if profile.avatar:
        old_path = AVATAR_DIR / profile.avatar
        if old_path.exists():
            old_path.unlink()
    
    avatar_name = f"avatar_{nickname}_{int(time.time())}{ext}"
    avatar_path = AVATAR_DIR / avatar_name
    with open(avatar_path, "wb") as f:
        f.write(content)
    
    profile.avatar = avatar_name
    chat_state.save_data()
    return JSONResponse({"success": True, "avatar": f"/avatars/{avatar_name}"})

@app.get("/api/profile/{nickname}")
async def get_profile(nickname: str):
    profile = chat_state.get_user_profile(nickname)
    if not profile:
        raise HTTPException(status_code=404, detail="User not found")
    return JSONResponse(profile.to_dict())

# ============================================
# WEBSOCKET ЭНДПОИНТ
# ============================================
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    nickname = None
    user_data = {"nickname": "Anonymous", "last_message_time": 0}
    
    try:
        data = await websocket.receive_text()
        try:
            join_data = json.loads(data)
            if join_data.get("type") == "join" and join_data.get("nickname"):
                nickname = join_data["nickname"].strip()[:20]
                password = join_data.get("password")
                
                if nickname.lower() == "system":
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "Никнейм 'System' зарезервирован"
                    }))
                    await websocket.close(code=1008)
                    return
                
                # Проверяем существование пользователя
                if chat_state.user_exists(nickname):
                    # Авторизация
                    if not password:
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "message": "Требуется пароль"
                        }))
                        await websocket.close(code=1008)
                        return
                    if not chat_state.verify_password(nickname, password):
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "message": "Неверный пароль"
                        }))
                        await websocket.close(code=1008)
                        return
                else:
                    # Регистрация
                    if not password or len(password) < MIN_PASSWORD_LENGTH:
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "message": f"Пароль должен быть минимум {MIN_PASSWORD_LENGTH} символов"
                        }))
                        await websocket.close(code=1008)
                        return
                    chat_state.create_user(nickname, password)
                
                user_data["nickname"] = nickname
                profile = chat_state.get_user_profile(nickname)
                profile.is_online = True
                profile.last_seen = time.time()
                chat_state.save_data()
            else:
                await websocket.close(code=1008)
                return
        except json.JSONDecodeError:
            await websocket.close(code=1008)
            return

        if nickname in chat_state.nicknames:
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": f"Никнейм '{nickname}' уже в чате"
            }))
            await websocket.close(code=1008)
            return

        chat_state.active_connections[websocket] = user_data
        chat_state.nicknames.add(nickname)
        
        await websocket.send_text(json.dumps({
            "type": "history",
            "messages": chat_state.get_messages()
        }))

        system_msg = chat_state.add_message(
            "System",
            f"👋 {nickname} присоединился к чату",
            is_system=True
        )
        await broadcast_message(system_msg)
        await broadcast_online_count()

        while True:
            try:
                data = await websocket.receive_text()
                try:
                    message_data = json.loads(data)
                    msg_type = message_data.get("type")
                    
                    if msg_type == "message":
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
                    
                    elif msg_type == "delete":
                        message_id = message_data.get("message_id")
                        if message_id:
                            success = chat_state.delete_message(message_id, nickname)
                            if success:
                                await broadcast_message({
                                    "id": message_id,
                                    "deleted": True,
                                    "nickname": nickname
                                }, is_delete=True)
                            else:
                                await websocket.send_text(json.dumps({
                                    "type": "error",
                                    "message": "Нельзя удалить это сообщение"
                                }))
                    
                    elif msg_type == "edit":
                        message_id = message_data.get("message_id")
                        new_text = message_data.get("text", "").strip()
                        if message_id and new_text:
                            # Находим и редактируем сообщение
                            for msg in chat_state.messages:
                                if msg.get("id") == message_id and msg.get("nickname") == nickname:
                                    msg["text"] = new_text
                                    msg["edited"] = True
                                    msg["edited_at"] = time.time()
                                    chat_state.save_data()
                                    await broadcast_message(msg, is_edit=True)
                                    break
                    
                    elif msg_type == "mention":
                        target = message_data.get("target")
                        if target:
                            # Отправляем уведомление конкретному пользователю
                            await send_mention_notification(nickname, target, message_data.get("text", ""))
                    
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
            profile = chat_state.get_user_profile(nickname)
            if profile:
                profile.is_online = False
                profile.last_seen = time.time()
                chat_state.save_data()
            
            system_msg = chat_state.add_message(
                "System",
                f"👋 {nickname} покинул чат",
                is_system=True
            )
            await broadcast_message(system_msg)
            await broadcast_online_count()

async def broadcast_message(message: dict, is_delete: bool = False, is_edit: bool = False):
    if not chat_state.active_connections:
        return
    
    if is_delete:
        data = json.dumps({"type": "delete", "message": message})
    elif is_edit:
        data = json.dumps({"type": "edit", "message": message})
    else:
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

async def send_mention_notification(from_user: str, to_user: str, text: str):
    """Отправляет уведомление о упоминании"""
    notification = {
        "type": "mention",
        "from": from_user,
        "to": to_user,
        "text": text,
        "timestamp": time.time()
    }
    
    data = json.dumps(notification)
    for connection, user_data in chat_state.active_connections.items():
        if user_data.get("nickname") == to_user:
            try:
                await connection.send_text(data)
                break
            except Exception:
                pass

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
# HTML ФРОНТЕНД
# ============================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
    <title>Global Chat</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0e0e10; height: 100vh; overflow: hidden; color: #e4e4e7; }
        
        /* Login */
        #login-screen { display: flex; align-items: center; justify-content: center; min-height: 100vh; background: linear-gradient(135deg, #0e0e10 0%, #1a1a2e 100%); }
        .login-card { background: rgba(30, 30, 46, 0.9); backdrop-filter: blur(20px); border: 1px solid rgba(255,255,255,0.06); padding: 2.5rem; border-radius: 1.5rem; width: 100%; max-width: 400px; box-shadow: 0 25px 60px rgba(0,0,0,0.8); }
        .login-card h1 { font-size: 2.2rem; font-weight: 700; background: linear-gradient(135deg, #5865f2, #a78bfa); -webkit-background-clip: text; -webkit-text-fill-color: transparent; text-align: center; margin-bottom: 0.5rem; }
        .login-card .subtitle { color: #8e8ea0; text-align: center; margin-bottom: 2rem; font-size: 0.95rem; }
        .login-card input { width: 100%; padding: 0.75rem 1rem; background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.08); border-radius: 0.75rem; color: #e4e4e7; font-size: 1rem; transition: all 0.3s; margin-bottom: 0.5rem; }
        .login-card input:focus { outline: none; border-color: #5865f2; box-shadow: 0 0 0 3px rgba(88,101,242,0.15); }
        .login-card input::placeholder { color: #6e6e80; }
        .login-card .btn-join { width: 100%; padding: 0.75rem; background: linear-gradient(135deg, #5865f2, #7c6df0); color: white; border: none; border-radius: 0.75rem; font-size: 1rem; font-weight: 600; cursor: pointer; transition: all 0.3s; margin-top: 0.5rem; }
        .login-card .btn-join:hover { transform: translateY(-2px); box-shadow: 0 8px 25px rgba(88,101,242,0.3); }
        .login-card .btn-link { background: none; border: none; color: #8e8ea0; font-size: 0.85rem; cursor: pointer; transition: color 0.3s; margin-top: 0.75rem; display: block; width: 100%; text-align: center; }
        .login-card .btn-link:hover { color: #e4e4e7; }
        .login-card .error { color: #f87171; font-size: 0.85rem; margin-top: 0.25rem; display: none; }
        
        /* Chat */
        #chat-screen { display: none; flex-direction: column; height: 100vh; background: #0e0e10; }
        .chat-header { background: rgba(30,30,46,0.95); backdrop-filter: blur(10px); border-bottom: 1px solid rgba(255,255,255,0.05); padding: 0.75rem 1.5rem; display: flex; justify-content: space-between; align-items: center; flex-shrink: 0; z-index: 10; flex-wrap: wrap; gap: 0.5rem; }
        .chat-header .left { display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; }
        .chat-header .title { font-size: 1.1rem; font-weight: 600; color: #e4e4e7; }
        .chat-header .online-badge { display: flex; align-items: center; gap: 0.4rem; background: rgba(88,101,242,0.15); color: #a78bfa; font-size: 0.75rem; padding: 0.25rem 0.75rem; border-radius: 9999px; font-weight: 500; }
        .chat-header .online-badge .dot { width: 6px; height: 6px; border-radius: 50%; background: #4ade80; animation: pulse-dot 2s infinite; }
        @keyframes pulse-dot { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
        .chat-header .right { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
        .chat-header .right .user-name { color: #8e8ea0; font-size: 0.85rem; }
        .chat-header .right .user-name span { color: #e4e4e7; font-weight: 500; }
        .chat-header .right .icon-btn { background: none; border: none; color: #8e8ea0; font-size: 1.1rem; cursor: pointer; padding: 0.4rem; border-radius: 0.5rem; transition: all 0.3s; }
        .chat-header .right .icon-btn:hover { color: #e4e4e7; background: rgba(255,255,255,0.05); }
        .chat-header .right .leave-btn { background: rgba(239,68,68,0.15); color: #f87171; padding: 0.25rem 0.75rem; border-radius: 9999px; border: none; font-size: 0.75rem; cursor: pointer; transition: all 0.3s; }
        .chat-header .right .leave-btn:hover { background: rgba(239,68,68,0.25); }
        
        .connection-status { padding: 0.4rem 1.5rem; text-align: center; font-size: 0.8rem; font-weight: 500; display: none; flex-shrink: 0; }
        .connection-status.connected { background: rgba(74,222,128,0.08); color: #4ade80; border-bottom: 1px solid rgba(74,222,128,0.1); display: block; }
        .connection-status.disconnected { background: rgba(239,68,68,0.08); color: #f87171; border-bottom: 1px solid rgba(239,68,68,0.1); display: block; }
        
        .messages-container { flex: 1; overflow-y: auto; padding: 1rem 1.5rem; display: flex; flex-direction: column; gap: 0.15rem; scroll-behavior: smooth; }
        .messages-container::-webkit-scrollbar { width: 5px; }
        .messages-container::-webkit-scrollbar-track { background: transparent; }
        .messages-container::-webkit-scrollbar-thumb { background: #2a2a3a; border-radius: 9999px; }
        .messages-container::-webkit-scrollbar-thumb:hover { background: #3a3a4a; }
        
        .message { display: flex; align-items: flex-start; gap: 0.6rem; padding: 0.2rem 0; animation: fadeIn 0.15s ease-in; position: relative; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }
        .message .avatar { width: 32px; height: 32px; border-radius: 50%; flex-shrink: 0; display: flex; align-items: center; justify-content: center; font-weight: 600; font-size: 0.75rem; color: white; text-transform: uppercase; overflow: hidden; background: #2a2a3a; cursor: pointer; }
        .message .avatar img { width: 100%; height: 100%; object-fit: cover; }
        .message .content { flex: 1; min-width: 0; padding-top: 0.15rem; }
        .message .content .msg-header { display: flex; align-items: baseline; gap: 0.5rem; flex-wrap: wrap; }
        .message .content .msg-nickname { font-weight: 600; font-size: 0.85rem; cursor: pointer; transition: color 0.2s; }
        .message .content .msg-nickname:hover { text-decoration: underline; }
        .message .content .msg-time { color: #5e5e70; font-size: 0.65rem; }
        .message .content .msg-text { color: #d4d4d8; font-size: 0.92rem; word-wrap: break-word; overflow-wrap: break-word; line-height: 1.4; }
        .message .content .msg-text.deleted { color: #6e6e80; font-style: italic; }
        .message .content .msg-actions { display: flex; gap: 0.5rem; margin-top: 0.15rem; opacity: 0; transition: opacity 0.2s; }
        .message:hover .content .msg-actions { opacity: 1; }
        .message .content .msg-actions button { background: none; border: none; color: #6e6e80; font-size: 0.7rem; cursor: pointer; padding: 0.1rem 0.3rem; border-radius: 0.25rem; transition: all 0.2s; }
        .message .content .msg-actions button:hover { color: #e4e4e7; background: rgba(255,255,255,0.05); }
        .message .content .msg-actions .delete-btn:hover { color: #f87171; }
        .message .content .msg-actions .edit-btn:hover { color: #60a5fa; }
        .message .content .msg-edit-indicator { color: #6e6e80; font-size: 0.65rem; font-style: italic; margin-left: 0.5rem; }
        .message.system { justify-content: center; }
        .message.system .content .msg-text { color: #6e6e80; font-size: 0.8rem; font-style: italic; text-align: center; }
        .message.system .avatar { display: none; }
        .message.system .content .msg-actions { display: none; }
        
        .file-attachment { display: inline-flex; align-items: center; gap: 0.5rem; background: rgba(255,255,255,0.04); padding: 0.4rem 0.75rem; border-radius: 0.5rem; margin-top: 0.2rem; cursor: pointer; transition: all 0.3s; border: 1px solid rgba(255,255,255,0.04); }
        .file-attachment:hover { background: rgba(255,255,255,0.08); }
        .file-attachment i { font-size: 1rem; color: #8e8ea0; }
        .file-attachment .file-name { color: #d4d4d8; font-size: 0.8rem; }
        .file-attachment .file-size { color: #6e6e80; font-size: 0.7rem; }
        .file-attachment .download-link { color: #5865f2; text-decoration: none; font-size: 0.8rem; }
        .file-attachment .download-link:hover { color: #7c6df0; }
        .message-image { max-width: 280px; max-height: 280px; border-radius: 0.5rem; cursor: pointer; margin-top: 0.2rem; transition: transform 0.2s; border: 1px solid rgba(255,255,255,0.04); }
        .message-image:hover { transform: scale(1.02); }
        
        .input-area { background: rgba(30,30,46,0.95); backdrop-filter: blur(10px); border-top: 1px solid rgba(255,255,255,0.04); padding: 0.6rem 1rem; display: flex; gap: 0.5rem; align-items: center; flex-shrink: 0; }
        .input-area .input-wrapper { flex: 1; display: flex; align-items: center; background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.06); border-radius: 0.75rem; padding: 0 0.5rem; transition: all 0.3s; }
        .input-area .input-wrapper:focus-within { border-color: #5865f2; box-shadow: 0 0 0 3px rgba(88,101,242,0.1); }
        .input-area .input-wrapper .icon-btn { background: none; border: none; color: #6e6e80; font-size: 1rem; cursor: pointer; padding: 0.4rem; border-radius: 0.4rem; transition: all 0.3s; }
        .input-area .input-wrapper .icon-btn:hover { color: #e4e4e7; background: rgba(255,255,255,0.05); }
        .input-area .input-wrapper input { flex: 1; background: transparent; border: none; color: #e4e4e7; padding: 0.6rem 0.3rem; font-size: 0.92rem; min-width: 0; }
        .input-area .input-wrapper input:focus { outline: none; }
        .input-area .input-wrapper input::placeholder { color: #6e6e80; }
        .input-area .input-wrapper input:disabled { opacity: 0.5; cursor: not-allowed; }
        .input-area .btn-send { background: linear-gradient(135deg, #5865f2, #7c6df0); color: white; padding: 0.6rem 1.2rem; border: none; border-radius: 0.75rem; font-weight: 600; font-size: 0.9rem; cursor: pointer; transition: all 0.3s; white-space: nowrap; }
        .input-area .btn-send:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 4px 15px rgba(88,101,242,0.3); }
        .input-area .btn-send:disabled { opacity: 0.4; cursor: not-allowed; transform: none; }
        
        .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); backdrop-filter: blur(8px); z-index: 1000; justify-content: center; align-items: center; }
        .modal.active { display: flex; }
        .modal-content { max-width: 90%; max-height: 90%; border-radius: 0.5rem; }
        .modal-close { position: fixed; top: 20px; right: 30px; color: #8e8ea0; font-size: 2rem; cursor: pointer; z-index: 1001; transition: color 0.3s; }
        .modal-close:hover { color: #e4e4e7; }
        
        .profile-modal .modal-content { background: #1e1e2e; color: #e4e4e7; padding: 2rem; border-radius: 1rem; max-width: 500px; width: 90%; max-height: 90vh; overflow-y: auto; }
        .profile-modal .modal-content h2 { font-size: 1.5rem; font-weight: 700; margin-bottom: 1.5rem; }
        .profile-modal .form-group { margin-bottom: 1rem; }
        .profile-modal .form-group label { display: block; color: #8e8ea0; font-size: 0.8rem; margin-bottom: 0.25rem; }
        .profile-modal .form-group input, .profile-modal .form-group textarea { width: 100%; background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.06); color: #e4e4e7; padding: 0.5rem 0.75rem; border-radius: 0.5rem; font-size: 0.9rem; transition: all 0.3s; }
        .profile-modal .form-group input:focus, .profile-modal .form-group textarea:focus { outline: none; border-color: #5865f2; }
        .profile-modal .avatar-upload { display: flex; align-items: center; gap: 1rem; margin-bottom: 1.5rem; }
        .profile-modal .current-avatar { width: 64px; height: 64px; border-radius: 50%; object-fit: cover; background: #2a2a3a; }
        .profile-modal .btn { padding: 0.4rem 1.2rem; border-radius: 0.5rem; border: none; font-weight: 500; font-size: 0.85rem; cursor: pointer; transition: all 0.3s; }
        .profile-modal .btn-primary { background: linear-gradient(135deg, #5865f2, #7c6df0); color: white; }
        .profile-modal .btn-primary:hover { transform: translateY(-1px); box-shadow: 0 4px 15px rgba(88,101,242,0.3); }
        .profile-modal .btn-secondary { background: rgba(255,255,255,0.06); color: #e4e4e7; }
        .profile-modal .btn-secondary:hover { background: rgba(255,255,255,0.1); }
        .profile-modal .btn-sm { padding: 0.25rem 0.75rem; font-size: 0.75rem; }
        .file-input-wrapper { position: relative; overflow: hidden; display: inline-block; }
        .file-input-wrapper input[type=file] { position: absolute; left: 0; top: 0; opacity: 0; width: 100%; height: 100%; cursor: pointer; }
        .profile-modal .btn-group { display: flex; gap: 0.75rem; margin-top: 1.5rem; }
        #profile-message { margin-top: 0.75rem; font-size: 0.85rem; }
        
        /* User profile view modal */
        .user-profile-modal .modal-content { background: #1e1e2e; color: #e4e4e7; padding: 2rem; border-radius: 1rem; max-width: 400px; width: 90%; text-align: center; }
        .user-profile-modal .modal-content .big-avatar { width: 80px; height: 80px; border-radius: 50%; object-fit: cover; margin: 0 auto 1rem; background: #2a2a3a; }
        .user-profile-modal .modal-content .user-name { font-size: 1.3rem; font-weight: 600; margin-bottom: 0.25rem; }
        .user-profile-modal .modal-content .user-status { font-size: 0.85rem; color: #8e8ea0; margin-bottom: 1rem; }
        .user-profile-modal .modal-content .user-info { color: #d4d4d8; font-size: 0.9rem; margin-bottom: 0.5rem; }
        .user-profile-modal .modal-content .user-contact { color: #8e8ea0; font-size: 0.85rem; }
        .user-profile-modal .modal-content .user-contact i { margin-right: 0.5rem; }
        .user-profile-modal .modal-content .btn-close { margin-top: 1.5rem; }
        
        /* Mention highlight */
        .mention-highlight { background: rgba(88,101,242,0.2); padding: 0.1rem 0.3rem; border-radius: 0.25rem; }
        .message.mentioned { background: rgba(88,101,242,0.05); border-left: 3px solid #5865f2; padding-left: 0.5rem; }
        
        .toast { position: fixed; top: 20px; right: 20px; z-index: 2000; background: #1e1e2e; border: 1px solid rgba(255,255,255,0.1); padding: 1rem 1.5rem; border-radius: 0.75rem; max-width: 350px; box-shadow: 0 10px 40px rgba(0,0,0,0.5); animation: slideIn 0.3s ease; }
        .toast .toast-title { font-weight: 600; font-size: 0.9rem; color: #a78bfa; }
        .toast .toast-text { font-size: 0.85rem; color: #d4d4d8; margin-top: 0.25rem; }
        @keyframes slideIn { from { opacity: 0; transform: translateX(50px); } to { opacity: 1; transform: translateX(0); } }
        
        @media (max-width: 640px) {
            .chat-header { padding: 0.5rem 0.75rem; }
            .chat-header .title { font-size: 0.95rem; }
            .chat-header .right .user-name { font-size: 0.75rem; }
            .messages-container { padding: 0.5rem 0.75rem; }
            .input-area { padding: 0.4rem 0.5rem; gap: 0.3rem; }
            .input-area .input-wrapper input { font-size: 0.85rem; padding: 0.4rem 0.2rem; }
            .input-area .btn-send { padding: 0.4rem 0.8rem; font-size: 0.8rem; }
            .message .avatar { width: 28px; height: 28px; font-size: 0.65rem; }
            .message .content .msg-text { font-size: 0.85rem; }
            .message-image { max-width: 200px; max-height: 200px; }
            .login-card { padding: 1.5rem; margin: 0 1rem; }
            .profile-modal .modal-content { padding: 1.5rem; }
        }
    </style>
</head>
<body>
    <!-- Login Screen -->
    <div id="login-screen">
        <div class="login-card">
            <h1>💬 Global Chat</h1>
            <p class="subtitle">Войдите или зарегистрируйтесь</p>
            <input type="text" id="login-nickname" placeholder="Никнейм" maxlength="20" autofocus>
            <input type="password" id="login-password" placeholder="Пароль (минимум 4 символа для регистрации)">
            <div id="login-error" class="error"></div>
            <button class="btn-join" id="login-btn">Войти</button>
            <button class="btn-link" id="switch-to-register">Нет аккаунта? Зарегистрироваться</button>
        </div>
    </div>

    <!-- Register Screen -->
    <div id="register-screen" style="display:none;">
        <div class="login-card">
            <h1>📝 Регистрация</h1>
            <p class="subtitle">Создайте новый аккаунт</p>
            <input type="text" id="reg-nickname" placeholder="Никнейм" maxlength="20">
            <input type="password" id="reg-password" placeholder="Пароль (минимум 4 символа)">
            <input type="password" id="reg-password-confirm" placeholder="Подтвердите пароль">
            <input type="email" id="reg-email" placeholder="Email (опционально)">
            <div id="reg-error" class="error"></div>
            <button class="btn-join" id="register-btn">Зарегистрироваться</button>
            <button class="btn-link" id="switch-to-login">← Уже есть аккаунт? Войти</button>
        </div>
    </div>

    <!-- Chat Screen -->
    <div id="chat-screen">
        <div class="chat-header">
            <div class="left">
                <span class="title">💬 Global Chat</span>
                <span class="online-badge">
                    <span class="dot"></span>
                    <span id="online-count">0</span>
                </span>
            </div>
            <div class="right">
                <span class="user-name">Вы: <span id="current-nickname">—</span></span>
                <button class="icon-btn" id="profile-btn" title="Профиль"><i class="fas fa-user-circle"></i></button>
                <button class="leave-btn" id="leave-btn">Выйти</button>
            </div>
        </div>

        <div class="connection-status" id="connection-status">⚠️ Нет соединения с сервером...</div>
        <div class="messages-container" id="messages-container"></div>

        <div class="input-area">
            <div class="input-wrapper">
                <button class="icon-btn" id="attach-btn" title="Прикрепить файл"><i class="fas fa-paperclip"></i></button>
                <button class="icon-btn" id="image-btn" title="Прикрепить изображение"><i class="fas fa-image"></i></button>
                <input type="text" id="message-input" placeholder="Введите сообщение... (используйте @ник для упоминания)" disabled>
            </div>
            <button class="btn-send" id="send-btn" disabled>Отправить</button>
        </div>
    </div>

    <!-- Image Modal -->
    <div class="modal" id="image-modal">
        <span class="modal-close" id="modal-close">&times;</span>
        <img class="modal-content" id="modal-image">
    </div>

    <!-- Profile Modal (self) -->
    <div class="modal profile-modal" id="profile-modal">
        <div class="modal-content">
            <h2>👤 Мой профиль</h2>
            <div class="avatar-upload">
                <img id="profile-avatar" class="current-avatar" src="" alt="Avatar">
                <div>
                    <div class="file-input-wrapper">
                        <button class="btn btn-primary btn-sm">📷 Сменить аватар</button>
                        <input type="file" id="avatar-input" accept="image/*">
                    </div>
                    <span style="color: #6e6e80; font-size: 0.7rem;">Максимум 2MB</span>
                </div>
            </div>
            <div class="form-group">
                <label>Информация о себе</label>
                <textarea id="profile-info" rows="2" placeholder="Расскажите о себе..."></textarea>
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
                <label>Изменить пароль</label>
                <input type="password" id="profile-password" placeholder="Новый пароль (минимум 4 символа)">
                <span style="color: #6e6e80; font-size: 0.7rem;">Оставьте пустым, чтобы не менять</span>
            </div>
            <div class="btn-group">
                <button class="btn btn-primary" id="profile-save-btn">💾 Сохранить</button>
                <button class="btn btn-secondary" id="profile-close-btn">Закрыть</button>
            </div>
            <div id="profile-message"></div>
        </div>
    </div>

    <!-- User Profile View Modal (other users) -->
    <div class="modal user-profile-modal" id="user-profile-modal">
        <div class="modal-content">
            <img id="user-profile-avatar" class="big-avatar" src="" alt="Avatar">
            <div class="user-name" id="user-profile-name">—</div>
            <div class="user-status" id="user-profile-status">—</div>
            <div class="user-info" id="user-profile-info"></div>
            <div class="user-contact" id="user-profile-phone"><i class="fas fa-phone"></i> <span>—</span></div>
            <div class="user-contact" id="user-profile-email"><i class="fas fa-envelope"></i> <span>—</span></div>
            <button class="btn btn-secondary btn-close" id="user-profile-close">Закрыть</button>
        </div>
    </div>

    <!-- Hidden file inputs -->
    <input type="file" id="file-input" style="display:none" multiple>
    <input type="file" id="image-input" style="display:none" accept="image/*" multiple>

    <script>
        // ============================================
        // STATE
        // ============================================
        const state = {
            ws: null,
            nickname: '',
            connected: false,
            reconnecting: false,
            reconnectAttempts: 0,
            maxReconnectAttempts: 10,
            profile: null,
            editingMessageId: null,
        };

        // ============================================
        // DOM REFS
        // ============================================
        const $ = id => document.getElementById(id);
        
        // Login/Register
        const loginScreen = $('login-screen');
        const registerScreen = $('register-screen');
        const chatScreen = $('chat-screen');
        const loginNickname = $('login-nickname');
        const loginPassword = $('login-password');
        const loginBtn = $('login-btn');
        const loginError = $('login-error');
        const switchToRegister = $('switch-to-register');
        const switchToLogin = $('switch-to-login');
        const regNickname = $('reg-nickname');
        const regPassword = $('reg-password');
        const regPasswordConfirm = $('reg-password-confirm');
        const regEmail = $('reg-email');
        const regBtn = $('register-btn');
        const regError = $('reg-error');
        
        // Chat
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
        
        // Profile Modal
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
        
        // User Profile View
        const userProfileModal = $('user-profile-modal');
        const userProfileClose = $('user-profile-close');
        const userProfileAvatar = $('user-profile-avatar');
        const userProfileName = $('user-profile-name');
        const userProfileStatus = $('user-profile-status');
        const userProfileInfo = $('user-profile-info');
        const userProfilePhone = $('user-profile-phone');
        const userProfileEmail = $('user-profile-email');
        
        // Image Modal
        const imageModal = $('image-modal');
        const modalImage = $('modal-image');
        const modalClose = $('modal-close');

        // ============================================
        // UTILITIES
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

        function formatDate(timestamp) {
            const date = new Date(timestamp * 1000);
            return date.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' });
        }

        function formatFileSize(bytes) {
            if (bytes < 1024) return bytes + ' B';
            if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
            return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
        }

        function scrollToBottom() {
            messagesContainer.scrollTop = messagesContainer.scrollHeight;
        }

        function getFileIcon(ext) {
            const icons = {
                '.pdf': 'fa-file-pdf',
                '.doc': 'fa-file-word',
                '.docx': 'fa-file-word',
                '.txt': 'fa-file-alt',
                '.zip': 'fa-file-archive',
                '.rar': 'fa-file-archive',
            };
            return icons[ext] || 'fa-file';
        }

        function getFileColor(ext) {
            const colors = {
                '.pdf': '#ef4444',
                '.doc': '#3b82f6',
                '.docx': '#3b82f6',
                '.txt': '#8b5cf6',
                '.zip': '#f59e0b',
                '.rar': '#f59e0b',
            };
            return colors[ext] || '#6e6e80';
        }

        function extractMentions(text) {
            const mentions = [];
            const regex = /@(\w+)/g;
            let match;
            while ((match = regex.exec(text)) !== null) {
                mentions.push(match[1]);
            }
            return mentions;
        }

        function showToast(title, text, duration = 5000) {
            const existing = document.querySelector('.toast');
            if (existing) existing.remove();
            
            const toast = document.createElement('div');
            toast.className = 'toast';
            toast.innerHTML = `
                <div class="toast-title">${title}</div>
                <div class="toast-text">${text}</div>
            `;
            document.body.appendChild(toast);
            setTimeout(() => toast.remove(), duration);
        }

        // ============================================
        // RENDER MESSAGE
        // ============================================
        function renderMessage(message) {
            const div = document.createElement('div');
            div.className = `message ${message.is_system ? 'system' : ''}`;
            div.dataset.messageId = message.id;
            
            // Avatar
            const avatar = document.createElement('div');
            avatar.className = 'avatar';
            
            if (!message.is_system) {
                if (message.nickname === state.nickname && state.profile && state.profile.avatar) {
                    const img = document.createElement('img');
                    img.src = state.profile.avatar;
                    avatar.appendChild(img);
                } else {
                    avatar.textContent = message.nickname.charAt(0).toUpperCase();
                    avatar.style.background = getColorFromString(message.nickname);
                }
                avatar.title = `Нажмите для просмотра профиля ${message.nickname}`;
                avatar.onclick = () => viewUserProfile(message.nickname);
            }
            div.appendChild(avatar);

            // Content
            const content = document.createElement('div');
            content.className = 'content';

            if (!message.is_system) {
                const header = document.createElement('div');
                header.className = 'msg-header';
                
                const name = document.createElement('span');
                name.className = 'msg-nickname';
                name.textContent = message.nickname;
                name.style.color = getColorFromString(message.nickname);
                name.onclick = () => viewUserProfile(message.nickname);
                header.appendChild(name);
                
                const time = document.createElement('span');
                time.className = 'msg-time';
                time.textContent = formatTime(message.timestamp);
                header.appendChild(time);
                
                if (message.edited) {
                    const edited = document.createElement('span');
                    edited.className = 'msg-edit-indicator';
                    edited.textContent = '(ред.)';
                    header.appendChild(edited);
                }
                
                content.appendChild(header);
            }

            const text = document.createElement('div');
            text.className = `msg-text ${message.deleted ? 'deleted' : ''}`;
            
            if (message.deleted) {
                text.textContent = message.text;
            } else {
                // Обработка упоминаний
                const parts = message.text.split(/(@\w+)/g);
                for (const part of parts) {
                    if (part.startsWith('@')) {
                        const mention = document.createElement('span');
                        mention.className = 'mention-highlight';
                        mention.textContent = part;
                        text.appendChild(mention);
                    } else {
                        text.appendChild(document.createTextNode(part));
                    }
                }
            }
            content.appendChild(text);

            // File
            if (message.file && !message.deleted) {
                const fileDiv = document.createElement('div');
                fileDiv.className = 'file-attachment';
                
                if (message.file.is_image) {
                    const img = document.createElement('img');
                    img.className = 'message-image';
                    img.src = message.file.url;
                    img.alt = message.file.filename;
                    img.onclick = () => openImageModal(message.file.url);
                    fileDiv.appendChild(img);
                } else {
                    const icon = document.createElement('i');
                    icon.className = `fas ${getFileIcon(message.file.extension)}`;
                    icon.style.color = getFileColor(message.file.extension);
                    fileDiv.appendChild(icon);
                    
                    const info = document.createElement('span');
                    info.className = 'file-name';
                    info.textContent = message.file.filename;
                    fileDiv.appendChild(info);
                    
                    const size = document.createElement('span');
                    size.className = 'file-size';
                    size.textContent = formatFileSize(message.file.size);
                    fileDiv.appendChild(size);
                    
                    const link = document.createElement('a');
                    link.className = 'download-link';
                    link.href = message.file.url;
                    link.download = message.file.filename;
                    link.innerHTML = '<i class="fas fa-download"></i>';
                    fileDiv.appendChild(link);
                }
                
                content.appendChild(fileDiv);
            }

            // Actions (only for own messages, not system, not deleted)
            if (!message.is_system && message.nickname === state.nickname && !message.deleted) {
                const actions = document.createElement('div');
                actions.className = 'msg-actions';
                
                const editBtn = document.createElement('button');
                editBtn.className = 'edit-btn';
                editBtn.innerHTML = '<i class="fas fa-edit"></i>';
                editBtn.title = 'Редактировать';
                editBtn.onclick = (e) => {
                    e.stopPropagation();
                    startEditing(message.id, message.text);
                };
                actions.appendChild(editBtn);
                
                const deleteBtn = document.createElement('button');
                deleteBtn.className = 'delete-btn';
                deleteBtn.innerHTML = '<i class="fas fa-trash"></i>';
                deleteBtn.title = 'Удалить';
                deleteBtn.onclick = (e) => {
                    e.stopPropagation();
                    if (confirm('Удалить это сообщение?')) {
                        sendMessage({ type: 'delete', message_id: message.id });
                    }
                };
                actions.appendChild(deleteBtn);
                
                content.appendChild(actions);
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
            // Проверяем, не существует ли уже такое сообщение
            const existing = messagesContainer.querySelector(`[data-message-id="${message.id}"]`);
            if (existing) {
                existing.replaceWith(renderMessage(message));
                return;
            }
            messagesContainer.appendChild(renderMessage(message));
            scrollToBottom();
            
            // Проверяем упоминания
            if (!message.is_system && message.text) {
                const mentions = extractMentions(message.text);
                if (mentions.includes(state.nickname)) {
                    showToast('🔔 Вас упомянули', `${message.nickname}: ${message.text}`);
                    // Подсвечиваем сообщение
                    const msgEl = messagesContainer.querySelector(`[data-message-id="${message.id}"]`);
                    if (msgEl) {
                        msgEl.classList.add('mentioned');
                        setTimeout(() => msgEl.classList.remove('mentioned'), 3000);
                    }
                }
            }
        }

        function updateMessage(message) {
            const existing = messagesContainer.querySelector(`[data-message-id="${message.id}"]`);
            if (existing) {
                existing.replaceWith(renderMessage(message));
            }
        }

        function deleteMessage(message) {
            const existing = messagesContainer.querySelector(`[data-message-id="${message.id}"]`);
            if (existing) {
                existing.replaceWith(renderMessage(message));
            }
        }

        // ============================================
        // EDITING
        // ============================================
        function startEditing(messageId, currentText) {
            state.editingMessageId = messageId;
            messageInput.value = currentText;
            messageInput.focus();
            messageInput.classList.add('editing');
            sendBtn.textContent = '💾 Сохранить';
            sendBtn.style.background = 'linear-gradient(135deg, #f59e0b, #fbbf24)';
        }

        function cancelEditing() {
            state.editingMessageId = null;
            messageInput.value = '';
            messageInput.classList.remove('editing');
            sendBtn.textContent = 'Отправить';
            sendBtn.style.background = '';
        }

        // ============================================
        // MODALS
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
        // USER PROFILE VIEW
        // ============================================
        async function viewUserProfile(nickname) {
            try {
                const response = await fetch(`/api/profile/${encodeURIComponent(nickname)}`);
                if (!response.ok) throw new Error('User not found');
                const profile = await response.json();
                
                userProfileAvatar.src = profile.avatar || `data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="80" height="80"%3E%3Ccircle cx="40" cy="40" r="40" fill="%232a2a3a"/%3E%3Ctext x="40" y="48" text-anchor="middle" fill="%23e4e4e7" font-size="36" font-weight="bold"%3E${nickname.charAt(0).toUpperCase()}%3C/text%3E%3C/svg%3E`;
                userProfileName.textContent = nickname;
                userProfileName.style.color = getColorFromString(nickname);
                userProfileStatus.textContent = profile.is_online ? '🟢 Онлайн' : '⚫ Офлайн';
                userProfileStatus.style.color = profile.is_online ? '#4ade80' : '#6e6e80';
                userProfileInfo.textContent = profile.info || 'Информация не указана';
                userProfilePhone.querySelector('span').textContent = profile.phone || 'Не указан';
                userProfileEmail.querySelector('span').textContent = profile.email || 'Не указан';
                
                userProfileModal.classList.add('active');
            } catch (error) {
                console.error('Error loading profile:', error);
                showToast('❌ Ошибка', 'Не удалось загрузить профиль');
            }
        }

        userProfileClose.onclick = () => userProfileModal.classList.remove('active');
        userProfileModal.onclick = (e) => {
            if (e.target === userProfileModal) userProfileModal.classList.remove('active');
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
                console.error('WebSocket error:', e);
                scheduleReconnect();
                return;
            }

            state.ws.onopen = function() {
                console.log('Connected');
                state.connected = true;
                state.reconnecting = false;
                state.reconnectAttempts = 0;
                updateConnectionStatus(true);
                enableChat(true);
                sendMessage({ type: 'join', nickname: state.nickname, password: state.password || '' });
            };

            state.ws.onmessage = function(event) {
                try {
                    const data = JSON.parse(event.data);
                    handleWebSocketMessage(data);
                } catch (e) {
                    console.error('Parse error:', e);
                }
            };

            state.ws.onclose = function() {
                console.log('Disconnected');
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
                case 'delete':
                    deleteMessage(data.message);
                    break;
                case 'edit':
                    updateMessage(data.message);
                    break;
                case 'online_count':
                    onlineCount.textContent = data.count;
                    break;
                case 'mention':
                    showToast('🔔 Вас упомянули', `${data.from}: ${data.text}`);
                    break;
                case 'error':
                    console.error('Server error:', data.message);
                    showToast('❌ Ошибка', data.message);
                    break;
            }
        }

        // ============================================
        // FILE UPLOAD
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
                        showToast('❌ Ошибка загрузки', error.detail || 'Не удалось загрузить файл');
                        continue;
                    }

                    const fileInfo = await response.json();
                    sendMessage({
                        type: 'message',
                        text: isImage ? '📷 Изображение' : `📎 ${fileInfo.filename}`,
                        file: fileInfo
                    });

                } catch (error) {
                    console.error('Upload error:', error);
                    showToast('❌ Ошибка', 'Не удалось загрузить файл');
                }
            }
        }

        // ============================================
        // PROFILE
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
            profileAvatar.src = state.profile.avatar || `data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="64" height="64"%3E%3Ccircle cx="32" cy="32" r="32" fill="%232a2a3a"/%3E%3Ctext x="32" y="38" text-anchor="middle" fill="%23e4e4e7" font-size="28" font-weight="bold"%3E${state.nickname.charAt(0).toUpperCase()}%3C/text%3E%3C/svg%3E`;
            profileInfo.value = state.profile.info || '';
            profilePhone.value = state.profile.phone || '';
            profileEmail.value = state.profile.email || '';
        }

        async function saveProfile() {
            const updates = {
                info: profileInfo.value,
                phone: profilePhone.value,
                email: profileEmail.value,
            };

            let success = true;
            for (const [field, value] of Object.entries(updates)) {
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
                    }
                } catch (error) {
                    success = false;
                }
            }

            // Изменение пароля
            const newPassword = profilePassword.value.trim();
            if (newPassword) {
                if (newPassword.length < 4) {
                    profileMessage.textContent = '❌ Пароль должен быть минимум 4 символа';
                    profileMessage.style.color = '#f87171';
                    return;
                }
                const formData = new FormData();
                formData.append('nickname', state.nickname);
                formData.append('field', 'password');
                formData.append('value', newPassword);
                
                try {
                    const response = await fetch('/api/profile/update', {
                        method: 'POST',
                        body: formData
                    });
                    if (!response.ok) success = false;
                } catch (error) {
                    success = false;
                }
            }

            if (success) {
                profileMessage.textContent = '✅ Профиль сохранён!';
                profileMessage.style.color = '#4ade80';
                await loadProfile(state.nickname);
                profilePassword.value = '';
                setTimeout(() => { profileMessage.textContent = ''; }, 3000);
            } else {
                profileMessage.textContent = '❌ Ошибка сохранения';
                profileMessage.style.color = '#f87171';
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
                    showToast('❌ Ошибка', error.detail || 'Не удалось загрузить аватар');
                    return;
                }

                const result = await response.json();
                state.profile.avatar = result.avatar;
                updateProfileUI();
                
                profileMessage.textContent = '✅ Аватар обновлён!';
                profileMessage.style.color = '#4ade80';
                setTimeout(() => { profileMessage.textContent = ''; }, 3000);

            } catch (error) {
                console.error('Avatar upload error:', error);
                showToast('❌ Ошибка', 'Не удалось загрузить аватар');
            }
        }

        // ============================================
        // UI UPDATES
        // ============================================
        function updateConnectionStatus(connected) {
            if (connected) {
                connectionStatus.className = 'connection-status connected';
                connectionStatus.textContent = '✅ Соединение установлено';
                setTimeout(() => {
                    connectionStatus.style.display = 'none';
                }, 2000);
            } else {
                connectionStatus.className = 'connection-status disconnected';
                connectionStatus.textContent = '⚠️ Нет соединения с сервером...';
                connectionStatus.style.display = 'block';
            }
        }

        function enableChat(enabled) {
            messageInput.disabled = !enabled;
            sendBtn.disabled = !enabled;
            if (enabled) messageInput.focus();
        }

        // ============================================
        // AUTH
        // ============================================
        function handleLogin() {
            const nickname = loginNickname.value.trim();
            const password = loginPassword.value;

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
            if (!password) {
                loginError.textContent = 'Введите пароль';
                loginError.style.display = 'block';
                return;
            }

            loginError.style.display = 'none';
            state.nickname = nickname;
            state.password = password;

            loginScreen.style.display = 'none';
            registerScreen.style.display = 'none';
            chatScreen.style.display = 'flex';
            currentNickname.textContent = nickname;

            loadProfile(nickname);
            connectWebSocket();
        }

        function handleRegister() {
            const nickname = regNickname.value.trim();
            const password = regPassword.value;
            const passwordConfirm = regPasswordConfirm.value;
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
            if (password.length < 4) {
                regError.textContent = 'Пароль должен быть минимум 4 символа';
                regError.style.display = 'block';
                return;
            }
            if (password !== passwordConfirm) {
                regError.textContent = 'Пароли не совпадают';
                regError.style.display = 'block';
                return;
            }

            regError.style.display = 'none';
            state.nickname = nickname;
            state.password = password;

            loginScreen.style.display = 'none';
            registerScreen.style.display = 'none';
            chatScreen.style.display = 'flex';
            currentNickname.textContent = nickname;

            loadProfile(nickname);
            connectWebSocket();
        }

        function leaveChat() {
            if (state.ws) state.ws.close();
            state.connected = false;
            state.reconnecting = false;
            state.nickname = '';
            state.password = '';
            state.editingMessageId = null;

            chatScreen.style.display = 'none';
            loginScreen.style.display = 'flex';
            loginNickname.value = '';
            loginPassword.value = '';
            loginNickname.focus();
            cancelEditing();
        }

        // ============================================
        // SEND MESSAGE
        // ============================================
        function sendChatMessage() {
            let text = messageInput.value.trim();
            if (!text) return;

            // Проверяем, редактируем ли мы сообщение
            if (state.editingMessageId) {
                sendMessage({
                    type: 'edit',
                    message_id: state.editingMessageId,
                    text: text
                });
                cancelEditing();
                messageInput.value = '';
                return;
            }

            // Проверяем упоминания
            const mentions = extractMentions(text);
            for (const mention of mentions) {
                if (mention === 'all') {
                    sendMessage({
                        type: 'mention',
                        target: 'all',
                        text: text
                    });
                    showToast('🔔 Уведомление', 'Вы упомянули всех (@all)');
                } else if (mention !== state.nickname) {
                    sendMessage({
                        type: 'mention',
                        target: mention,
                        text: text
                    });
                }
            }

            sendMessage({ type: 'message', text: text });
            messageInput.value = '';
        }

        // ============================================
        // EVENT LISTENERS
        // ============================================
        // Login/Register
        loginBtn.addEventListener('click', handleLogin);
        loginNickname.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') loginBtn.click();
        });
        loginPassword.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') loginBtn.click();
        });

        switchToRegister.addEventListener('click', () => {
            loginScreen.style.display = 'none';
            registerScreen.style.display = 'flex';
            regNickname.focus();
        });

        switchToLogin.addEventListener('click', () => {
            registerScreen.style.display = 'none';
            loginScreen.style.display = 'flex';
            loginNickname.focus();
        });

        regBtn.addEventListener('click', handleRegister);
        regNickname.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') regPassword.focus();
        });
        regPassword.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') regPasswordConfirm.focus();
        });
        regPasswordConfirm.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') regBtn.click();
        });

        // Chat
        sendBtn.addEventListener('click', sendChatMessage);
        messageInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendChatMessage();
            }
            if (e.key === 'Escape') {
                cancelEditing();
                messageInput.blur();
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

        // Profile
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

        // ============================================
        // INIT
        // ============================================
        loginNickname.focus();
        console.log('💬 Global Chat Pro loaded!');
        console.log('✨ Features: Auth, Mentions, Delete, Edit, Profile View');
    </script>
</body>
</html>
"""

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
