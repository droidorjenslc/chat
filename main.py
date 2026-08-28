import asyncio
import hashlib
import json
import time
from collections import deque
from datetime import datetime
from typing import List, Dict, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

# ============================================
# КОНФИГУРАЦИЯ
# ============================================
MAX_MESSAGES = 100
RATE_LIMIT_SECONDS = 1  # 1 сообщение в секунду
HEARTBEAT_INTERVAL = 30  # секунд


# ============================================
# ХРАНЕНИЕ ДАННЫХ В ПАМЯТИ
# ============================================
class ChatState:
    def __init__(self):
        # Храним последние 100 сообщений: [{"nickname": str, "text": str, "timestamp": float}, ...]
        self.messages: deque = deque(maxlen=MAX_MESSAGES)
        # Активные WebSocket соединения: {websocket: {"nickname": str, "last_message_time": float}}
        self.active_connections: Dict[WebSocket, Dict] = {}
        # Список никнеймов для быстрого доступа
        self.nicknames: Set[str] = set()

        # Добавляем приветственное сообщение
        self.add_message("System", "Добро пожаловать в Global Chat! 🚀", is_system=True)

    def add_message(self, nickname: str, text: str, is_system: bool = False):
        """Добавляет сообщение в историю"""
        message = {
            "nickname": nickname,
            "text": text,
            "timestamp": time.time(),
            "is_system": is_system
        }
        self.messages.append(message)
        return message

    def get_messages(self) -> List[Dict]:
        """Возвращает все сообщения в правильном порядке"""
        return list(self.messages)

    def get_online_count(self) -> int:
        """Возвращает количество активных пользователей"""
        return len(self.active_connections)

    def get_online_users(self) -> List[str]:
        """Возвращает список активных никнеймов"""
        return [data["nickname"] for data in self.active_connections.values()]

    def is_rate_limited(self, websocket: WebSocket) -> bool:
        """Проверяет rate-limit для конкретного соединения"""
        if websocket not in self.active_connections:
            return False

        last_time = self.active_connections[websocket].get("last_message_time", 0)
        current_time = time.time()

        if current_time - last_time < RATE_LIMIT_SECONDS:
            return True

        self.active_connections[websocket]["last_message_time"] = current_time
        return False


# Глобальное состояние чата
chat_state = ChatState()

