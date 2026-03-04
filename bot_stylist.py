import os
import logging
import asyncio
import json
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    CallbackQueryHandler, filters, ContextTypes
)
from telegram.constants import ParseMode
from database import Database
from ai import AIStylist

load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Константы
EVENTS = {
    'official': '👔 Официальное',
    'interview': '🤝 Собеседование',
    'office': '💼 Офис',
    'study': '📚 Учеба',
    'walk': '🚶 Прогулка',
    'sport_outdoor': '🏃 Спорт на улице',
    'sport_indoor': '🏋️‍♂️ Спорт в зале',
    'date': '❤️ Свидание',
    'party': '🎉 Вечеринка',
    'cinema': '🎬 Кино',
    'theater': '🎭 Театр',
    'concert': '🎸 Концерт',
    'museum': '🏛️ Музей / Выставка',
    'restaurant': '🍽️ Ресторан',
    'bar': '🍷 Бар',
    'family_dinner': '👪 Семейный ужин',
    'home': '🏠 Дом',
    'shopping': '🛍️ Шопинг'
}

# Инициализация компонентов
db = Database()
ai_stylist = AIStylist(
    mistral_api_key=os.getenv('MISTRAL_API_KEY'),
    openweather_api_key=os.getenv('OPENWEATHER_API_KEY')
)

# Клавиатура с одной кнопкой
MAIN_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("👔 Подобрать образ", callback_data="show_events")]
])

