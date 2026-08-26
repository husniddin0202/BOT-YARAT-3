import os
import json
import asyncio
import logging
import httpx
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from io import BytesIO
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

MAIN_BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # bosh administrator (siz)
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")  # masalan: ravshan_uzz (@ belgisiz)


def admin_contact_url() -> str:
    if ADMIN_USERNAME:
        return f"https://t.me/{ADMIN_USERNAME}"
    return f"tg://user?id={ADMIN_ID}"

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
DATA_FILE = "bots_data.json"
TRIAL_DAYS = 7

logging.basicConfig(level=logging.INFO)

main_bot = Bot(token=MAIN_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
main_dp = Dispatcher(storage=MemoryStorage())

BOT_TYPES = {
    "kino": "🎬 Kino bot",
    "shop": "🛒 Savdo bot",
    "ai": "🤖 AI-yordamchi bot",
    "post": "📢 E'lon/Xabar bot",
    "money": "💱 Pul (valyuta) bot",
    "translate": "🌐 Tarjimon bot",
    "contact": "📞 Aloqa bot",
    "survey": "📝 Anketa bot",
    "weather": "🌤 Ob-havo bot",
}

DEFAULT_PRICES = {
    "kino": 120_000,
    "ai": 120_000,
    "shop": 120_000,
    "post": 120_000,
    "money": 120_000,
    "translate": 120_000,
    "contact": 120_000,
    "survey": 120_000,
    "weather": 120_000,
}
DEFAULT_MONTHLY_RATE = 0.2  # keyingi oylar uchun narxning 20 foizi (standart)

running_bots = {}


MONGO_URI = os.getenv("MONGO_URI", "")
mongo_collection = None

if MONGO_URI:
    from pymongo import MongoClient
    try:
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
        mongo_client.admin.command("ping")  # ulanishni darhol sinab ko'ramiz
        mongo_db = mongo_client["botcreator"]
        mongo_collection = mongo_db["data"]
        logging.info("✅ MongoDB'ga muvaffaqiyatli ulanildi — ma'lumotlar doimiy saqlanadi.")
    except Exception as e:
        logging.error(f"❌ MongoDB'ga ulanib bo'lmadi, oddiy fayl ishlatiladi. Xato: {e}")
        mongo_collection = None
else:
    logging.warning("⚠️ MONGO_URI o'rnatilmagan — ma'lumotlar vaqtinchalik faylda saqlanadi.")


def load_data():
    if mongo_collection is not None:
        try:
            doc = mongo_collection.find_one({"_id": "main"})
            if doc:
                doc.pop("_id", None)
                return doc
            return {"bots": {}, "next_bot_id": 1}
        except Exception as e:
            logging.error(f"MongoDB'dan o'qishda xato: {e}")
            return {"bots": {}, "next_bot_id": 1}
    # Zaxira variant: MongoDB sozlanmagan bo'lsa, oddiy fayl orqali ishlaydi
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"bots": {}, "next_bot_id": 1}


def save_data():
    if mongo_collection is not None:
        try:
            doc = dict(data)
            doc["_id"] = "main"
            mongo_collection.replace_one({"_id": "main"}, doc, upsert=True)
            return
        except Exception as e:
            logging.error(f"MongoDB'ga yozishda xato: {e}")
    # Zaxira variant
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


data = load_data()
data.setdefault("next_bot_id", 1)
data.setdefault("prices", dict(DEFAULT_PRICES))
data.setdefault("global_buttons", [])  # [{"label": "...", "response": "..."}]
data.setdefault("monthly_rate", DEFAULT_MONTHLY_RATE)
for _key, _val in DEFAULT_PRICES.items():
    data["prices"].setdefault(_key, _val)

# RAVSHAN BUILDER BOTning to'liq nusxalari (klonlari) shu yerda ro'yxatga olinadi.
# Har biri: {"token": "...", "username": "...", "created_at": "..."}
data.setdefault("platform_clones", [])

running_platform_clones = {}  # token -> asyncio task


def get_price(bot_type: str) -> int:
    return data["prices"].get(bot_type, DEFAULT_PRICES.get(bot_type, 0))


def get_monthly_rate() -> float:
    return data.get("monthly_rate", DEFAULT_MONTHLY_RATE)


def is_active(info: dict) -> bool:
    paid_until = info.get("paid_until")
    if paid_until and datetime.now() < datetime.fromisoformat(paid_until):
        return True
    created = datetime.fromisoformat(info["created_at"])
    return datetime.now() < created + timedelta(days=TRIAL_DAYS)


def next_payment_amount(info: dict) -> int:
    """Birinchi to'lov — to'liq narx. Keyingi to'lovlar — 20 foiz."""
    price = get_price(info["type"])
    if info.get("paid_until"):
        return int(price * get_monthly_rate())
    return price


async def ask_gemini_chat(contents: list) -> str:
    headers = {"x-goog-api-key": GEMINI_API_KEY, "content-type": "application/json"}
    payload = {"contents": contents}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(GEMINI_URL, headers=headers, json=payload)
        resp.raise_for_status()
        result = resp.json()
        return result["candidates"][0]["content"]["parts"][0]["text"]


async def ask_gemini(prompt: str) -> str:
    return await ask_gemini_chat([{"role": "user", "parts": [{"text": prompt}]}])


# ---------- Holatlar (FSM) ----------
class NewBotFlow(StatesGroup):
    waiting_token = State()


class NewPlatformFlow(StatesGroup):
    """RAVSHAN BUILDER BOTning to'liq nusxasini (klon) yaratish uchun — faqat ADMIN_ID."""
    waiting_token = State()


class EditPrice(StatesGroup):
    waiting_amount = State()


class EditRate(StatesGroup):
    waiting_percent = State()


class ActivateFlow(StatesGroup):
    waiting_days = State()


class GlobalButtonAdd(StatesGroup):
    waiting_label = State()
    waiting_response = State()


class AddMovie(StatesGroup):
    waiting_code = State()
    waiting_desc = State()
    waiting_video = State()


class AddSeries(StatesGroup):
    waiting_code = State()
    waiting_title = State()
    waiting_desc = State()
    waiting_episode = State()


class AddProduct(StatesGroup):
    waiting_name = State()
    waiting_price = State()


class Checkout(StatesGroup):
    waiting_address = State()
    waiting_phone = State()


class PostFlow(StatesGroup):
    waiting_text = State()
    waiting_confirm = State()


class AddChannel(StatesGroup):
    waiting_username = State()


class CurrencyAdd(StatesGroup):
    waiting_code = State()
    waiting_rate = State()


class CurrencyUpdate(StatesGroup):
    waiting_rate = State()


class MoneyAmount(StatesGroup):
    waiting_amount = State()


class SurveyAdmin(StatesGroup):
    waiting_question = State()


class SurveyAnswer(StatesGroup):
    answering = State()


class WeatherCity(StatesGroup):
    waiting_city = State()


class AddAdmin(StatesGroup):
    waiting_id = State()


def is_admin(info: dict, uid: int) -> bool:
    return uid in info.get("admin_ids", [info.get("admin_id")])


def admins_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Admin qo'shish", callback_data="adm_add")],
        [InlineKeyboardButton(text="📋 Adminlar ro'yxati", callback_data="adm_list")],
        [InlineKeyboardButton(text="➖ Adminni o'chirish", callback_data="adm_del")],
    ])


def setup_admin_management(dp: Dispatcher, token: str):
    info = data["bots"][token]
    info.setdefault("admin_ids", [info.get("admin_id")])
    owner_id = info["admin_id"]

    @dp.message(Command("cancel"))
    async def cancel_cmd(message: Message, state: FSMContext):
        current = await state.get_state()
        if current is None:
            await message.answer("Bekor qilinadigan jarayon yo'q.")
            return
        await state.clear()
        await message.answer("❌ Jarayon bekor qilindi.")

    @dp.message(Command("admins"))
    @dp.message(F.text == "👤 Adminlar")
    async def admins_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("👤 Adminlar boshqaruvi:", reply_markup=admins_kb())

    @dp.callback_query(F.data == "adm_add")
    async def adm_add_cb(callback: CallbackQuery, state: FSMContext):
        if not is_admin(info, callback.from_user.id):
            return
        await callback.message.answer("Yangi admin Telegram ID'ini yuboring (/myid orqali bilib olish mumkin):")
        await state.set_state(AddAdmin.waiting_id)
        await callback.answer()

    @dp.message(AddAdmin.waiting_id)
    async def adm_add_process(message: Message, state: FSMContext):
        try:
            new_id = int(message.text.strip())
        except ValueError:
            await message.answer("❌ Faqat raqam kiriting.")
            return
        if new_id not in info["admin_ids"]:
            info["admin_ids"].append(new_id)
            save_data()
        await message.answer(f"✅ Admin qo'shildi: {new_id}")
        await state.clear()

    @dp.callback_query(F.data == "adm_list")
    async def adm_list_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        lines = []
        for aid in info["admin_ids"]:
            tag = " (asosiy)" if aid == owner_id else ""
            lines.append(f"• {aid}{tag}")
        await callback.message.answer("👤 Adminlar:\n\n" + "\n".join(lines))
        await callback.answer()

    @dp.callback_query(F.data == "adm_del")
    async def adm_del_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        removable = [aid for aid in info["admin_ids"] if aid != owner_id]
        if not removable:
            await callback.message.answer("O'chirish uchun qo'shimcha admin yo'q.")
            await callback.answer()
            return
        buttons = [[InlineKeyboardButton(text=str(aid), callback_data=f"admdel_{aid}")] for aid in removable]
        await callback.message.answer("O'chirmoqchi bo'lgan adminni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()

    @dp.callback_query(F.data.startswith("admdel_"))
    async def adm_delid_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        target_id = int(callback.data.split("_", 1)[1])
        if target_id in info["admin_ids"] and target_id != owner_id:
            info["admin_ids"].remove(target_id)
            save_data()
            await callback.message.answer(f"🗑 Admin o'chirildi: {target_id}")
        await callback.answer()


# ---------- Majburiy obuna (barcha botlar uchun umumiy) ----------
def channels_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="ch_add")],
        [InlineKeyboardButton(text="📋 Kanallar ro'yxati", callback_data="ch_list")],
        [InlineKeyboardButton(text="➖ Kanal o'chirish", callback_data="ch_del")],
    ])