# ============================================
# FASTAPI ПРИЛОЖЕНИЕ
# ============================================
app = FastAPI(title="Global Chat", description="Минималистичный веб-мессенджер")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================
# HTML ФРОНТЕНД (встроенный)
# ============================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Global Chat</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: #0f172a;
            height: 100vh;
            overflow: hidden;
        }
        #login-screen {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 100vh;
            background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
        }
        #login-screen .card {
            background: rgba(255, 255, 255, 0.05);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.1);
            padding: 2.5rem;
            border-radius: 1.5rem;
            width: 100%;
            max-width: 420px;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
        }
        #login-screen h1 {
            font-size: 2.5rem;
            font-weight: 700;
            background: linear-gradient(135deg, #60a5fa, #a78bfa);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 0.5rem;
        }
        #login-screen .subtitle {
            color: #94a3b8;
            margin-bottom: 2rem;
        }
        #login-screen input {
            background: rgba(255, 255, 255, 0.1);
            border: 1px solid rgba(255, 255, 255, 0.2);
            color: #f1f5f9;
            padding: 0.75rem 1rem;
            border-radius: 0.75rem;
            width: 100%;
            font-size: 1rem;
            transition: all 0.3s;
        }
        #login-screen input:focus {
            outline: none;
            border-color: #60a5fa;
            box-shadow: 0 0 0 3px rgba(96, 165, 250, 0.2);
        }
        #login-screen input::placeholder {
            color: #64748b;
        }
        #login-screen .btn-join {
            background: linear-gradient(135deg, #60a5fa, #a78bfa);
            color: white;
            padding: 0.75rem 1.5rem;
            border-radius: 0.75rem;
            font-weight: 600;
            width: 100%;
            border: none;
            cursor: pointer;
            font-size: 1.1rem;
            transition: all 0.3s;
            margin-top: 1rem;
        }
        #login-screen .btn-join:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 20px -10px rgba(96, 165, 250, 0.4);
        }
        #login-screen .btn-join:disabled {
            opacity: 0.5;
            cursor: not-allowed;
            transform: none;
        }
        #chat-screen {
            display: none;
            flex-direction: column;
            height: 100vh;
            background: #0f172a;
        }
        /* Header */
        .chat-header {
            background: rgba(30, 41, 59, 0.95);
            backdrop-filter: blur(10px);
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
            padding: 1rem 1.5rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-shrink: 0;
        }
        .chat-header .title {
            color: #f1f5f9;
            font-size: 1.25rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }
        .chat-header .title .online-badge {
            background: rgba(34, 197, 94, 0.2);
            color: #4ade80;
            font-size: 0.75rem;
            padding: 0.25rem 0.75rem;
            border-radius: 9999px;
            font-weight: 500;
        }
        .chat-header .user-info {
            color: #94a3b8;
            font-size: 0.9rem;
            display: flex;
            align-items: center;
            gap: 1rem;
        }
        .chat-header .user-info .nickname {
            color: #f1f5f9;
            font-weight: 500;
        }
        .chat-header .user-info .leave-btn {
            background: rgba(239, 68, 68, 0.2);
            color: #f87171;
            padding: 0.25rem 1rem;
            border-radius: 9999px;
            border: none;
            cursor: pointer;
            font-size: 0.8rem;
            transition: all 0.3s;
        }
        .chat-header .user-info .leave-btn:hover {
            background: rgba(239, 68, 68, 0.3);
        }
        /* Messages */
        .messages-container {
            flex: 1;
            overflow-y: auto;
            padding: 1rem 1.5rem;
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
            scroll-behavior: smooth;
        }
        .messages-container::-webkit-scrollbar {
            width: 6px;
        }
        .messages-container::-webkit-scrollbar-track {
            background: transparent;
        }
        .messages-container::-webkit-scrollbar-thumb {
            background: #334155;
            border-radius: 9999px;
        }
        .message {
            display: flex;
            align-items: flex-start;
            gap: 0.75rem;
            animation: fadeIn 0.2s ease-in;
        }
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(10px); }
            to { opacity: 1; transform: translateY(0); }
        }
        .message .avatar {
            width: 36px;
            height: 36px;
            border-radius: 9999px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 600;
            font-size: 0.875rem;
            flex-shrink: 0;
            color: white;
            text-transform: uppercase;
        }
        .message .content {
            flex: 1;
            min-width: 0;
        }
        .message .content .msg-nickname {
            font-weight: 600;
            font-size: 0.875rem;
            margin-right: 0.5rem;
        }
        .message .content .msg-text {
            color: #f1f5f9;
            word-wrap: break-word;
            overflow-wrap: break-word;
        }
        .message .content .msg-time {
            color: #64748b;
            font-size: 0.7rem;
            margin-left: 0.5rem;
        }
        .message.system .content .msg-text {
            color: #94a3b8;
            font-style: italic;
        }
        .message.system .avatar {
            background: #334155 !important;
        }
        /* Input area */
        .input-area {
            background: rgba(30, 41, 59, 0.95);
            backdrop-filter: blur(10px);
            border-top: 1px solid rgba(255, 255, 255, 0.05);
            padding: 0.75rem 1.5rem;
            display: flex;
            gap: 0.75rem;
            flex-shrink: 0;
        }
        .input-area input {
            flex: 1;
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            color: #f1f5f9;
            padding: 0.625rem 1rem;
            border-radius: 0.75rem;
            font-size: 0.95rem;
            transition: all 0.3s;
        }
        .input-area input:focus {
            outline: none;
            border-color: #60a5fa;
            box-shadow: 0 0 0 3px rgba(96, 165, 250, 0.15);
        }
        .input-area input::placeholder {
            color: #64748b;
        }
        .input-area input:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        .input-area .btn-send {
            background: linear-gradient(135deg, #60a5fa, #a78bfa);
            color: white;
            padding: 0.625rem 1.5rem;
            border-radius: 0.75rem;
            border: none;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
            white-space: nowrap;
        }
        .input-area .btn-send:hover:not(:disabled) {
            transform: translateY(-2px);
            box-shadow: 0 10px 20px -10px rgba(96, 165, 250, 0.4);
        }
        .input-area .btn-send:disabled {
            opacity: 0.5;
            cursor: not-allowed;
            transform: none;
        }
        /* Connection status */
        .connection-status {
            padding: 0.5rem 1.5rem;
            text-align: center;
            font-size: 0.875rem;
            font-weight: 500;
            background: rgba(239, 68, 68, 0.15);
            color: #f87171;
            border-bottom: 1px solid rgba(239, 68, 68, 0.2);
            display: none;
            flex-shrink: 0;
        }
        .connection-status.connected {
            background: rgba(34, 197, 94, 0.1);
            color: #4ade80;
            border-bottom-color: rgba(34, 197, 94, 0.2);
            display: block;
        }
        .connection-status.disconnected {
            display: block;
        }
        @media (max-width: 640px) {
            .chat-header {
                flex-direction: column;
                gap: 0.5rem;
                padding: 0.75rem 1rem;
            }
            .chat-header .user-info {
                width: 100%;
                justify-content: space-between;
            }
            .messages-container {
                padding: 0.75rem 1rem;
            }
            .input-area {
                padding: 0.5rem 1rem;
            }
            .input-area .btn-send {
                padding: 0.5rem 1rem;
                font-size: 0.9rem;
            }
        }
    </style>
</head>
<body>
    <!-- Login Screen -->
    <div id="login-screen">
        <div class="card">
            <h1>💬 Global Chat</h1>
            <p class="subtitle">Присоединяйтесь к общему чату</p>
            <input type="text" id="nickname-input" placeholder="Введите ваш никнейм..." maxlength="20" autofocus>
            <div id="login-error" style="color: #f87171; font-size: 0.875rem; margin-top: 0.5rem; display: none;"></div>
            <button class="btn-join" id="join-btn">Войти в чат</button>
        </div>
    </div>

    <!-- Chat Screen -->
    <div id="chat-screen">
        <div class="chat-header">
            <div class="title">
                💬 Global Chat
                <span class="online-badge" id="online-count">🟢 0</span>
            </div>
            <div class="user-info">
                <span>Вы: <span class="nickname" id="current-nickname">—</span></span>
                <button class="leave-btn" id="leave-btn">Выйти</button>
            </div>
        </div>

        <div class="connection-status" id="connection-status">⚠️ Нет соединения с сервером...</div>

        <div class="messages-container" id="messages-container"></div>

        <div class="input-area">
            <input type="text" id="message-input" placeholder="Введите сообщение..." disabled>
            <button class="btn-send" id="send-btn" disabled>Отправить</button>
        </div>
    </div>

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
        };

        // ============================================
        // DOM ЭЛЕМЕНТЫ
        // ============================================
        const $ = (id) => document.getElementById(id);
        const loginScreen = $('login-screen');
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

        // ============================================
        // УТИЛИТЫ
        // ============================================
        function getColorFromString(str) {
            let hash = 0;
            for (let i = 0; i < str.length; i++) {
                hash = str.charCodeAt(i) + ((hash << 5) - hash);
            }
            const hue = Math.abs(hash) % 360;
            return `hsl(${hue}, 70%, 60%)`;
        }

        function formatTime(timestamp) {
            const date = new Date(timestamp * 1000);
            return date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
        }

        function scrollToBottom() {
            messagesContainer.scrollTop = messagesContainer.scrollHeight;
        }

        // ============================================
        // ОТРИСОВКА СООБЩЕНИЙ
        // ============================================
        function renderMessage(message) {
            const div = document.createElement('div');
            div.className = `message ${message.is_system ? 'system' : ''}`;

            const avatar = document.createElement('div');
            avatar.className = 'avatar';
            if (message.is_system) {
                avatar.textContent = '🔔';
                avatar.style.background = '#334155';
            } else {
                const initial = message.nickname.charAt(0).toUpperCase();
                avatar.textContent = initial;
                avatar.style.background = getColorFromString(message.nickname);
            }
            div.appendChild(avatar);

            const content = document.createElement('div');
            content.className = 'content';

            const nameSpan = document.createElement('span');
            nameSpan.className = 'msg-nickname';
            if (!message.is_system) {
                nameSpan.textContent = message.nickname;
                nameSpan.style.color = getColorFromString(message.nickname);
            }
            content.appendChild(nameSpan);

            const textSpan = document.createElement('span');
            textSpan.className = 'msg-text';
            textSpan.textContent = message.text;
            content.appendChild(textSpan);

            const timeSpan = document.createElement('span');
            timeSpan.className = 'msg-time';
            timeSpan.textContent = formatTime(message.timestamp);
            content.appendChild(timeSpan);

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
        // WEBSOCKET СОЕДИНЕНИЕ
        // ============================================
        function getWebSocketUrl() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            return `${protocol}//${window.location.host}/ws`;
        }

        function connectWebSocket() {
            if (state.ws && state.ws.readyState === WebSocket.OPEN) {
                return;
            }

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

                // Отправляем никнейм
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
                // onclose will be called after onerror
            };
        }

        function scheduleReconnect() {
            if (state.reconnectAttempts >= state.maxReconnectAttempts) {
                console.log('Max reconnect attempts reached');
                return;
            }

            state.reconnectAttempts++;
            const delay = Math.min(1000 * Math.pow(1.5, state.reconnectAttempts), 30000);
            console.log(`Reconnecting in ${delay}ms... (attempt ${state.reconnectAttempts})`);

            setTimeout(() => {
                if (!state.connected) {
                    connectWebSocket();
                }
            }, delay);
        }

        // ============================================
        // ОБРАБОТКА СООБЩЕНИЙ ОТ СЕРВЕРА
        // ============================================
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
                    break;

                default:
                    console.log('Unknown message type:', data.type);
            }
        }

        // ============================================
        // ОТПРАВКА СООБЩЕНИЙ
        // ============================================
        function sendMessage(data) {
            if (state.ws && state.ws.readyState === WebSocket.OPEN) {
                state.ws.send(JSON.stringify(data));
                return true;
            }
            return false;
        }

        function sendChatMessage(text) {
            if (!text.trim() || !state.connected) return;
            sendMessage({ type: 'message', text: text.trim() });
            messageInput.value = '';
            messageInput.focus();
        }

        // ============================================
        // ОБНОВЛЕНИЕ UI
        // ============================================
        function updateConnectionStatus(connected) {
            if (connected) {
                connectionStatus.className = 'connection-status connected';
                connectionStatus.textContent = '✅ Соединение установлено';
                setTimeout(() => {
                    connectionStatus.style.display = 'none';
                }, 3000);
            } else {
                connectionStatus.className = 'connection-status disconnected';
                connectionStatus.textContent = '⚠️ Нет соединения с сервером...';
                connectionStatus.style.display = 'block';
            }
        }

        function enableChat(enabled) {
            messageInput.disabled = !enabled;
            sendBtn.disabled = !enabled;
            if (enabled) {
                messageInput.focus();
            }
        }

        function updateOnlineCount(count) {
            onlineCount.textContent = `🟢 ${count}`;
        }

        // ============================================
        // ВХОД И ВЫХОД
        // ============================================
        function joinChat() {
            const nickname = nicknameInput.value.trim();
            if (!nickname) {
                loginError.textContent = 'Пожалуйста, введите никнейм';
                loginError.style.display = 'block';
                return;
            }
            if (nickname.length > 20) {
                loginError.textContent = 'Никнейм не должен превышать 20 символов';
                loginError.style.display = 'block';
                return;
            }
            if (nickname.toLowerCase() === 'system') {
                loginError.textContent = 'Этот никнейм зарезервирован';
                loginError.style.display = 'block';
                return;
            }

            loginError.style.display = 'none';
            state.nickname = nickname;
            currentNickname.textContent = nickname;

            // Переключаем экраны
            loginScreen.style.display = 'none';
            chatScreen.style.display = 'flex';

            // Подключаемся к WebSocket
            connectWebSocket();
        }

        function leaveChat() {
            if (state.ws) {
                state.ws.close();
            }
            state.connected = false;
            state.reconnecting = false;
            state.nickname = '';

            // Переключаем экраны
            chatScreen.style.display = 'none';
            loginScreen.style.display = 'flex';
            nicknameInput.value = '';
            nicknameInput.focus();
        }

        // ============================================
        // ОБРАБОТЧИКИ СОБЫТИЙ
        // ============================================
        joinBtn.addEventListener('click', joinChat);

        nicknameInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                joinChat();
            }
        });

        sendBtn.addEventListener('click', () => sendChatMessage(messageInput.value));

        messageInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendChatMessage(messageInput.value);
            }
        });

        leaveBtn.addEventListener('click', leaveChat);

        // ============================================
        // ИНИЦИАЛИЗАЦИЯ
        // ============================================
        // Фокус на поле ввода при загрузке
        nicknameInput.focus();

        // Автоматическое переподключение при потере фокуса окна
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden && state.nickname && !state.connected && !state.reconnecting) {
                connectWebSocket();
            }
        });

        console.log('💬 Global Chat client loaded');
    </script>