# Клавиатура с выбором мероприятия
EVENTS_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton(name, callback_data=f"event_{key}")] 
    for key, name in EVENTS.items()
])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Быстрый старт"""
    user_id = update.effective_user.id
    user = await db.get_user(user_id)
    
    if user and user.get('registration_complete'):
        await update.message.reply_text(
            "👋 С возвращением! Я всегда готов помочь с выбором образа.\n"
            "Просто напиши мне, что планируешь, и я помогу подобрать идеальный лук!",
            reply_markup=MAIN_KEYBOARD,
            parse_mode=ParseMode.HTML
        )
        # Включаем режим диалога по умолчанию
        context.user_data['chat_mode'] = True
    else:
        await db.create_or_update_user(user_id, {
            'registration_step': 'awaiting_info',
            'registration_complete': False
        })
        
        await update.message.reply_text(
            "👋 Привет!\n Я твой личный стилист.\nМеня зовут Светлана)\nЯ помогу тебе с образом на все случаи жизни.\n\n"
            "Расскажи о себе в одном сообщении:\n"
            "• Имя\n"
            "• Возраст\n"
            "• Город\n\n",
            parse_mode=ParseMode.HTML
        )

async def handle_registration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Быстрая регистрация"""
    user_id = update.effective_user.id
    user_input = update.message.text
    
    await update.message.chat.send_action(action="typing")
    
    user = await db.get_user(user_id)
    if not user:
        await start(update, context)
        return
    
    current_data = {
        'name': user.get('name'),
        'age': user.get('age'),
        'city': user.get('city'),
        'gender': user.get('gender')
    }
    
    try:
        result = await asyncio.wait_for(
            ai_stylist.process_registration(user_input, current_data),
            timeout=15
        )
    except asyncio.TimeoutError:
        await update.message.reply_text("⏳ Превышено время ожидания. Попробуйте еще раз.")
        return
    
    # Обновляем данные
    updates = {}
    for field in ['name', 'age', 'city', 'gender']:
        if result.get(field) and result[field] != 'unknown' and result[field] is not None:
            updates[field] = result[field]
    
    if updates:
        await db.create_or_update_user(user_id, updates)
    
    missing = result.get('missing_fields', [])
    
    if not missing and updates.get('gender'):
        await db.create_or_update_user(user_id, {
            'registration_complete': True,
            'registration_step': None
        })
        
        gender_text = 'Мужской' if updates.get('gender') == 'male' else 'Женский'
        await update.message.reply_text(
            f"✅ Отлично! Регистрация завершена!\n\n"
            f"Имя: {updates.get('name')}\n"
            f"Возраст: {updates.get('age')}\n"
            f"Город: {updates.get('city')}\n"
            f"Пол: {gender_text}\n\n"
            f"Теперь мы можем общаться! Просто напиши, куда планируешь пойти, "
            f"и я помогу подобрать образ. Или нажми кнопку ниже:",
            reply_markup=MAIN_KEYBOARD,
            parse_mode=ParseMode.HTML
        )
        # Включаем режим диалога
        context.user_data['chat_mode'] = True
    else:
        next_q = result.get('next_question') or "Уточните, пожалуйста:"
        await update.message.reply_text(next_q, parse_mode=ParseMode.HTML)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопок"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    user = await db.get_user(user_id)
    
    if not user or not user.get('registration_complete'):
        await query.message.reply_text("Сначала /start")
        return
    
    if query.data == "show_events":
        # Показываем выбор мероприятий
        await query.message.reply_text(
            "Выбери мероприятие:",
            reply_markup=EVENTS_KEYBOARD,
            parse_mode=ParseMode.HTML
        )
        
    elif query.data.startswith("event_"):
        event_key = query.data.replace("event_", "")
        event_name = EVENTS.get(event_key, event_key)
        
        # Показываем, что бот думает
        await query.message.chat.send_action(action="typing")
        
        # Получаем погоду
        weather = await ai_stylist.get_weather(user['city'])
        
        if not weather:
            await query.message.reply_text(
                "❌ Не удалось получить погоду.\n"
                "Проверь название города или попробуй позже."
            )
            return
        
        # Показываем прогресс
        progress_msg = await query.message.reply_text(f"⏳ Подбираю образ для {event_name}...")
        
        # Генерируем образ
        outfit = await ai_stylist.generate_outfit(user, event_name, weather)
        
        # Сохраняем в историю
        asyncio.create_task(
            db.save_outfit_history(user_id, event_key, outfit, weather)
        )
        
        # Формируем ответ
        response = (
            f"✨ Образ для *{event_name}*\n\n"
            f"{outfit}\n\n"
            f"📍 {weather['city']}: {weather['temperature']}°C, {weather['description']}"
        )
        
        await progress_msg.delete()
        await query.message.reply_text(
            response,
            parse_mode=ParseMode.HTML
        )
        
        # Спрашиваем, нужна ли помощь
        await query.message.reply_text(
            "💬 Что думаешь? Можешь задать вопросы по образу или попросить другой вариант.",
            reply_markup=MAIN_KEYBOARD,
            parse_mode=ParseMode.HTML
        )
        
        # Сохраняем контекст
        asyncio.create_task(
            db.save_message_context(user_id, "assistant", response[:200])
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка сообщений - всегда в режиме диалога"""
    user_id = update.effective_user.id
    user = await db.get_user(user_id)
    
    if not user:
        await start(update, context)
        return
    
    # Регистрация
    if not user.get('registration_complete'):
        await handle_registration(update, context)
        return
    
    # Всегда в режиме диалога после регистрации
    await update.message.chat.send_action(action="typing")
    
    # Получаем контекст сообщений
    context_messages = await db.get_message_context(user_id)
    
    # Анализируем сообщение пользователя - может быть запрос на подбор образа
    response = await ai_stylist.chat_with_stylist(
        user, 
        update.message.text, 
        context_messages
    )
    
    # Сохраняем сообщения пользователя и ассистента
    asyncio.create_task(
        db.save_message_context(user_id, "user", update.message.text)
    )
    asyncio.create_task(
        db.save_message_context(user_id, "assistant", response)
    )
    
    # Отправляем ответ
    await update.message.reply_text(response, parse_mode=ParseMode.HTML)
    
    # Если это был запрос на подбор образа, показываем кнопки с мероприятиями
    if any(word in update.message.text.lower() for word in ['образ', 'подбери', 'подобрать', 'составь', 'скомбинируй', 'выглядеть', 'лук', 'одеть', 'надеть', 'пойти', 'в чем']):
        await update.message.reply_text(
            "Какое мероприятие планируешь?",
            reply_markup=EVENTS_KEYBOARD,
            parse_mode=ParseMode.HTML
        )
    else:
        # В остальных случаях просто показываем основную кнопку
        await update.message.reply_text(
            "Если захочешь подобрать образ, просто нажми кнопку ниже:",
            reply_markup=MAIN_KEYBOARD,
            parse_mode=ParseMode.HTML
        )

async def exit_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда выхода (оставляем для совместимости)"""
    await update.message.reply_text(
        "Готова ответить на твои вопросы!",
        reply_markup=MAIN_KEYBOARD,
        parse_mode=ParseMode.HTML
    )

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /menu"""
    await update.message.reply_text(
        "Чем могу помочь?",
        reply_markup=MAIN_KEYBOARD,
        parse_mode=ParseMode.HTML
    )

async def post_init(application: Application):
    """После инициализации бота"""
    await db.init()
    logger.info("Database initialized")

async def shutdown(application: Application):
    """При остановке бота"""
    await db.close()
    await ai_stylist.close()
    logger.info("Bot stopped")

def main():
    """Запуск"""
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not found")
        return
    
    # Создаем приложение
    app = Application.builder().token(token).post_init(post_init).build()
    
    # Добавляем обработчики
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("exit", exit_chat))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Регистрируем shutdown
    app.post_shutdown = shutdown
    
    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()