async def get_missing_channels(bot: Bot, channels: dict, user_id: int):
    missing = []
    for chat_id, info in channels.items():
        try:
            member = await bot.get_chat_member(chat_id=int(chat_id), user_id=user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
                missing.append(info)
        except Exception as e:
            logging.error(f"Obuna tekshirishda xato ({chat_id}): {e}")
            # Xatolik bo'lsa ham xavfsiz tomonni tanlaymiz — obuna talab qilinadi
            missing.append(info)
    return missing


def subscribe_kb(missing):
    buttons = [[InlineKeyboardButton(text=info["title"], url=f"https://t.me/{info['username'].lstrip('@')}")] for info in missing]
    buttons.append([InlineKeyboardButton(text="✅ Obuna bo'ldim", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def require_subscription(event, info: dict, admin_id: int) -> bool:
    uid = event.from_user.id
    if is_admin(info, uid):
        return True
    channels = info.get("channels", {})
    if not channels:
        return True
    missing = await get_missing_channels(event.bot, channels, uid)
    if missing:
        kb = subscribe_kb(missing)
        text = "Botdan foydalanish uchun quyidagi kanal(lar)ga obuna bo'ling:"
        if isinstance(event, CallbackQuery):
            await event.message.answer(text, reply_markup=kb)
            await event.answer()
        else:
            await event.answer(text, reply_markup=kb)
        return False
    return True


async def check_active(event, info: dict, admin_id: int) -> bool:
    """True bo'lsa - bot ishlaydi. False bo'lsa - sinov tugagan / to'lov kerak."""
    if is_active(info):
        return True
    uid = event.from_user.id
    amount = next_payment_amount(info)
    is_renewal = bool(info.get("paid_until"))
    kb = None
    if is_admin(info, uid):
        kb = contact_admin_kb()
        if is_renewal:
            text = (
                f"⏳ <b>Oylik to'lov muddati tugadi.</b>\n\n"
                f"Davom ettirish uchun: <b>{amount:,} so'm</b> (oylik, narxning 20%).\n\n"
                "To'lovni amalga oshirish uchun administrator bilan bog'laning."
            )
        else:
            text = (
                f"⏳ <b>Bepul sinov muddati tugadi.</b>\n\n"
                f"Ushbu bot ({BOT_TYPES.get(info['type'])}) boshlang'ich narxi: <b>{amount:,} so'm</b>.\n"
                f"Keyingi oylardan boshlab: {int(get_price(info['type']) * get_monthly_rate()):,} so'm/oy.\n\n"
                "To'lovni amalga oshirish uchun administrator bilan bog'laning."
            )
    else:
        text = "🚧 Bot vaqtincha ishlamayapti."
    if isinstance(event, CallbackQuery):
        await event.message.answer(text, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb)
    return False


def setup_subscription_handlers(dp: Dispatcher, token: str, admin_id: int):
    info = data["bots"][token]
    info.setdefault("channels", {})

    @dp.callback_query(F.data == "ch_add")
    async def ch_add_cb(callback: CallbackQuery, state: FSMContext):
        if not is_admin(info, callback.from_user.id):
            return
        await callback.message.answer(
            "Kanal usernameni yuboring (masalan: @mening_kanalim).\n"
            "⚠️ Bot o'sha kanalda ADMIN bo'lishi shart!"
        )
        await state.set_state(AddChannel.waiting_username)
        await callback.answer()

    @dp.message(AddChannel.waiting_username)
    async def ch_add_process(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        username = message.text.strip()
        try:
            chat = await message.bot.get_chat(username)
            info["channels"][str(chat.id)] = {"username": username, "title": chat.title}
            save_data()
            await message.answer(f"✅ Qo'shildi: {chat.title}")

            # Bot o'sha kanalda ADMIN ekanligini darhol tekshiramiz
            try:
                bot_member = await message.bot.get_chat_member(chat_id=chat.id, user_id=message.bot.id)
                if bot_member.status not in ("administrator", "creator"):
                    await message.answer(
                        f"⚠️ <b>Diqqat!</b> Bot \"{chat.title}\" kanalida ADMIN emas.\n"
                        "Obuna tekshiruvi ishlashi uchun botni o'sha kanalga ADMIN qilib qo'ying!"
                    )
            except Exception:
                await message.answer(
                    f"⚠️ <b>Diqqat!</b> Bot \"{chat.title}\" kanalida ADMIN ekanligini tekshira olmadim.\n"
                    "Iltimos, botni o'sha kanalga ADMIN qilib qo'ying, aks holda obuna tekshiruvi ishlamaydi!"
                )
        except Exception as e:
            await message.answer(f"❌ Xatolik: kanal topilmadi.\n{e}")
        await state.clear()

    @dp.callback_query(F.data == "ch_list")
    async def ch_list_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        if not info["channels"]:
            await callback.message.answer("Hozircha majburiy kanallar yo'q.")
        else:
            text = "📋 Majburiy obuna kanallari:\n\n" + "\n".join(
                f"• {c['title']} ({c['username']})" for c in info["channels"].values()
            )
            await callback.message.answer(text)
        await callback.answer()

    @dp.callback_query(F.data == "ch_del")
    async def ch_del_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        if not info["channels"]:
            await callback.message.answer("O'chirish uchun kanal yo'q.")
            await callback.answer()
            return
        buttons = [[InlineKeyboardButton(text=c["title"], callback_data=f"chdel_{cid}")] for cid, c in info["channels"].items()]
        await callback.message.answer("O'chirmoqchi bo'lgan kanalni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()

    @dp.callback_query(F.data.startswith("chdel_"))
    async def ch_delid_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        cid = callback.data.split("_", 1)[1]
        removed = info["channels"].pop(cid, None)
        save_data()
        if removed:
            await callback.message.answer(f"🗑 O'chirildi: {removed['title']}")
        await callback.answer()

    @dp.callback_query(F.data == "check_sub")
    async def check_sub_cb(callback: CallbackQuery):
        missing = await get_missing_channels(callback.bot, info["channels"], callback.from_user.id)
        if missing:
            await callback.answer("Hali barcha kanallarga obuna bo'lmagansiz ❌", show_alert=True)
        else:
            await callback.message.edit_text("✅ Rahmat! Endi /start bosib davom eting.")
            await callback.answer()

    @dp.message(Command("channels"))
    @dp.message(F.text == "📡 Majburiy obuna")
    async def channels_panel(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("📡 Majburiy obuna boshqaruvi:", reply_markup=channels_admin_kb())


# ---------- Bosh (creator) bot — XALQ UCHUN OMMAVIY ----------
def types_kb():
    buttons = [[InlineKeyboardButton(text=f"{name} — {get_price(key):,} so'm/oy", callback_data=f"type_{key}")] for key, name in BOT_TYPES.items()]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def contact_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Admin bilan bog'lanish", url=admin_contact_url())]
    ])


def setup_platform_bot(dp: Dispatcher):
    """
    RAVSHAN BUILDER BOTning to'liq mantig'i shu yerda joylashgan: /start, /newbot,
    /prices, /globalbuttons, /mybots, to'lov tasdiqlash va h.k.

    Bu funksiya bitta Dispatcher (main_dp yoki klon bot dispatcheri)ga qo'llanadi.
    Shu tufayli /newplatform orqali yaratilgan har qanday klon — asl RAVSHAN BUILDER
    BOTning AYNAN o'zi kabi ishlaydi (bir xil narxlar, bir xil botlar bazasi,
    bir xil ADMIN_ID nazorati — chunki hammasi umumiy `data` obyektidan foydalanadi).
    """

    @dp.message(Command("cancel"))
    async def main_cancel(message: Message, state: FSMContext):
        current_state = await state.get_state()
        if current_state is None:
            await message.answer("Bekor qilinadigan jarayon yo'q.")
            return
        await state.clear()
        await message.answer("❌ Jarayon bekor qilindi.")

    @dp.message(Command("myid"))
    async def myid_handler(message: Message):
        await message.answer(f"Sizning Telegram ID'ingiz: <code>{message.from_user.id}</code>")

    @dp.message(Command("start"))
    async def main_start(message: Message):
        price_lines = "\n".join(f"{BOT_TYPES[key]} — {get_price(key):,} so'm" for key in BOT_TYPES)
        text = (
            "🤖 <b>Bot yaratuvchi platformaga xush kelibsiz!</b>\n\n"
            "Bu yerda bir necha daqiqada o'zingizga kerakli botni yaratishingiz mumkin.\n\n"
            "💳 <b>Boshlang'ich narxlar:</b>\n"
            f"{price_lines}\n\n"
            f"📅 Keyingi oylardan boshlab — narxning atigi {int(get_monthly_rate()*100)}%.\n"
            f"🎁 Har bir bot uchun {TRIAL_DAYS} kunlik BEPUL sinov muddati bor!\n\n"
            "💳 To'lov usuli: administrator bilan bog'lanish\n\n"
            "Bot yaratish: /newbot\n"
            "Botlaringiz: /mybots"
        )
        await message.answer(text, reply_markup=contact_admin_kb())

    @dp.message(Command("newbot"))
    async def newbot_start(message: Message, state: FSMContext):
        await message.answer(
            "Yangi bot tokenini yuboring.\n"
            "(@BotFather orqali /newbot bilan yaratib, tokenni shu yerga joylashtiring)"
        )
        await state.set_state(NewBotFlow.waiting_token)

    @dp.message(NewBotFlow.waiting_token)
    async def newbot_token(message: Message, state: FSMContext):
        token = message.text.strip()
        try:
            test_bot = Bot(token=token)
            me = await test_bot.get_me()
            await test_bot.session.close()
        except Exception:
            await message.answer("❌ Token noto'g'ri. Qaytadan yuboring.")
            return

        await state.update_data(token=token, bot_name=me.first_name)
        await message.answer(f"✅ Bot topildi: <b>{me.first_name}</b>\n\nEndi bot turini tanlang:", reply_markup=types_kb())

    @dp.callback_query(F.data.startswith("type_"))
    async def newbot_type(callback: CallbackQuery, state: FSMContext):
        bot_type = callback.data.split("_", 1)[1]
        state_data = await state.get_data()
        token = state_data.get("token")
        bot_name = state_data.get("bot_name")

        if not token:
            await callback.answer("Xatolik: qaytadan /newbot bosing.", show_alert=True)
            return

        bot_id = data["next_bot_id"]
        data["next_bot_id"] += 1

        data["bots"][token] = {
            "id": bot_id,
            "type": bot_type,
            "name": bot_name,
            "admin_id": callback.from_user.id,
            "admin_ids": [callback.from_user.id],
            "created_at": datetime.now().isoformat(),
            "paid_until": None,
            "movies": {},
            "products": {},
            "next_id": 1,
            "carts": {},
            "channels": {},
            "users": [],
            "stats": {},
        }
        save_data()

        await start_child_bot(token, bot_type)

        await callback.message.edit_text(
            f"✅ {BOT_TYPES[bot_type]} ishga tushdi: <b>{bot_name}</b>\n\n"
            f"🎁 {TRIAL_DAYS} kunlik bepul sinov boshlandi!\n"
            "Majburiy obuna qo'shish uchun o'sha botga /channels yozing."
        )
        await state.clear()
        await callback.answer()

    @dp.message(Command("prices"))
    async def prices_panel(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        buttons = [
            [InlineKeyboardButton(text=f"{name} — {get_price(key):,} so'm", callback_data=f"editprice_{key}")]
            for key, name in BOT_TYPES.items()
        ]
        buttons.append([InlineKeyboardButton(
            text=f"📅 Oylik foiz: {int(get_monthly_rate()*100)}%", callback_data="editrate"
        )])
        await message.answer("💰 <b>Narxlarni boshqarish</b>\n\nO'zgartirish uchun tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data == "editrate")
    async def editrate_cb(callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id != ADMIN_ID:
            return
        await callback.message.answer(
            f"Hozirgi oylik foiz: {int(get_monthly_rate()*100)}%\n\nYangi foizni kiriting (masalan: 20):"
        )
        await state.set_state(EditRate.waiting_percent)
        await callback.answer()

    @dp.message(EditRate.waiting_percent)
    async def editrate_save(message: Message, state: FSMContext):
        try:
            percent = float(message.text.strip().replace("%", ""))
            if not (0 <= percent <= 100):
                raise ValueError
        except ValueError:
            await message.answer("❌ 0 dan 100 gacha bo'lgan raqam kiriting.")
            return
        data["monthly_rate"] = percent / 100
        save_data()
        await message.answer(f"✅ Oylik foiz endi {percent:g}% qilib o'rnatildi.")
        await state.clear()

    @dp.callback_query(F.data.startswith("editprice_"))
    async def editprice_cb(callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id != ADMIN_ID:
            return
        bot_type = callback.data.split("_", 1)[1]
        await state.update_data(edit_price_type=bot_type)
        await callback.message.answer(
            f"{BOT_TYPES[bot_type]} uchun yangi boshlang'ich narxni kiriting (so'm, faqat raqam):"
        )
        await state.set_state(EditPrice.waiting_amount)
        await callback.answer()

    @dp.message(EditPrice.waiting_amount)
    async def editprice_save(message: Message, state: FSMContext):
        try:
            amount = int(message.text.strip().replace(" ", ""))
        except ValueError:
            await message.answer("❌ Faqat raqam kiriting.")
            return
        state_data = await state.get_data()
        bot_type = state_data.get("edit_price_type")
        if bot_type:
            data["prices"][bot_type] = amount
            save_data()
            await message.answer(f"✅ {BOT_TYPES[bot_type]} narxi endi {amount:,} so'm.")
        await state.clear()

    @dp.message(Command("globalbuttons"))
    async def global_buttons_panel(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        buttons = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Tugma qo'shish", callback_data="gb_add")],
            [InlineKeyboardButton(text="📋 Tugmalar ro'yxati", callback_data="gb_list")],
            [InlineKeyboardButton(text="➖ Tugmani o'chirish", callback_data="gb_del")],
        ])
        await message.answer(
            "🧩 <b>Global tugmalar boshqaruvi</b>\n\n"
            "Bu yerda qo'shgan tugma barcha turdagi botning menyusiga avtomatik qo'shiladi.",
            reply_markup=buttons,
        )

    @dp.callback_query(F.data == "gb_add")
    async def gb_add_cb(callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id != ADMIN_ID:
            return
        await callback.message.answer("Tugma nomini yozing (masalan: ℹ️ Biz haqimizda):")
        await state.set_state(GlobalButtonAdd.waiting_label)
        await callback.answer()

    @dp.message(GlobalButtonAdd.waiting_label)
    async def gb_add_label(message: Message, state: FSMContext):
        await state.update_data(label=message.text.strip())
        await message.answer("Endi shu tugma bosilganda chiqadigan javob matnini yozing:")
        await state.set_state(GlobalButtonAdd.waiting_response)

    @dp.message(GlobalButtonAdd.waiting_response)
    async def gb_add_response(message: Message, state: FSMContext):
        state_data = await state.get_data()
        label = state_data.get("label")
        data["global_buttons"].append({"label": label, "response": message.text})
        save_data()
        await message.answer(f"✅ Tugma qo'shildi: {label}\n\nEndi barcha botlarda ko'rinadi.")
        await state.clear()

    @dp.callback_query(F.data == "gb_list")
    async def gb_list_cb(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            return
        if not data["global_buttons"]:
            await callback.message.answer("Hozircha global tugmalar yo'q.")
        else:
            text = "📋 <b>Global tugmalar:</b>\n\n" + "\n".join(f"• {b['label']}" for b in data["global_buttons"])
            await callback.message.answer(text)
        await callback.answer()

    @dp.callback_query(F.data == "gb_del")
    async def gb_del_cb(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            return
        if not data["global_buttons"]:
            await callback.message.answer("O'chirish uchun tugma yo'q.")
            await callback.answer()
            return
        buttons = [
            [InlineKeyboardButton(text=b["label"], callback_data=f"gbdel_{i}")]
            for i, b in enumerate(data["global_buttons"])
        ]
        await callback.message.answer("O'chirmoqchi bo'lgan tugmani tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()

    @dp.callback_query(F.data.startswith("gbdel_"))
    async def gb_delid_cb(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            return
        idx = int(callback.data.split("_", 1)[1])
        if 0 <= idx < len(data["global_buttons"]):
            removed = data["global_buttons"].pop(idx)
            save_data()
            await callback.message.answer(f"🗑 O'chirildi: {removed['label']}")
        await callback.answer()

    @dp.message(Command("mybots"))
    async def mybots(message: Message):
        uid = message.from_user.id
        if uid == ADMIN_ID:
            items = list(data["bots"].items())
        else:
            items = [(t, i) for t, i in data["bots"].items() if uid in i.get("admin_ids", [i["admin_id"]])]

        if not items:
            await message.answer("Hali botlaringiz yo'q. /newbot orqali yarating.")
            return

        for token, info in items:
            status = "🟢 Faol" if is_active(info) else "🔴 Sinov/to'lov tugagan"
            paid_until = info.get("paid_until")
            if paid_until:
                date_str = datetime.fromisoformat(paid_until).strftime("%d.%m.%Y")
                paid_note = f" (to'langan: {date_str} gacha)"
            else:
                paid_note = ""
            text = f"{BOT_TYPES.get(info['type'])}: <b>{info['name']}</b>\n{status}{paid_note}"
            if uid == ADMIN_ID and info["admin_id"] != ADMIN_ID:
                text += f"\n👤 Egasi ID: {info['admin_id']}"
            kb = None
            if uid == ADMIN_ID:
                amount = next_payment_amount(info)
                buttons = [[InlineKeyboardButton(text=f"✅ To'lovni tasdiqlash ({amount:,} so'm)", callback_data=f"activate_{info['id']}")]]
                if info.get("paid_until"):
                    buttons.append([InlineKeyboardButton(text="❌ Tasdiqdan chiqarish", callback_data=f"deactivate_{info['id']}")])
                kb = InlineKeyboardMarkup(inline_keyboard=buttons)
            await message.answer(text, reply_markup=kb)

    @dp.callback_query(F.data.startswith("activate_"))
    async def activate_cb(callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id != ADMIN_ID:
            return
        bot_id = int(callback.data.split("_", 1)[1])
        await state.update_data(activate_bot_id=bot_id)
        await callback.message.answer("Necha kunga faollashtirilsin? (masalan: 30):")
        await state.set_state(ActivateFlow.waiting_days)
        await callback.answer()

    @dp.message(ActivateFlow.waiting_days)
    async def activate_days_save(message: Message, state: FSMContext):
        try:
            days = int(message.text.strip())
            if days <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Musbat butun raqam kiriting (masalan: 30).")
            return
        state_data = await state.get_data()
        bot_id = state_data.get("activate_bot_id")
        for token, info in data["bots"].items():
            if info.get("id") == bot_id:
                expiry = datetime.now() + timedelta(days=days)
                info["paid_until"] = expiry.isoformat()
                save_data()
                await message.answer(f"✅ {info['name']} bot {expiry.strftime('%d.%m.%Y')} sanagacha faollashtirildi.")
                break
        await state.clear()

    @dp.callback_query(F.data.startswith("deactivate_"))
    async def deactivate_cb(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            return
        bot_id = int(callback.data.split("_", 1)[1])
        for token, info in data["bots"].items():
            if info.get("id") == bot_id:
                info["paid_until"] = None
                save_data()
                await callback.message.answer(f"❌ {info['name']} bot tasdiqdan chiqarildi (to'lov holati bekor qilindi).")
                break
        await callback.answer()

    # ---- RAVSHAN BUILDER BOTning to'liq nusxasini (klon) yaratish — FAQAT ADMIN_ID ----
    @dp.message(Command("newplatform"))
    async def newplatform_start(message: Message, state: FSMContext):
        if message.from_user.id != ADMIN_ID:
            return
        await message.answer(
            "🏗 <b>RAVSHAN BUILDER BOTning yangi nusxasini yaratish</b>\n\n"
            "@BotFather orqali yangi bot yarating va uning tokenini shu yerga yuboring.\n"
            "Token yuborilishi bilan bu yangi bot — hozirgi bot bilan bir xil, "
            "to'liq ishlaydigan RAVSHAN BUILDER BOT nusxasiga aylanadi "
            "(bir xil botlar bazasi, bir xil narxlar, siz — bir xil admin)."
        )
        await state.set_state(NewPlatformFlow.waiting_token)

    @dp.message(NewPlatformFlow.waiting_token)
    async def newplatform_token(message: Message, state: FSMContext):
        if message.from_user.id != ADMIN_ID:
            await state.clear()
            return
        clone_token = message.text.strip()
        try:
            test_bot = Bot(token=clone_token)
            me = await test_bot.get_me()
            await test_bot.session.close()
        except Exception:
            await message.answer("❌ Token noto'g'ri. Qaytadan yuboring.")
            return

        await start_platform_clone(clone_token, me.username)
        await message.answer(
            f"✅ Tayyor! @{me.username} — bu endi to'liq RAVSHAN BUILDER BOT nusxasi.\n\n"
            "U orqali ham /newbot bilan botlar yaratish, /mybots, /prices, /globalbuttons "
            "— hammasi ishlaydi, xuddi shu botdagidek."
        )
        await state.clear()


async def start_platform_clone(token: str, username: str = None):
    """RAVSHAN BUILDER BOTning to'liq nusxasini berilgan token bilan ishga tushiradi."""
    if token in running_platform_clones:
        return
    clone_bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    clone_dp = Dispatcher(storage=MemoryStorage())
    setup_platform_bot(clone_dp)
    task = asyncio.create_task(clone_dp.start_polling(clone_bot))
    running_platform_clones[token] = task

    if not any(c["token"] == token for c in data["platform_clones"]):
        data["platform_clones"].append({
            "token": token,
            "username": username,
            "created_at": datetime.now().isoformat(),
        })
        save_data()


setup_platform_bot(main_dp)


# ---------- Kino bot ----------
def setup_kino_bot(dp: Dispatcher, token: str):
    info = data["bots"][token]
    admin_id = info["admin_id"]
    info["stats"].setdefault("requests", 0)
    setup_subscription_handlers(dp, token, admin_id)
    setup_admin_management(dp, token)
    setup_global_buttons_handler(dp, lambda m, s: kstart(m))

    def kino_admin_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="🎬 Film qo'shish"), KeyboardButton(text="📺 Serial qo'shish")],
            [KeyboardButton(text="📋 Filmlar ro'yxati"), KeyboardButton(text="🗑 Film o'chirish")],
            [KeyboardButton(text="📊 Statistika")],
            [KeyboardButton(text="📡 Majburiy obuna"), KeyboardButton(text="👤 Adminlar")],
        ] + get_global_button_rows(), resize_keyboard=True)

    @dp.message(Command("start"))
    async def kstart(message: Message):
        uid = message.from_user.id
        if uid not in info["users"]:
            info["users"].append(uid)
            save_data()
        if not await check_active(message, info, admin_id):
            return
        if is_admin(info, uid):
            await message.answer(
                "🎬 <b>Kino bot boshqaruvi</b>\n\nPastdagi menyudan foydalaning 👇",
                reply_markup=kino_admin_kb(),
            )
        else:
            await message.answer("🎬 Film kodini yuboring, men uni topib beraman.")

    @dp.message(Command("stats"))
    @dp.message(F.text == "📊 Statistika")
    async def kino_stats(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(
            f"📊 <b>Statistika</b>\n\n"
            f"👥 Foydalanuvchilar: {len(info['users'])}\n"
            f"🔍 Kino so'rovlari: {info['stats']['requests']}\n"
            f"🎞 Saqlangan filmlar: {len(info['movies'])}"
        )

    @dp.message(Command("addmovie"))
    @dp.message(F.text == "🎬 Film qo'shish")
    async def addmovie_cmd(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Kino kodini yuboring (faqat raqam, masalan: 40):")
        await state.set_state(AddMovie.waiting_code)

    @dp.message(F.text == "📺 Serial qo'shish")
    async def addseries_cmd(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Serial kodini yuboring (faqat raqam, masalan: 41):")
        await state.set_state(AddSeries.waiting_code)

    @dp.message(AddSeries.waiting_code)
    async def addseries_code(message: Message, state: FSMContext):
        code = message.text.strip()
        if not code.isdigit():
            await message.answer("❌ Kod faqat raqamlardan iborat bo'lishi kerak. Qaytadan yuboring:")
            return
        await state.update_data(code=code)
        await message.answer("Serial nomini yozing (masalan: Umar ibn Xattob):")
        await state.set_state(AddSeries.waiting_title)

    @dp.message(AddSeries.waiting_title)
    async def addseries_title(message: Message, state: FSMContext):
        await state.update_data(title=message.text.strip())
        await message.answer(
            "Tavsif yozing (sifati, davlati, janri, tili, yili va h.k.):"
        )
        await state.set_state(AddSeries.waiting_desc)

    @dp.message(AddSeries.waiting_desc)
    async def addseries_desc(message: Message, state: FSMContext):
        await state.update_data(desc=message.text.strip(), episodes={})
        await message.answer(
            "Endi 1-qism videosini yuboring.\n"
            "Har bir videoni ketma-ket yuboraverasiz (avtomatik 1, 2, 3... deb raqamlanadi).\n"
            "Barcha qismlarni yuborib bo'lgach, /done deb yozing."
        )
        await state.set_state(AddSeries.waiting_episode)

    @dp.message(AddSeries.waiting_episode, F.video)
    async def addseries_episode(message: Message, state: FSMContext):
        state_data = await state.get_data()
        episodes = state_data.get("episodes", {})
        next_num = len(episodes) + 1
        episodes[str(next_num)] = message.video.file_id
        await state.update_data(episodes=episodes)
        await message.answer(f"✅ {next_num}-qism saqlandi. Davom eting yoki /done deb tugating.")

    @dp.message(AddSeries.waiting_episode, Command("done"))
    async def addseries_done(message: Message, state: FSMContext):
        state_data = await state.get_data()
        episodes = state_data.get("episodes", {})
        if not episodes:
            await message.answer("❌ Kamida bitta qism yuborishingiz kerak.")
            return
        code = state_data["code"]
        info["movies"][code] = {
            "type": "series",
            "title": state_data["title"],
            "desc": state_data["desc"],
            "episodes": episodes,
        }
        save_data()
        await message.answer(
            f"✅ Serial saqlandi: <b>{state_data['title']}</b> ({len(episodes)} qism), Kod: {code}"
        )
        await state.clear()

    @dp.message(AddSeries.waiting_episode)
    async def addseries_wrong(message: Message):
        await message.answer("❌ Video yuboring yoki barcha qismlar tugagan bo'lsa /done deb yozing.")

    async def send_series_episode(send_func, series: dict, code: str, ep_num: int):
        episodes = series["episodes"]
        sorted_eps = sorted(int(k) for k in episodes.keys())
        total = len(sorted_eps)
        file_id = episodes.get(str(ep_num))
        caption = (
            f"🎬 <b>{series['title']}</b>\n"
            f"🆔 Kodi: {code}\n"
            f"📁 Qism: {ep_num}/{total}\n\n"
            f"{series.get('desc', '')}"
        )
        buttons = []
        row = []
        for n in sorted_eps:
            label = f"• {n}-qism" if n == ep_num else f"{n}-qism"
            row.append(InlineKeyboardButton(text=label, callback_data=f"ep_{code}_{n}"))
            if len(row) == 4:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        next_ep = ep_num + 1
        if next_ep in sorted_eps:
            buttons.append([InlineKeyboardButton(text="Keyingi ▶️", callback_data=f"ep_{code}_{next_ep}")])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await send_func(file_id, caption=caption, reply_markup=kb)

    @dp.callback_query(F.data.startswith("ep_"))
    async def episode_nav_cb(callback: CallbackQuery):
        _, code, num_str = callback.data.split("_")
        num = int(num_str)
        series = info["movies"].get(code)
        if not series or series.get("type") != "series":
            await callback.answer("Topilmadi.", show_alert=True)
            return
        await send_series_episode(callback.message.answer_video, series, code, num)
        await callback.answer()

    @dp.message(AddMovie.waiting_code)
    async def addmovie_code(message: Message, state: FSMContext):
        code = message.text.strip()
        if not code.isdigit():
            await message.answer("❌ Kod faqat raqamlardan iborat bo'lishi kerak. Qaytadan yuboring:")
            return
        await state.update_data(code=code)
        await message.answer("Endi kino haqida qisqacha tavsif yozing (janr, yil, va h.k.):")
        await state.set_state(AddMovie.waiting_desc)

    @dp.message(AddMovie.waiting_desc)
    async def addmovie_desc(message: Message, state: FSMContext):
        await state.update_data(desc=message.text.strip())
        await message.answer("Endi filmni (videoni) yuboring:")
        await state.set_state(AddMovie.waiting_video)

    @dp.message(AddMovie.waiting_video, F.video)
    async def addmovie_video(message: Message, state: FSMContext):
        state_data = await state.get_data()
        code = state_data.get("code")
        desc = state_data.get("desc", "")
        info["movies"][code] = {"file_id": message.video.file_id, "desc": desc}
        save_data()
        await message.answer(f"✅ Kod <b>{code}</b> bilan film saqlandi.")
        await state.clear()

    @dp.message(AddMovie.waiting_video)
    async def addmovie_wrong(message: Message):
        await message.answer("❌ Iltimos, video fayl yuboring (forward qilingan bo'lsa ham bo'ladi).")

    @dp.message(F.text == "📋 Filmlar ro'yxati")
    async def list_movies(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["movies"]:
            await message.answer("Hozircha filmlar yo'q.")
            return
        lines = []
        for code, m in info["movies"].items():
            if m.get("type") == "series":
                lines.append(f"• Kod {code} 📺 [Serial] {m.get('title', '-')} ({len(m.get('episodes', {}))} qism)")
            else:
                lines.append(f"• Kod {code} 🎬 {m.get('desc', '-')[:40]}")
        await message.answer("📋 <b>Filmlar:</b>\n\n" + "\n".join(lines))

    @dp.message(F.text == "🗑 Film o'chirish")
    async def del_movie_start(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["movies"]:
            await message.answer("O'chirish uchun film yo'q.")
            return
        buttons = [[InlineKeyboardButton(text=f"Kod {code}", callback_data=f"delmovie_{code}")] for code in info["movies"]]
        await message.answer("O'chirmoqchi bo'lgan filmni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("delmovie_"))
    async def del_movie_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        code = callback.data.split("_", 1)[1]
        removed = info["movies"].pop(code, None)
        save_data()
        if removed:
            await callback.message.answer(f"🗑 Kod {code} o'chirildi.")
        await callback.answer()

    @dp.message(F.text)
    async def get_movie(message: Message):
        if not await check_active(message, info, admin_id):
            return
        if not await require_subscription(message, info, admin_id):
            return
        code = message.text.strip()
        info["stats"]["requests"] += 1
        save_data()
        entry = info["movies"].get(code)
        if not entry:
            await message.answer("❌ Bunday kodli film topilmadi.")
            return
        if entry.get("type") == "series":
            await send_series_episode(message.answer_video, entry, code, 1)
        else:
            caption = f"🎬 Kod: {code}"
            if entry.get("desc"):
                caption += f"\n\n{entry['desc']}"
            await message.answer_video(entry["file_id"], caption=caption)


# ---------- Savdo bot ----------
def setup_shop_bot(dp: Dispatcher, token: str):
    info = data["bots"][token]
    admin_id = info["admin_id"]
    info["stats"].setdefault("orders", 0)
    info["stats"].setdefault("revenue", 0)
    setup_subscription_handlers(dp, token, admin_id)
    setup_admin_management(dp, token)
    setup_global_buttons_handler(dp, lambda m, s: sstart(m))

    def admin_kb():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Mahsulot qo'shish", callback_data="padd")],
            [InlineKeyboardButton(text="📦 Mahsulotlar ro'yxati", callback_data="plist")],
            [InlineKeyboardButton(text="➖ Mahsulotni o'chirish", callback_data="pdel")],
        ])

    def catalog_kb():
        buttons = []
        sorted_products = sorted(info["products"].items(), key=lambda item: item[1]["name"].lower())
        for pid, p in sorted_products:
            if p["qty"] > 0:
                buttons.append([InlineKeyboardButton(text=f"{p['name']} — {p['price']:,} so'm", callback_data=f"buy_{pid}")])
        return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

    def main_menu_kb():
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🛍 Mahsulotlar"), KeyboardButton(text="🛒 Savatim")],
                [KeyboardButton(text="📜 Buyurtmalarim")],
            ],
            resize_keyboard=True,
        )

    async def send_cart(user_id: int, send_func):
        uid = str(user_id)
        cart = info["carts"].get(uid, {})
        if not cart:
            await send_func("🛒 Savatingiz bo'sh.")
            return
        lines = []
        total = 0
        for pid, qty in cart.items():
            p = info["products"].get(pid)
            if not p:
                continue
            subtotal = p["price"] * qty
            total += subtotal
            lines.append(f"{p['name']} x{qty} = {subtotal:,} so'm")
        text = "🛒 <b>Savatingiz:</b>\n\n" + "\n".join(lines) + f"\n\n💰 Jami: {total:,} so'm"
        buttons = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Buyurtma berish", callback_data="checkout")],
            [InlineKeyboardButton(text="🗑 Tozalash", callback_data="cart_clear")],
        ])
        await send_func(text, reply_markup=buttons)

    def shop_admin_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="📊 Statistika")],
            [KeyboardButton(text="📡 Majburiy obuna"), KeyboardButton(text="👤 Adminlar")],
        ] + get_global_button_rows(), resize_keyboard=True)

    @dp.message(Command("start"))
    async def sstart(message: Message):
        uid = message.from_user.id
        if uid not in info["users"]:
            info["users"].append(uid)
            save_data()
        if not await check_active(message, info, admin_id):
            return
        if is_admin(info, uid):
            await message.answer("🛒 <b>Savdo bot boshqaruvi</b>", reply_markup=admin_kb())
            await message.answer("Qo'shimcha bo'limlar 👇", reply_markup=shop_admin_menu_kb())
            return
        if not await require_subscription(message, info, admin_id):
            return

        if not info["products"]:
            await message.answer("Hozircha mahsulotlar yo'q.", reply_markup=main_menu_kb())
        else:
            kb = catalog_kb()
            await message.answer("🛍 Mahsulotlar:", reply_markup=kb)
            await message.answer("Pastdagi menyudan foydalaning 👇", reply_markup=main_menu_kb())

    @dp.message(F.text == "🛍 Mahsulotlar")
    async def show_catalog(message: Message):
        if not await check_active(message, info, admin_id):
            return
        if not await require_subscription(message, info, admin_id):
            return
        kb = catalog_kb()
        if not kb:
            await message.answer("Hozircha mahsulotlar yo'q.")
        else:
            await message.answer("🛍 Mahsulotlar:", reply_markup=kb)

    @dp.message(F.text == "🛒 Savatim")
    async def show_cart_menu(message: Message):
        await send_cart(message.from_user.id, message.answer)

    @dp.message(Command("stats"))
    @dp.message(F.text == "📊 Statistika")
    async def shop_stats(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(
            f"📊 <b>Statistika</b>\n\n"
            f"👥 Foydalanuvchilar: {len(info['users'])}\n"
            f"🧾 Buyurtmalar: {info['stats']['orders']}\n"
            f"💰 Jami tushum: {info['stats']['revenue']:,} so'm"
        )

    @dp.callback_query(F.data == "padd")
    async def padd_cb(callback: CallbackQuery, state: FSMContext):
        if not is_admin(info, callback.from_user.id):
            return
        await callback.message.answer("Mahsulot nomini yozing:")
        await state.set_state(AddProduct.waiting_name)
        await callback.answer()

    @dp.message(AddProduct.waiting_name)
    async def padd_name(message: Message, state: FSMContext):
        await state.update_data(name=message.text.strip())
        await message.answer("Narxini yozing (faqat raqam, so'mda):")
        await state.set_state(AddProduct.waiting_price)

    @dp.message(AddProduct.waiting_price)
    async def padd_price(message: Message, state: FSMContext):
        try:
            price = int(message.text.strip().replace(" ", ""))
        except ValueError:
            await message.answer("❌ Faqat raqam kiriting.")
            return
        state_data = await state.get_data()
        pid = str(info["next_id"])
        info["next_id"] += 1
        info["products"][pid] = {"name": state_data["name"], "price": price, "qty": 999999}
        save_data()
        await message.answer(f"✅ Qo'shildi: {state_data['name']} — {price:,} so'm")
        await state.clear()

        # Mavjud xaridorlarga yangilangan katalogni tabiiy ko'rinishda yuborish
        for uid in info["users"]:
            if is_admin(info, uid):
                continue
            try:
                kb = catalog_kb()
                if kb:
                    await message.bot.send_message(uid, "🛍 Mahsulotlar:", reply_markup=kb)
            except Exception:
                pass

    @dp.callback_query(F.data == "plist")
    async def plist_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        if not info["products"]:
            await callback.message.answer("Mahsulotlar yo'q.")
        else:
            sorted_products = sorted(info["products"].items(), key=lambda item: item[1]["name"].lower())
            text = "📦 <b>Mahsulotlar (alifbo tartibida):</b>\n\n" + "\n".join(
                f"#{pid}: {p['name']} — {p['price']:,} so'm ({p['qty']} dona)" for pid, p in sorted_products
            )
            await callback.message.answer(text)
        await callback.answer()

    @dp.callback_query(F.data == "pdel")
    async def pdel_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        if not info["products"]:
            await callback.message.answer("O'chirish uchun mahsulot yo'q.")
            await callback.answer()
            return
        sorted_products = sorted(info["products"].items(), key=lambda item: item[1]["name"].lower())
        buttons = [[InlineKeyboardButton(text=p["name"], callback_data=f"pdelid_{pid}")] for pid, p in sorted_products]
        await callback.message.answer("O'chirmoqchi bo'lgan mahsulotni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()

    @dp.callback_query(F.data.startswith("pdelid_"))
    async def pdelid_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        pid = callback.data.split("_", 1)[1]
        removed = info["products"].pop(pid, None)
        save_data()
        if removed:
            await callback.message.answer(f"🗑 O'chirildi: {removed['name']}")
        await callback.answer()

    @dp.callback_query(F.data.startswith("buy_"))
    async def buy_cb(callback: CallbackQuery):
        if not await check_active(callback, info, admin_id):
            return
        if not await require_subscription(callback, info, admin_id):
            return
        pid = callback.data.split("_", 1)[1]
        uid = str(callback.from_user.id)
        product = info["products"].get(pid)
        if not product or product["qty"] <= 0:
            await callback.answer("❌ Mahsulot tugagan.", show_alert=True)
            return
        cart = info["carts"].setdefault(uid, {})
        cart[pid] = cart.get(pid, 0) + 1
        save_data()
        total = sum(info["products"][p]["price"] * q for p, q in cart.items() if p in info["products"])
        await callback.answer(f"✅ Qo'shildi! Savat: {total:,} so'm")

    @dp.callback_query(F.data == "cart")
    async def cart_cb(callback: CallbackQuery):
        await send_cart(callback.from_user.id, callback.message.answer)
        await callback.answer()

    @dp.callback_query(F.data == "cart_clear")
    async def cart_clear_cb(callback: CallbackQuery):
        uid = str(callback.from_user.id)
        info["carts"][uid] = {}
        save_data()
        await callback.message.answer("🗑 Savat tozalandi.")
        await callback.answer()

    def location_kb():
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📍 Joylashuvni yuborish", request_location=True)]],
            resize_keyboard=True, one_time_keyboard=True,
        )

    def contact_kb():
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📞 Raqamni yuborish", request_contact=True)]],
            resize_keyboard=True, one_time_keyboard=True,
        )

    @dp.callback_query(F.data == "checkout")
    async def checkout_cb(callback: CallbackQuery, state: FSMContext):
        uid = str(callback.from_user.id)
        cart = info["carts"].get(uid, {})
        if not cart:
            await callback.answer("Savat bo'sh.", show_alert=True)
            return
        await callback.message.answer(
            "📍 Yetkazib berish manzilini yuboring — pastdagi tugma orqali joylashuvingizni ulashing:",
            reply_markup=location_kb(),
        )
        await state.set_state(Checkout.waiting_address)
        await callback.answer()

    @dp.message(Checkout.waiting_address, F.location)
    async def checkout_address_location(message: Message, state: FSMContext):
        lat, lon = message.location.latitude, message.location.longitude
        address = f"https://maps.google.com/?q={lat},{lon}"
        await state.update_data(address=address)
        await message.answer("📞 Endi telefon raqamingizni yuboring:", reply_markup=contact_kb())
        await state.set_state(Checkout.waiting_phone)

    @dp.message(Checkout.waiting_address)
    async def checkout_address_text(message: Message, state: FSMContext):
        await state.update_data(address=message.text.strip())
        await message.answer("📞 Endi telefon raqamingizni yuboring:", reply_markup=contact_kb())
        await state.set_state(Checkout.waiting_phone)

    @dp.message(Checkout.waiting_phone, F.contact)
    async def checkout_phone_contact(message: Message, state: FSMContext):
        await finalize_order(message, state, message.contact.phone_number)

    @dp.message(Checkout.waiting_phone)
    async def checkout_phone_text(message: Message, state: FSMContext):
        await finalize_order(message, state, message.text.strip())

    async def finalize_order(message: Message, state: FSMContext, phone: str):
        state_data = await state.get_data()
        address = state_data.get("address", "-")

        uid = str(message.from_user.id)
        cart = info["carts"].get(uid, {})
        lines = []
        total = 0
        for pid, qty in cart.items():
            p = info["products"].get(pid)
            if not p:
                continue
            subtotal = p["price"] * qty
            total += subtotal
            lines.append(f"{p['name']} x{qty} = {subtotal:,} so'm")
            p["qty"] = max(0, p["qty"] - qty)

        username = message.from_user.username or message.from_user.id
        order_text = (
            f"🛒 <b>Yangi buyurtma!</b>\n"
            f"Xaridor: @{username}\n"
            f"📍 Manzil: {address}\n"
            f"📞 Telefon: {phone}\n\n"
            + "\n".join(lines)
            + f"\n\n💰 Jami: {total:,} so'm"
        )
        await message.bot.send_message(admin_id, order_text)

        info["carts"][uid] = {}
        info["stats"]["orders"] += 1
        info["stats"]["revenue"] += total
        info.setdefault("order_history", {})
        info["order_history"].setdefault(uid, []).append({
            "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "lines": lines,
            "total": total,
            "address": address,
            "phone": phone,
        })
        save_data()

        await message.answer("✅ Buyurtmangiz qabul qilindi! Tez orada siz bilan bog'lanishadi.", reply_markup=ReplyKeyboardRemove())
        await state.clear()

    @dp.message(F.text == "📜 Buyurtmalarim")
    async def my_orders(message: Message):
        uid = str(message.from_user.id)
        orders = info.get("order_history", {}).get(uid, [])
        if not orders:
            await message.answer("Sizda hali buyurtmalar yo'q.")
            return
        text = "📜 <b>Buyurtmalarim:</b>\n\n"
        for o in orders[-10:]:
            text += f"🗓 {o['date']}\n" + "\n".join(o["lines"]) + f"\n💰 Jami: {o['total']:,} so'm\n\n"
        await message.answer(text)


# ---------- AI-yordamchi bot ----------
def setup_ai_bot(dp: Dispatcher, token: str):
    info = data["bots"][token]
    admin_id = info["admin_id"]
    info["stats"].setdefault("questions", 0)
    setup_subscription_handlers(dp, token, admin_id)
    setup_admin_management(dp, token)
    setup_global_buttons_handler(dp, lambda m, s: astart(m))

    def ai_admin_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="🔄 Yangi suhbat"), KeyboardButton(text="📊 Statistika")],
            [KeyboardButton(text="📢 Xabar yuborish")],
            [KeyboardButton(text="📡 Majburiy obuna"), KeyboardButton(text="👤 Adminlar")],
        ] + get_global_button_rows(), resize_keyboard=True)

    def ai_user_kb():
        return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🔄 Yangi suhbat")]] + get_global_button_rows(), resize_keyboard=True)

    @dp.message(Command("start"))
    async def astart(message: Message):
        uid = message.from_user.id
        if uid not in info["users"]:
            info["users"].append(uid)
            save_data()
        if not await check_active(message, info, admin_id):
            return
        if is_admin(info, uid):
            await message.answer(
                "🤖 Salom! Pastdagi menyudan foydalaning 👇\nSavol yozsangiz ham javob beraman.",
                reply_markup=ai_admin_kb(),
            )
        else:
            await message.answer(
                "🤖 Salom! Menga istalgan savolni yozing, sun'iy intellekt sifatida javob beraman.",
                reply_markup=ai_user_kb(),
            )

    @dp.message(Command("stats"))
    @dp.message(F.text == "📊 Statistika")
    async def ai_stats(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(
            f"📊 <b>Statistika</b>\n\n"
            f"👥 Foydalanuvchilar: {len(info['users'])}\n"
            f"❓ Savollar soni: {info['stats']['questions']}"
        )

    @dp.message(F.text == "🔄 Yangi suhbat")
    async def reset_chat(message: Message):
        info.setdefault("ai_history", {})
        info["ai_history"][str(message.from_user.id)] = []
        save_data()
        await message.answer("🔄 Suhbat tarixi tozalandi. Yangi savol yozing.")

    @dp.message(F.text == "📢 Xabar yuborish")
    async def ai_newpost_cb(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("E'lon matnini yuboring:")
        await state.set_state(PostFlow.waiting_text)

    @dp.message(PostFlow.waiting_text)
    async def ai_post_text(message: Message, state: FSMContext):
        await state.update_data(text=message.text)
        buttons = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Yuborish", callback_data="ai_post_confirm")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="ai_post_cancel")],
        ])
        await message.answer(
            f"Quyidagi xabar {len(info['users'])} kishiga yuborilsinmi?\n\n{message.text}",
            reply_markup=buttons,
        )
        await state.set_state(PostFlow.waiting_confirm)

    @dp.callback_query(F.data == "ai_post_confirm", PostFlow.waiting_confirm)
    async def ai_post_confirm_cb(callback: CallbackQuery, state: FSMContext):
        state_data = await state.get_data()
        text = state_data.get("text", "")
        count = 0
        for uid in info["users"]:
            try:
                await callback.bot.send_message(uid, text)
                count += 1
            except Exception:
                pass
        await callback.message.edit_text(f"✅ {count} ta foydalanuvchiga yuborildi.")
        await state.clear()
        await callback.answer()

    @dp.callback_query(F.data == "ai_post_cancel", PostFlow.waiting_confirm)
    async def ai_post_cancel_cb(callback: CallbackQuery, state: FSMContext):
        await callback.message.edit_text("❌ Bekor qilindi.")
        await state.clear()
        await callback.answer()

    @dp.message(F.text)
    async def ai_chat(message: Message):
        if not await check_active(message, info, admin_id):
            return
        if not await require_subscription(message, info, admin_id):
            return
        info["stats"]["questions"] += 1
        info.setdefault("ai_history", {})
        uid = str(message.from_user.id)
        history = info["ai_history"].setdefault(uid, [])

        contents = list(history) + [{"role": "user", "parts": [{"text": message.text}]}]

        await message.bot.send_chat_action(message.chat.id, "typing")
        thinking = await message.answer("💭 O'ylayapman...")
        try:
            answer = await ask_gemini_chat(contents)
            await thinking.edit_text(answer)
            history.append({"role": "user", "parts": [{"text": message.text}]})
            history.append({"role": "model", "parts": [{"text": answer}]})
            info["ai_history"][uid] = history[-12:]  # oxirgi 6 ta savol-javobni saqlaymiz
            save_data()
        except Exception as e:
            logging.error(f"Xatolik: {e}")
            await thinking.edit_text("Xatolik yuz berdi, birozdan keyin qayta urinib ko'ring.")


# ---------- E'lon/Xabar bot ----------
class WelcomeFlow(StatesGroup):
    waiting_text = State()


def setup_post_bot(dp: Dispatcher, token: str):
    info = data["bots"][token]
    admin_id = info["admin_id"]
    info["stats"].setdefault("posts_sent", 0)
    info.setdefault("welcome_text", "📢 Yangiliklarga obuna bo'ldingiz!")
    setup_subscription_handlers(dp, token, admin_id)
    setup_admin_management(dp, token)
    setup_global_buttons_handler(dp, lambda m, s: pstart(m))

    def admin_kb():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Xabar yuborish", callback_data="newpost")],
            [InlineKeyboardButton(text="📊 Statistika", callback_data="pstats")],
        ])

    def post_menu_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="✏️ Salom xabarini sozlash")],
            [KeyboardButton(text="📡 Majburiy obuna"), KeyboardButton(text="👤 Adminlar")],
        ] + get_global_button_rows(), resize_keyboard=True)

    @dp.message(Command("start"))
    async def pstart(message: Message):
        uid = message.from_user.id
        if uid not in info["users"]:
            info["users"].append(uid)
            save_data()
        if not await check_active(message, info, admin_id):
            return
        if is_admin(info, uid):
            await message.answer("📢 <b>E'lon bot boshqaruvi</b>", reply_markup=admin_kb())
            await message.answer("Qo'shimcha bo'limlar 👇", reply_markup=post_menu_kb())
        else:
            await message.answer(info["welcome_text"])

    @dp.message(F.text == "✏️ Salom xabarini sozlash")
    async def welcome_edit_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(
            f"Hozirgi salom xabari:\n\n{info['welcome_text']}\n\nYangi xabar matnini yuboring:"
        )
        await state.set_state(WelcomeFlow.waiting_text)

    @dp.message(WelcomeFlow.waiting_text)
    async def welcome_edit_save(message: Message, state: FSMContext):
        info["welcome_text"] = message.text
        save_data()
        await message.answer("✅ Salom xabari yangilandi.")
        await state.clear()

    @dp.message(Command("stats"))
    @dp.message(F.text == "📊 Statistika")
    @dp.callback_query(F.data == "pstats")
    async def post_stats(event):
        if not is_admin(info, event.from_user.id):
            return
        text = (
            f"📊 <b>Statistika</b>\n\n"
            f"👥 Obunachilar: {len(info['users'])}\n"
            f"📤 Yuborilgan e'lonlar: {info['stats']['posts_sent']}"
        )
        if isinstance(event, CallbackQuery):
            await event.message.answer(text)
            await event.answer()
        else:
            await event.answer(text)

    @dp.callback_query(F.data == "newpost")
    @dp.message(F.text == "📢 Xabar yuborish")
    async def newpost_cb(event, state: FSMContext):
        if not is_admin(info, event.from_user.id):
            return
        text = "E'lon matnini yuboring (rasm yubormoqchi bo'lsangiz, rasmni izoh/caption bilan yuboring):"
        if isinstance(event, CallbackQuery):
            await event.message.answer(text)
            await event.answer()
        else:
            await event.answer(text)
        await state.set_state(PostFlow.waiting_text)

    @dp.message(PostFlow.waiting_text, F.photo)
    async def post_photo(message: Message, state: FSMContext):
        await state.update_data(photo=message.photo[-1].file_id, text=message.caption or "")
        buttons = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Yuborish", callback_data="post_confirm")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="post_cancel")],
        ])
        await message.answer_photo(
            message.photo[-1].file_id,
            caption=f"Shu rasm {len(info['users'])} kishiga yuborilsinmi?\n\n{message.caption or ''}",
            reply_markup=buttons,
        )
        await state.set_state(PostFlow.waiting_confirm)

    @dp.message(PostFlow.waiting_text)
    async def post_text(message: Message, state: FSMContext):
        await state.update_data(text=message.text, photo=None)
        buttons = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Yuborish", callback_data="post_confirm")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="post_cancel")],
        ])
        await message.answer(
            f"Quyidagi xabar {len(info['users'])} kishiga yuborilsinmi?\n\n{message.text}",
            reply_markup=buttons,
        )
        await state.set_state(PostFlow.waiting_confirm)

    @dp.callback_query(F.data == "post_confirm", PostFlow.waiting_confirm)
    async def post_confirm_cb(callback: CallbackQuery, state: FSMContext):
        state_data = await state.get_data()
        text = state_data.get("text", "")
        photo = state_data.get("photo")
        count = 0
        for uid in info["users"]:
            try:
                if photo:
                    await callback.bot.send_photo(uid, photo, caption=text)
                else:
                    await callback.bot.send_message(uid, text)
                count += 1
            except Exception:
                pass
        info["stats"]["posts_sent"] += 1
        save_data()
        await callback.message.edit_caption(caption=f"✅ {count} ta foydalanuvchiga yuborildi.") if photo else await callback.message.edit_text(f"✅ {count} ta foydalanuvchiga yuborildi.")
        await state.clear()
        await callback.answer()

    @dp.callback_query(F.data == "post_cancel", PostFlow.waiting_confirm)
    async def post_cancel_cb(callback: CallbackQuery, state: FSMContext):
        await callback.message.edit_text("❌ Bekor qilindi.")
        await state.clear()
        await callback.answer()