</body>
</html>
"""


# ============================================
# ЭНДПОИНТЫ
# ============================================
@app.get("/", response_class=HTMLResponse)
async def get_chat_page():
    """Отдает HTML-страницу чата"""
    return HTML_TEMPLATE


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket эндпоинт для чата"""
    await websocket.accept()

    nickname = None
    user_data = {"nickname": "Anonymous", "last_message_time": 0}

    try:
        # Ожидаем первое сообщение с никнеймом
        data = await websocket.receive_text()
        try:
            join_data = json.loads(data)
            if join_data.get("type") == "join" and join_data.get("nickname"):
                nickname = join_data["nickname"].strip()[:20]
                # Проверка на зарезервированный никнейм
                if nickname.lower() == "system":
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "Никнейм 'System' зарезервирован"
                    }))
                    await websocket.close(code=1008)
                    return
                user_data["nickname"] = nickname
            else:
                await websocket.close(code=1008)
                return
        except json.JSONDecodeError:
            await websocket.close(code=1008)
            return

        # Проверяем, что никнейм не занят
        if nickname in chat_state.nicknames:
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": f"Никнейм '{nickname}' уже используется"
            }))
            await websocket.close(code=1008)
            return

        # Регистрируем пользователя
        chat_state.active_connections[websocket] = user_data
        chat_state.nicknames.add(nickname)

        # Отправляем историю сообщений
        await websocket.send_text(json.dumps({
            "type": "history",
            "messages": chat_state.get_messages()
        }))

        # Оповещаем всех о новом пользователе
        system_msg = chat_state.add_message(
            "System",
            f"👋 {nickname} присоединился к чату",
            is_system=True
        )
        await broadcast_message(system_msg)
        await broadcast_online_count()

        # Основной цикл обработки сообщений
        while True:
            try:
                data = await websocket.receive_text()
                try:
                    message_data = json.loads(data)
                    if message_data.get("type") == "message":
                        text = message_data.get("text", "").strip()
                        if text:
                            # Rate limiting
                            if chat_state.is_rate_limited(websocket):
                                await websocket.send_text(json.dumps({
                                    "type": "error",
                                    "message": "Слишком много сообщений! Подождите немного."
                                }))
                                continue

                            # Создаем сообщение
                            msg = chat_state.add_message(nickname, text)
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
        # Очистка при отключении
        if websocket in chat_state.active_connections:
            del chat_state.active_connections[websocket]
        if nickname and nickname in chat_state.nicknames:
            chat_state.nicknames.remove(nickname)

        # Оповещаем о выходе пользователя
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
    """Отправляет сообщение всем активным клиентам"""
    if not chat_state.active_connections:
        return

    data = json.dumps({"type": "message", "message": message})
    disconnected = []

    for connection in chat_state.active_connections.keys():
        try:
            await connection.send_text(data)
        except Exception:
            disconnected.append(connection)

    # Удаляем недоступные соединения
    for conn in disconnected:
        if conn in chat_state.active_connections:
            del chat_state.active_connections[conn]


async def broadcast_online_count():
    """Отправляет актуальное количество онлайн всем клиентам"""
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
# ЗАПУСК (приложение готово к использованию с uvicorn)
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