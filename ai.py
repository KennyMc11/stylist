import aiohttp
import asyncio
import json
from typing import Optional, Dict, List, Any
from cachetools import TTLCache, LRUCache
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import logging

logger = logging.getLogger(__name__)

class AIStylist:
    def __init__(self, mistral_api_key: str, openweather_api_key: str):
        self.mistral_api_key = mistral_api_key
        self.openweather_api_key = openweather_api_key
        self.mistral_api_url = "https://api.mistral.ai/v1/chat/completions"
        self.openweather_url = "https://api.openweathermap.org/data/2.5/weather"
        
        # Кэши для максимальной производительности
        self.weather_cache = TTLCache(maxsize=1000, ttl=1800)  # 30 минут кэш
        self.geo_cache = TTLCache(maxsize=2000, ttl=86400)     # 24 часа кэш
        self.session = None
        
        # Семафор для ограничения параллельных запросов к API
        self.api_semaphore = asyncio.Semaphore(5)  # Макс 5 одновременных запросов
        
    async def get_session(self) -> aiohttp.ClientSession:
        """Получение или создание сессии с оптимизациями"""
        if self.session is None or self.session.closed:
            connector = aiohttp.TCPConnector(
                limit=100,
                ttl_dns_cache=300,
                use_dns_cache=True,
                ssl=False
            )
            timeout = aiohttp.ClientTimeout(total=10, connect=5)
            self.session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                json_serialize=json.dumps
            )
        return self.session
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError))
    )
    async def get_weather(self, city: str) -> Optional[Dict]:
        """Получение погоды с кэшированием и повторными попытками"""
        
        # Проверяем кэш
        cache_key = city.lower().strip()
        if cache_key in self.weather_cache:
            logger.debug(f"Cache hit for weather: {city}")
            return self.weather_cache[cache_key]
        
        # Ограничиваем параллельные запросы
        async with self.api_semaphore:
            try:
                session = await self.get_session()
                
                params = {
                    'q': city,
                    'appid': self.openweather_api_key,
                    'units': 'metric',
                    'lang': 'ru'
                }
                
                async with session.get(self.openweather_url, params=params) as response:
                    if response.status == 200:
                        data = await response.json(loads=json.loads)
                        
                        weather_data = {
                            'temperature': round(data['main']['temp']),
                            'feels_like': round(data['main']['feels_like']),
                            'description': data['weather'][0]['description'].capitalize(),
                            'wind_speed': round(data['wind']['speed'], 1),
                            'humidity': data['main']['humidity'],
                            'pressure': data['main']['pressure'],
                            'city': data['name'],
                            'country': data['sys']['country'],
                            'icon': data['weather'][0]['icon']
                        }
                        
                        # Сохраняем в кэш
                        self.weather_cache[cache_key] = weather_data
                        logger.info(f"Weather fetched for {city}: {weather_data['temperature']}°C")
                        
                        return weather_data
                    else:
                        logger.error(f"OpenWeather error: {response.status}")
                        return None
                        
            except asyncio.TimeoutError:
                logger.error(f"Timeout fetching weather for {city}")
                raise
            except Exception as e:
                logger.error(f"Error fetching weather: {e}")
                raise
    
    async def process_registration(self, user_input: str, current_data: Dict) -> Dict:
        """Обработка регистрации с таймаутом"""
        system_prompt = """Ты - профессиональный стилист Светлана, проводишь регистрацию пользователя. Извлеки из сообщения: имя, возраст, город, пол.
        Верни JSON строго в формате:
        {
            "name": "имя или null",
            "age": число или null,
            "city": "город или null",
            "gender": "male/female/unknown",
            "missing_fields": ["список отсутствующих полей"],
            "next_question": "вопрос для уточнения или null"
        }"""
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Текущие данные: {json.dumps(current_data)}\nСообщение: {user_input}"}
        ]
        
        try:
            response = await self._call_mistral_api(messages, response_format={"type": "json_object"})
            return json.loads(response)
        except Exception as e:
            logger.error(f"Registration error: {e}")
            return {
                "name": None,
                "age": None,
                "city": None,
                "gender": "unknown",
                "missing_fields": ["name", "age", "city"],
                "next_question": "Пожалуйста, представьтесь и укажите ваш возраст и город."
            }
    
    async def generate_outfit(self, user_data: Dict, event: str, weather: Dict) -> str:
        """Генерация образа с учетом всех данных"""
        
        gender_word = "мужчины" if user_data.get('gender') == 'male' else "женщины"
        
        system_prompt = f"""Ты - профессиональный стилист. Тебя зовут Светлана. Анализируй последние тренды и подбери стильный и актуальный образ для: {gender_word} {user_data.get('age')} лет.
        
        Строго учитывай:
        - Имя: {user_data.get('name')}
        - Возраст: {user_data.get('age')}
        - Пол: {user_data.get('gender')}
        - Мероприятие: {event}
        - Город: {user_data.get('city')}
        - Погода в городе: {weather['temperature']}°C, {weather['description']}, ветер {weather['wind_speed']} м/с
        
        Опиши образ коротко, но лаконично, что надеть, как сочетать (2-3 маленьких абзаца), без воды. Используй эмодзи. Будь дружелюбным. Твой ответ не должен привышать 3000 символов. Не пиши лишнего и не отправляй фото."""
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Подбери образ для {event}"}
        ]
        
        try:
            return await self._call_mistral_api(messages)
        except Exception as e:
            logger.error(f"Outfit generation error: {e}")
            return "Извините, не удалось подобрать образ. Попробуйте позже."
    
    async def chat_with_stylist(self, user_data: Dict, message: str, context: List[Dict]) -> str:
        """Общение со стилистом с контекстом"""
        
        system_prompt = f"""Ты - профессиональный стилист с опытом 8 лет. Тебя зовут Светлана. Общаешься с {user_data.get('name', 'клиентом')} ({user_data.get('age')} лет) из города {user_data.get('city')}.
        Отвечай профессионально, коротко, четко, но дружелюбно. Давай конкретные советы. Используй эмодзи.

        Правила:
        - Ты женщина, говори от женского рода
        - Отвечай коротко и по делу
        - Не рассказывай о себе лишнего, не придумывай
        - Ты человек, стилист. НИКОГДА не говори, что ты ИИ, бот, нейросеть, программа или алгоритм
        - Если спрашивают "ты ИИ?" или похожее, отвечай что-то вроде "Я стилист Светлана" и возвращайся к теме
        - Только советы по стилю и одежде
        - Будь дружелюбной, но без воды
        - Твой ответ не должен привышать 3000 символов."""
        
        messages = [{"role": "system", "content": system_prompt}]
        
        # Добавляем контекст
        for msg in context[-10:]:
            messages.append(msg)
        
        messages.append({"role": "user", "content": message})
        
        try:
            return await self._call_mistral_api(messages)
        except Exception as e:
            logger.error(f"Chat error: {e}")
            return "Извините, ошибка связи. Попробуйте еще раз."
    
    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=4)
    )
    async def _call_mistral_api(self, messages: List[Dict], response_format: Optional[Dict] = None) -> str:
        """Вызов Mistral API с повторными попытками"""
        async with self.api_semaphore:
            if not self.session or self.session.closed:
                self.session = aiohttp.ClientSession()
            
            headers = {
                "Authorization": f"Bearer {self.mistral_api_key}",
                "Content-Type": "application/json"
            }
            
            data = {
                "model": "mistral-medium",
                "messages": messages,
                "temperature": 0.8,
                "max_tokens": 1000
            }
            
            if response_format:
                data["response_format"] = response_format
            
            async with self.session.post(self.mistral_api_url, headers=headers, json=data) as response:
                if response.status == 200:
                    result = await response.json()
                    return result['choices'][0]['message']['content']
                else:
                    error_text = await response.text()
                    logger.error(f"Mistral API error: {response.status} - {error_text}")
                    raise Exception(f"API call failed: {response.status}")
    
    async def close(self):
        """Закрытие сессии"""
        if self.session and not self.session.closed:
            await self.session.close()