# ---------- Pul (valyuta) bot ----------
def setup_money_bot(dp: Dispatcher, token: str):
    info = data["bots"][token]
    admin_id = info["admin_id"]
    info["stats"].setdefault("conversions", 0)
    info.setdefault("rates", {"USD": 12650, "EUR": 13700, "RUB": 140})
    setup_subscription_handlers(dp, token, admin_id)
    setup_admin_management(dp, token)
    setup_global_buttons_handler(dp, lambda m, s: mstart(m))

    def admin_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="➕ Valyuta qo'shish"), KeyboardButton(text="✏️ Kursni yangilash")],
            [KeyboardButton(text="🗑 Valyutani o'chirish"), KeyboardButton(text="📊 Statistika")],
            [KeyboardButton(text="📡 Majburiy obuna"), KeyboardButton(text="👤 Adminlar")],
        ] + get_global_button_rows(), resize_keyboard=True)

    def currency_kb():
        buttons = [[InlineKeyboardButton(text=code, callback_data=f"curr_{code}")] for code in info["rates"]]
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @dp.message(Command("start"))
    async def mstart(message: Message):
        uid = message.from_user.id
        if uid not in info["users"]:
            info["users"].append(uid)
            save_data()
        if not await check_active(message, info, admin_id):
            return
        if is_admin(info, uid):
            rates_text = "\n".join(f"{c}: {r:,} so'm" for c, r in info["rates"].items()) or "Hozircha valyuta yo'q."
            await message.answer(f"💱 <b>Pul bot boshqaruvi</b>\n\nJoriy kurslar:\n{rates_text}", reply_markup=admin_kb())
            return
        if not await require_subscription(message, info, admin_id):
            return
        if not info["rates"]:
            await message.answer("Hozircha valyutalar qo'shilmagan.")
            return
        await message.answer("💱 Valyutani tanlang:", reply_markup=currency_kb())

    @dp.message(Command("stats"))
    @dp.message(F.text == "📊 Statistika")
    async def money_stats(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(
            f"📊 <b>Statistika</b>\n\n👥 Foydalanuvchilar: {len(info['users'])}\n"
            f"💱 Konvertatsiyalar: {info['stats']['conversions']}\n💰 Valyutalar soni: {len(info['rates'])}"
        )

    @dp.message(F.text == "➕ Valyuta qo'shish")
    async def add_currency_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Valyuta kodini yozing (masalan: GBP, CNY, TRY, KZT):")
        await state.set_state(CurrencyAdd.waiting_code)

    @dp.message(CurrencyAdd.waiting_code)
    async def add_currency_code(message: Message, state: FSMContext):
        code = message.text.strip().upper()
        if not code.isalpha() or len(code) > 6:
            await message.answer("❌ Kodni to'g'ri kiriting (masalan: GBP).")
            return
        await state.update_data(code=code)
        await message.answer(f"1 {code} necha so'm? (faqat raqam):")
        await state.set_state(CurrencyAdd.waiting_rate)

    @dp.message(CurrencyAdd.waiting_rate)
    async def add_currency_rate(message: Message, state: FSMContext):
        try:
            rate = float(message.text.strip().replace(" ", "").replace(",", "."))
        except ValueError:
            await message.answer("❌ Faqat raqam kiriting.")
            return
        state_data = await state.get_data()
        code = state_data.get("code")
        info["rates"][code] = rate
        save_data()
        await message.answer(f"✅ {code} qo'shildi: 1 {code} = {rate:,} so'm")
        await state.clear()

    @dp.message(F.text == "✏️ Kursni yangilash")
    async def update_rate_start(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["rates"]:
            await message.answer("Hozircha valyuta yo'q. Avval qo'shing.")
            return
        buttons = [[InlineKeyboardButton(text=f"{c} ({r:,})", callback_data=f"updrate_{c}")] for c, r in info["rates"].items()]
        await message.answer("Qaysi valyuta kursini yangilaymiz?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("updrate_"))
    async def update_rate_pick(callback: CallbackQuery, state: FSMContext):
        if not is_admin(info, callback.from_user.id):
            return
        code = callback.data.split("_", 1)[1]
        await state.update_data(update_code=code)
        await callback.message.answer(f"1 {code} uchun yangi kursni kiriting (so'm):")
        await state.set_state(CurrencyUpdate.waiting_rate)
        await callback.answer()

    @dp.message(CurrencyUpdate.waiting_rate)
    async def update_rate_save(message: Message, state: FSMContext):
        try:
            rate = float(message.text.strip().replace(" ", "").replace(",", "."))
        except ValueError:
            await message.answer("❌ Faqat raqam kiriting.")
            return
        state_data = await state.get_data()
        code = state_data.get("update_code")
        if code in info["rates"]:
            info["rates"][code] = rate
            save_data()
            await message.answer(f"✅ {code} kursi yangilandi: {rate:,} so'm")
        await state.clear()

    @dp.message(F.text == "🗑 Valyutani o'chirish")
    async def del_currency_start(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["rates"]:
            await message.answer("O'chirish uchun valyuta yo'q.")
            return
        buttons = [[InlineKeyboardButton(text=c, callback_data=f"delcurr_{c}")] for c in info["rates"]]
        await message.answer("O'chirmoqchi bo'lgan valyutani tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("delcurr_"))
    async def del_currency_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        code = callback.data.split("_", 1)[1]
        removed = info["rates"].pop(code, None)
        save_data()
        if removed is not None:
            await callback.message.answer(f"🗑 {code} o'chirildi.")
        await callback.answer()

    @dp.callback_query(F.data.startswith("curr_"))
    async def pick_currency(callback: CallbackQuery, state: FSMContext):
        currency = callback.data.split("_", 1)[1]
        await state.update_data(currency=currency)
        await callback.message.answer(f"{currency} miqdorini kiriting:")
        await state.set_state(MoneyAmount.waiting_amount)
        await callback.answer()

    @dp.message(MoneyAmount.waiting_amount)
    async def calc_amount(message: Message, state: FSMContext):
        try:
            amount = float(message.text.strip().replace(",", "."))
        except ValueError:
            await message.answer("❌ Faqat raqam kiriting.")
            return
        state_data = await state.get_data()
        currency = state_data.get("currency", "USD")
        rate = info["rates"].get(currency, 0)
        total = amount * rate
        info["stats"]["conversions"] += 1
        save_data()
        await message.answer(f"💱 {amount:,.2f} {currency} = <b>{total:,.0f} so'm</b>")
        await state.clear()


# ---------- Tarjimon bot ----------
def setup_translate_bot(dp: Dispatcher, token: str):
    info = data["bots"][token]
    admin_id = info["admin_id"]
    info["stats"].setdefault("translations", 0)
    info.setdefault("user_lang", {})
    setup_subscription_handlers(dp, token, admin_id)
    setup_admin_management(dp, token)
    setup_global_buttons_handler(dp, lambda m, s: tstart(m))

    LANGS = {"uz": "🇺🇿 O'zbek", "en": "🇬🇧 English", "ru": "🇷🇺 Русский", "tr": "🇹🇷 Türkçe", "ar": "🇸🇦 العربية"}

    def lang_kb():
        buttons = [[InlineKeyboardButton(text=name, callback_data=f"lang_{code}")] for code, name in LANGS.items()]
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    def lang_chosen_kb():
        return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🔄 Tilni o'zgartirish")]] + get_global_button_rows(), resize_keyboard=True)

    def admin_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="📊 Statistika"), KeyboardButton(text="📢 Xabar yuborish")],
            [KeyboardButton(text="📡 Majburiy obuna"), KeyboardButton(text="👤 Adminlar")],
        ] + get_global_button_rows(), resize_keyboard=True)

    @dp.message(Command("start"))
    async def tstart(message: Message):
        uid = message.from_user.id
        if uid not in info["users"]:
            info["users"].append(uid)
            save_data()
        if not await check_active(message, info, admin_id):
            return
        if is_admin(info, uid):
            await message.answer("🌐 <b>Tarjimon bot boshqaruvi</b>", reply_markup=admin_kb())
            return
        if not await require_subscription(message, info, admin_id):
            return
        await message.answer("🌐 Qaysi tilga tarjima qilishni xohlaysiz?", reply_markup=lang_kb())

    @dp.message(Command("stats"))
    @dp.message(F.text == "📊 Statistika")
    async def translate_stats(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(
            f"📊 <b>Statistika</b>\n\n👥 Foydalanuvchilar: {len(info['users'])}\n🌐 Tarjimalar: {info['stats']['translations']}"
        )

    @dp.message(F.text == "📢 Xabar yuborish")
    async def t_newpost(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("E'lon matnini yuboring:")
        await state.set_state(PostFlow.waiting_text)

    @dp.message(PostFlow.waiting_text)
    async def t_post_text(message: Message, state: FSMContext):
        await state.update_data(text=message.text)
        buttons = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Yuborish", callback_data="t_post_confirm")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="t_post_cancel")],
        ])
        await message.answer(f"{len(info['users'])} kishiga yuborilsinmi?\n\n{message.text}", reply_markup=buttons)
        await state.set_state(PostFlow.waiting_confirm)

    @dp.callback_query(F.data == "t_post_confirm", PostFlow.waiting_confirm)
    async def t_post_confirm(callback: CallbackQuery, state: FSMContext):
        state_data = await state.get_data()
        text = state_data.get("text", "")
        count = 0
        for uid in info["users"]:
            try:
                await callback.bot.send_message(uid, text)
                count += 1
            except Exception:
                pass
        await callback.message.edit_text(f"✅ {count} kishiga yuborildi.")
        await state.clear()
        await callback.answer()

    @dp.callback_query(F.data == "t_post_cancel", PostFlow.waiting_confirm)
    async def t_post_cancel(callback: CallbackQuery, state: FSMContext):
        await callback.message.edit_text("❌ Bekor qilindi.")
        await state.clear()
        await callback.answer()

    @dp.callback_query(F.data.startswith("lang_"))
    async def pick_lang(callback: CallbackQuery):
        code = callback.data.split("_", 1)[1]
        info.setdefault("user_lang", {})
        info["user_lang"][str(callback.from_user.id)] = code
        save_data()
        await callback.message.answer(
            f"✅ Til tanlandi: {LANGS[code]}\n\nEndi tarjima qilmoqchi bo'lgan matningizni yuboring.",
            reply_markup=lang_chosen_kb(),
        )
        await callback.answer()

    @dp.message(F.text == "🔄 Tilni o'zgartirish")
    async def change_lang(message: Message):
        await message.answer("🌐 Qaysi tilga tarjima qilishni xohlaysiz?", reply_markup=lang_kb())

    @dp.message(F.text)
    async def do_translate(message: Message):
        if not await check_active(message, info, admin_id):
            return
        if not await require_subscription(message, info, admin_id):
            return
        uid = str(message.from_user.id)
        lang = info.get("user_lang", {}).get(uid)
        if not lang:
            await message.answer("Avval tilni tanlang:", reply_markup=lang_kb())
            return
        lang_name = LANGS.get(lang, lang)
        thinking = await message.answer("💭 Tarjima qilinmoqda...")
        try:
            result = await ask_gemini(
                f"Translate the following text to {lang_name}. Respond with ONLY the translation, nothing else:\n\n{message.text}"
            )
            info["stats"]["translations"] += 1
            save_data()
            await thinking.edit_text(result)
        except Exception as e:
            logging.error(f"Xatolik: {e}")
            await thinking.edit_text("Xatolik yuz berdi.")


# ---------- Aloqa bot ----------
def setup_contact_bot(dp: Dispatcher, token: str):
    info = data["bots"][token]
    admin_id = info["admin_id"]
    info["stats"].setdefault("messages", 0)
    info.setdefault("reply_map", {})
    setup_subscription_handlers(dp, token, admin_id)
    setup_admin_management(dp, token)
    setup_global_buttons_handler(dp, lambda m, s: cstart(m))

    def admin_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="📊 Statistika")],
            [KeyboardButton(text="📡 Majburiy obuna"), KeyboardButton(text="👤 Adminlar")],
        ] + get_global_button_rows(), resize_keyboard=True)

    @dp.message(Command("start"))
    async def cstart(message: Message):
        uid = message.from_user.id
        if uid not in info["users"]:
            info["users"].append(uid)
            save_data()
        if not await check_active(message, info, admin_id):
            return
        if is_admin(info, uid):
            await message.answer(
                "📞 <b>Aloqa bot boshqaruvi</b>\n\nFoydalanuvchi xabar yuborsa, sizga keladi. "
                "Javob berish uchun o'sha xabarga REPLY qilib yozing.",
                reply_markup=admin_kb(),
            )
            return
        await message.answer("📞 Xabaringizni yozing, tez orada javob beramiz.")

    @dp.message(Command("stats"))
    @dp.message(F.text == "📊 Statistika")
    async def contact_stats(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(
            f"📊 <b>Statistika</b>\n\n👥 Foydalanuvchilar: {len(info['users'])}\n✉️ Xabarlar: {info['stats']['messages']}"
        )

    @dp.message(F.text)
    async def route_message(message: Message):
        uid = message.from_user.id
        if is_admin(info, uid):
            if message.reply_to_message:
                target = info["reply_map"].get(str(message.reply_to_message.message_id))
                if target:
                    try:
                        await message.bot.send_message(target, f"💬 <b>Javob:</b>\n\n{message.text}")
                        await message.answer("✅ Yuborildi.")
                    except Exception:
                        await message.answer("❌ Yuborib bo'lmadi.")
            return
        if not await check_active(message, info, admin_id):
            return
        if not await require_subscription(message, info, admin_id):
            return
        info["stats"]["messages"] += 1
        save_data()
        username = message.from_user.username or uid
        sent = await message.bot.send_message(
            admin_id, f"✉️ <b>Yangi xabar</b>\nKimdan: @{username} (ID: {uid})\n\n{message.text}"
        )
        info["reply_map"][str(sent.message_id)] = uid
        save_data()
        await message.answer("✅ Xabaringiz yuborildi, tez orada javob beramiz.")


# ---------- Anketa bot ----------
def setup_survey_bot(dp: Dispatcher, token: str):
    info = data["bots"][token]
    admin_id = info["admin_id"]
    info["stats"].setdefault("responses", 0)
    info.setdefault("questions", [])
    info.setdefault("responses_data", {})
    setup_subscription_handlers(dp, token, admin_id)
    setup_admin_management(dp, token)
    setup_global_buttons_handler(dp, lambda m, s: survstart(m, s))

    def admin_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="➕ Savol qo'shish"), KeyboardButton(text="📋 Savollar")],
            [KeyboardButton(text="🗑 Savolni o'chirish"), KeyboardButton(text="📊 Statistika")],
            [KeyboardButton(text="📡 Majburiy obuna"), KeyboardButton(text="👤 Adminlar")],
        ] + get_global_button_rows(), resize_keyboard=True)

    @dp.message(Command("start"))
    async def survstart(message: Message, state: FSMContext):
        uid = message.from_user.id
        if uid not in info["users"]:
            info["users"].append(uid)
            save_data()
        if not await check_active(message, info, admin_id):
            return
        if is_admin(info, uid):
            await message.answer("📝 <b>Anketa bot boshqaruvi</b>", reply_markup=admin_kb())
            return
        if not await require_subscription(message, info, admin_id):
            return
        if not info["questions"]:
            await message.answer("Hozircha savollar yo'q.")
            return
        await state.update_data(answers=[], q_index=0)
        await state.set_state(SurveyAnswer.answering)
        await message.answer(f"📝 Anketa boshlandi!\n\n1) {info['questions'][0]}")

    @dp.message(Command("stats"))
    @dp.message(F.text == "📊 Statistika")
    async def survey_stats(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(
            f"📊 <b>Statistika</b>\n\n👥 Foydalanuvchilar: {len(info['users'])}\n"
            f"📝 Javob berganlar: {info['stats']['responses']}\n❓ Savollar soni: {len(info['questions'])}"
        )

    @dp.message(F.text == "➕ Savol qo'shish")
    async def add_question_start(message: Message, state: FSMContext):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer("Savol matnini yozing:")
        await state.set_state(SurveyAdmin.waiting_question)

    @dp.message(SurveyAdmin.waiting_question)
    async def add_question_save(message: Message, state: FSMContext):
        info["questions"].append(message.text.strip())
        save_data()
        await message.answer(f"✅ Savol qo'shildi ({len(info['questions'])}-savol).")
        await state.clear()

    @dp.message(F.text == "📋 Savollar")
    async def list_questions(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["questions"]:
            await message.answer("Savollar yo'q.")
            return
        text = "📋 <b>Savollar:</b>\n\n" + "\n".join(f"{i+1}) {q}" for i, q in enumerate(info["questions"]))
        await message.answer(text)

    @dp.message(F.text == "🗑 Savolni o'chirish")
    async def del_question_start(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        if not info["questions"]:
            await message.answer("O'chirish uchun savol yo'q.")
            return
        buttons = [[InlineKeyboardButton(text=f"{i+1}) {q[:30]}", callback_data=f"delq_{i}")] for i, q in enumerate(info["questions"])]
        await message.answer("O'chirmoqchi bo'lgan savolni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @dp.callback_query(F.data.startswith("delq_"))
    async def del_question_cb(callback: CallbackQuery):
        if not is_admin(info, callback.from_user.id):
            return
        idx = int(callback.data.split("_", 1)[1])
        if 0 <= idx < len(info["questions"]):
            removed = info["questions"].pop(idx)
            save_data()
            await callback.message.answer(f"🗑 O'chirildi: {removed}")
        await callback.answer()

    @dp.message(SurveyAnswer.answering)
    async def collect_answer(message: Message, state: FSMContext):
        state_data = await state.get_data()
        answers = state_data.get("answers", [])
        q_index = state_data.get("q_index", 0)
        answers.append(message.text)
        q_index += 1
        if q_index >= len(info["questions"]):
            uid = str(message.from_user.id)
            info["responses_data"][uid] = answers
            info["stats"]["responses"] += 1
            save_data()
            await message.answer("✅ Anketa yakunlandi! Rahmat.")
            await state.clear()
        else:
            await state.update_data(answers=answers, q_index=q_index)
            await message.answer(f"{q_index+1}) {info['questions'][q_index]}")


# ---------- Ob-havo bot ----------
def setup_weather_bot(dp: Dispatcher, token: str):
    info = data["bots"][token]
    admin_id = info["admin_id"]
    info["stats"].setdefault("lookups", 0)
    setup_subscription_handlers(dp, token, admin_id)
    setup_admin_management(dp, token)
    setup_global_buttons_handler(dp, lambda m, s: wstart(m))

    def admin_kb():
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="📊 Statistika")],
            [KeyboardButton(text="📡 Majburiy obuna"), KeyboardButton(text="👤 Adminlar")],
        ], resize_keyboard=True)

    def user_kb():
        return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🌤 Ob-havoni bilish")]], resize_keyboard=True)

    async def fetch_weather(city: str):
        url = f"https://wttr.in/{city}?format=j1"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers={"User-Agent": "curl"})
            resp.raise_for_status()
            return resp.json()

    @dp.message(Command("start"))
    async def wstart(message: Message):
        uid = message.from_user.id
        if uid not in info["users"]:
            info["users"].append(uid)
            save_data()
        if not await check_active(message, info, admin_id):
            return
        if is_admin(info, uid):
            await message.answer("🌤 <b>Ob-havo bot boshqaruvi</b>", reply_markup=admin_kb())
            return
        if not await require_subscription(message, info, admin_id):
            return
        await message.answer("🌤 Ob-havo botiga xush kelibsiz!", reply_markup=user_kb())

    @dp.message(Command("stats"))
    @dp.message(F.text == "📊 Statistika")
    async def weather_stats(message: Message):
        if not is_admin(info, message.from_user.id):
            return
        await message.answer(
            f"📊 <b>Statistika</b>\n\n👥 Foydalanuvchilar: {len(info['users'])}\n🌤 So'rovlar: {info['stats']['lookups']}"
        )

    @dp.message(F.text == "🌤 Ob-havoni bilish")
    async def ask_city(message: Message, state: FSMContext):
        if not await check_active(message, info, admin_id):
            return
        if not await require_subscription(message, info, admin_id):
            return
        await message.answer("Shahar nomini yozing (masalan: Tashkent):")
        await state.set_state(WeatherCity.waiting_city)

    @dp.message(WeatherCity.waiting_city)
    async def show_weather(message: Message, state: FSMContext):
        city = message.text.strip()
        try:
            d = await fetch_weather(city)
            cur = d["current_condition"][0]
            temp = cur["temp_C"]
            desc = cur["weatherDesc"][0]["value"]
            humidity = cur["humidity"]
            wind = cur["windspeedKmph"]
            info["stats"]["lookups"] += 1
            save_data()
            await message.answer(
                f"🌤 <b>{city}</b>\n\n🌡 Harorat: {temp}°C\n☁️ Holat: {desc}\n💧 Namlik: {humidity}%\n💨 Shamol: {wind} km/soat"
            )
        except Exception as e:
            logging.error(f"Xatolik: {e}")
            await message.answer("❌ Ob-havo ma'lumotini olishda xatolik. Shahar nomini tekshirib qayta urinib ko'ring.")
        await state.clear()



