import aiosqlite
import json
from typing import Optional, Dict, List, Any
from datetime import datetime
import logging
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

class Database:
    def __init__(self, db_path: str = "stylist_bot.db"):
        self.db_path = db_path
        self._connection_pool = []
        self._max_pool_size = 10
        self._initialized = False
        
    async def init(self):
        """Инициализация БД с оптимальными настройками"""
        if self._initialized:
            return
            
        # Создаем пул соединений
        for _ in range(self._max_pool_size):
            conn = await aiosqlite.connect(self.db_path)
            
            # Критические оптимизации SQLite
            await conn.execute("PRAGMA journal_mode = WAL")        # Write-Ahead Logging для параллельности
            await conn.execute("PRAGMA synchronous = NORMAL")      # Баланс скорости и безопасности
            await conn.execute("PRAGMA cache_size = -64000")       # 64MB кэш
            await conn.execute("PRAGMA temp_store = MEMORY")       # Временные таблицы в памяти
            await conn.execute("PRAGMA mmap_size = 30000000000")   # Memory-mapped I/O
            await conn.execute("PRAGMA page_size = 4096")          # Оптимальный размер страницы
            
            # Устанавливаем row_factory (НЕ через await!)
            conn.row_factory = aiosqlite.Row
            
            self._connection_pool.append(conn)
        
        await self._init_tables()
        self._initialized = True
        logger.info(f"Database initialized with {self._max_pool_size} connections")
    
    @asynccontextmanager
    async def get_connection(self):
        """Получение соединения из пула"""
        if not self._connection_pool:
            await self.init()
            
        conn = self._connection_pool.pop()
        try:
            yield conn
        finally:
            self._connection_pool.append(conn)
    
    async def _init_tables(self):
        """Создание таблиц с оптимизированными индексами"""
        async with self.get_connection() as conn:
            # Таблица пользователей
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    name TEXT,
                    age INTEGER,
                    city TEXT,
                    gender TEXT,
                    registration_complete INTEGER DEFAULT 0,
                    registration_step TEXT,
                    last_activity TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица контекста сообщений
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS message_context (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    role TEXT,
                    content TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                )
            ''')
            
            # Таблица истории подборов
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS outfit_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    event_type TEXT,
                    outfit_data TEXT,
                    weather_data TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                )
            ''')
            
            # Индексы для быстрого поиска
            await conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_message_context_user_time 
                ON message_context(user_id, timestamp DESC)
            ''')
            
            await conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_outfit_history_user_time 
                ON outfit_history(user_id, created_at DESC)
            ''')
            
            # Индекс для активности пользователей
            await conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_users_last_activity 
                ON users(last_activity DESC)
            ''')
            
            await conn.commit()
            logger.info("Database tables initialized")
    
    async def get_user(self, user_id: int) -> Optional[Dict]:
        """Быстрое получение пользователя"""
        async with self.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM users WHERE user_id = ?", 
                (user_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None
    
    async def create_or_update_user(self, user_id: int, data: Dict):
        """Быстрое обновление пользователя"""
        async with self.get_connection() as conn:
            # Добавляем время последней активности
            data['last_activity'] = datetime.now().isoformat()
            
            # Проверяем существование пользователя
            cursor = await conn.execute(
                "SELECT user_id FROM users WHERE user_id = ?", 
                (user_id,)
            )
            exists = await cursor.fetchone()
            
            if exists:
                # Обновление существующего пользователя
                fields = []
                values = []
                for key, value in data.items():
                    if value is not None:
                        fields.append(f"{key} = ?")
                        values.append(value)
                
                if fields:
                    query = f"UPDATE users SET {', '.join(fields)} WHERE user_id = ?"
                    values.append(user_id)
                    await conn.execute(query, values)
            else:
                # Вставка нового пользователя
                fields = ['user_id'] + [k for k, v in data.items() if v is not None]
                placeholders = ['?'] * len(fields)
                values = [user_id] + [v for v in data.values() if v is not None]
                
                query = f"INSERT INTO users ({', '.join(fields)}) VALUES ({', '.join(placeholders)})"
                await conn.execute(query, values)
            
            await conn.commit()
    
    async def save_message_context(self, user_id: int, role: str, content: str):
        """Быстрое сохранение контекста"""
        async with self.get_connection() as conn:
            await conn.execute(
                "INSERT INTO message_context (user_id, role, content) VALUES (?, ?, ?)",
                (user_id, role, content)
            )
            
            # Оставляем только последние 20 сообщений
            await conn.execute('''
                DELETE FROM message_context 
                WHERE user_id = ? AND id NOT IN (
                    SELECT id FROM message_context 
                    WHERE user_id = ? 
                    ORDER BY timestamp DESC 
                    LIMIT 20
                )
            ''', (user_id, user_id))
            
            await conn.commit()
    
    async def get_message_context(self, user_id: int) -> List[Dict]:
        """Быстрое получение контекста"""
        async with self.get_connection() as conn:
            cursor = await conn.execute('''
                SELECT role, content FROM message_context 
                WHERE user_id = ? 
                ORDER BY timestamp ASC
            ''', (user_id,))
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    
    async def save_outfit_history(self, user_id: int, event_type: str, outfit_data: str, weather_data: Dict):
        """Сохранение истории"""
        async with self.get_connection() as conn:
            await conn.execute(
                "INSERT INTO outfit_history (user_id, event_type, outfit_data, weather_data) VALUES (?, ?, ?, ?)",
                (user_id, event_type, outfit_data, json.dumps(weather_data, ensure_ascii=False))
            )
            await conn.commit()
    
    async def get_outfit_history(self, user_id: int, limit: int = 5) -> List[Dict]:
        """Получение истории"""
        async with self.get_connection() as conn:
            cursor = await conn.execute('''
                SELECT event_type, outfit_data, weather_data, created_at 
                FROM outfit_history 
                WHERE user_id = ? 
                ORDER BY created_at DESC 
                LIMIT ?
            ''', (user_id, limit))
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    
    async def clear_message_context(self, user_id: int):
        """Очистка контекста"""
        async with self.get_connection() as conn:
            await conn.execute(
                "DELETE FROM message_context WHERE user_id = ?", 
                (user_id,)
            )
            await conn.commit()
    
    async def close(self):
        """Закрытие всех соединений"""
        for conn in self._connection_pool:
            await conn.close()
        self._connection_pool.clear()
        self._initialized = False
        logger.info("Database connections closed")