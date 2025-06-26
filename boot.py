from telegram import *
from telegram.ext import Application, CommandHandler, CallbackQueryHandler
import random, os, asyncio, time

COOLDOWN_TIME = 3
LAST_REQUEST_TIME = 0

GIF_FILES = ['1.gif', '2.gif', '3.gif', '4.gif', '5.gif'] 
TOKEN = '6340580990:AAERzIItHOTuf8OUe0VjrbJTmvEL6K6s_T4'

async def start(update: Update, context):
    keyboard = [
        [InlineKeyboardButton("👾", callback_data='1')],
        [InlineKeyboardButton("✨", callback_data='2')],
        [InlineKeyboardButton("🎮", callback_data='3')],
        [InlineKeyboardButton("🌌", callback_data='4')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text('select the one you want💫', reply_markup=reply_markup)

async def handle_button_press(update: Update, context):
    global LAST_REQUEST_TIME
    current_time = time.time()
    query = update.callback_query
    await query.answer()

    if current_time - LAST_REQUEST_TIME < COOLDOWN_TIME:
        await query.edit_message_text(text="Too many requests. Please wait 3 seconds!", reply_markup=query.message.reply_markup)
        return

    LAST_REQUEST_TIME = current_time

    if query.data == '1':
        await query.edit_message_text(text="🤖🤖🤖", reply_markup=query.message.reply_markup)
    elif query.data == '2':
        await query.edit_message_text(text="✨🌠💫", reply_markup=query.message.reply_markup)
    elif query.data == '3':
        await query.edit_message_text(text="卐", reply_markup=query.message.reply_markup)
    elif query.data == '4':
        try:
            gif_file = random.choice(GIF_FILES)
            gif_path = os.path.join(os.getcwd(), gif_file)
            await context.bot.send_document(chat_id=query.message.chat_id, document=open(gif_path, 'rb'))
            await asyncio.sleep(COOLDOWN_TIME)
        except FileNotFoundError:
            await query.edit_message_text(text="GIF file not found. Please check the path and file name.")
        except Exception as e:
            await query.edit_message_text(text=f"Error sending GIF: {e}")

def main():
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_button_press))
    application.run_polling()

if __name__ == '__main__':
    main()