SETUP_FUNCTIONS = {
    "kino": setup_kino_bot,
    "shop": setup_shop_bot,
    "ai": setup_ai_bot,
    "post": setup_post_bot,
    "money": setup_money_bot,
    "translate": setup_translate_bot,
    "contact": setup_contact_bot,
    "survey": setup_survey_bot,
    "weather": setup_weather_bot,
}


def get_global_button_rows():
    rows = [[KeyboardButton(text="◀️ Orqaga")]]
    rows += [[KeyboardButton(text=b["label"])] for b in data.get("global_buttons", [])]
    return rows


def is_global_button_text(message: Message) -> bool:
    if not message.text:
        return False
    return any(message.text == b["label"] for b in data.get("global_buttons", []))


def setup_global_buttons_handler(dp: Dispatcher, start_func=None):
    @dp.message(F.text == "◀️ Orqaga")
    async def back_button_handler(message: Message, state: FSMContext):
        await state.clear()
        if start_func:
            await start_func(message, state)
        else:
            await message.answer("🏠 Bosh menyuga qaytish uchun /start bosing.")

    @dp.message(is_global_button_text)
    async def global_button_handler(message: Message):
        for b in data.get("global_buttons", []):
            if b["label"] == message.text:
                await message.answer(b["response"])
                return


async def start_child_bot(token: str, bot_type: str):
    if token in running_bots:
        return
    child_bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    child_dp = Dispatcher(storage=MemoryStorage())
    SETUP_FUNCTIONS[bot_type](child_dp, token)
    task = asyncio.create_task(child_dp.start_polling(child_bot))
    running_bots[token] = task


async def trial_warning_loop():
    """Har 6 soatda barcha botlarni tekshirib, sinov/to'lov muddati tugashiga 1 kun qolganlarga ogohlantirish yuboradi."""
    while True:
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            for token, info in data["bots"].items():
                if info.get("paid_until"):
                    expiry = datetime.fromisoformat(info["paid_until"])
                    kind = "to'lov"
                else:
                    expiry = datetime.fromisoformat(info["created_at"]) + timedelta(days=TRIAL_DAYS)
                    kind = "sinov"

                days_left = (expiry - datetime.now()).total_seconds() / 86400
                if 0 <= days_left <= 1 and info.get("last_warned_date") != today:
                    amount = next_payment_amount(info)
                    try:
                        await main_bot.send_message(
                            info["admin_id"],
                            f"⏳ <b>Ogohlantirish!</b>\n\n"
                            f"{BOT_TYPES.get(info['type'])} (<b>{info['name']}</b>) uchun {kind} muddati "
                            f"taxminan 1 kundan keyin tugaydi.\n\n"
                            f"Davom ettirish uchun to'lov: <b>{amount:,} so'm</b>.\n"
                            "To'lovni amalga oshirish uchun administrator bilan bog'laning.",
                            reply_markup=contact_admin_kb(),
                        )
                    except Exception as e:
                        logging.error(f"Ogohlantirish yuborishda xato ({token}): {e}")
                    info["last_warned_date"] = today
                    save_data()
        except Exception as e:
            logging.error(f"trial_warning_loop xatosi: {e}")

        await asyncio.sleep(6 * 60 * 60)  # 6 soat


async def main():
    for token, info in data["bots"].items():
        info.setdefault("stats", {})
        await start_child_bot(token, info["type"])
    for clone in data.get("platform_clones", []):
        await start_platform_clone(clone["token"], clone.get("username"))
    asyncio.create_task(trial_warning_loop())
    await main_dp.start_polling(main_bot)


if __name__ == "__main__":
    asyncio.run(main())
