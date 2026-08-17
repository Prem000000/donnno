"""
Discord Grinding Bot - Telegram Controlled
Features:
- Multi-user sessions (each Telegram user = separate grinder)
- Add/remove Discord tokens via Telegram
- Add/remove target channels
- Add custom words for AI to use
- Start/stop grinding per user
- VPS deployment ready

BEHAVIORAL MODEL:
- 450-500 messages/day per account
- 90-95% replies (via on_message), 5-10% standalone (via grinding_loop)
- Per-person reply limits, channel cooldowns
- Bot accusation denial, admin warning detection
- English default language, Hinglish if chat context is Hinglish
"""

import os
import json
import asyncio
import random
import re
import time
import logging
import signal
import sys
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime, timezone, timedelta
from functools import wraps

# Telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

# Discord
import discord

# For AgentRouter - Using Anthropic client
from anthropic import Anthropic

# ========================== CONFIG ==========================

# Get tokens from environment variables (Railway)
TG_BOT_TOKEN = os.environ.get('TG_BOT_TOKEN', "8411649204:AAGPnQhIMKKB1rhfoSgGz2ZBtBokNZX1eH4")
AGENTROUTER_API_KEY = os.environ.get('AGENTROUTER_API_KEY', "YOUR_AGENTROUTER_API_KEY_HERE")
AGENTROUTER_BASE_URL = "https://agentrouter.org"

AGENTROUTER_MODELS = [
    "claude-opus-4-8",
]

DEFAULT_API_KEYS = [
    AGENTROUTER_API_KEY,
]

SESSIONS_DIR = Path("user_sessions")
SESSIONS_DIR.mkdir(exist_ok=True)

FORCE_JOIN_CHANNELS = []

BOT_NAME = "⚡ DC GRIND BOT ⚡"
BOT_TAGLINE = "by Thakur Saab 🔥"
ADMIN_USERNAME = "@kittu2141"
ADMIN_IDS = [2095029151]

BOT_DENIAL_RESPONSES = [
    "lol prove it bro, im literally typing from my phone",
    "haha prove it then, ask me anything",
    "prove it bro, what makes you think that",
    "bro prove it, im just chilling here like everyone else",
    "lmaooo prove it, im a real person bro",
    "nah bro im real, prove it if you think otherwise",
    "bro seriously? prove it then lol",
    "ok ok prove it, im curious what made you say that",
    "lol what? prove it bro, im literally eating rn",
    "haha prove it, bots cant have this much personality",
    "bro prove it, bots dont know about this project like i do",
]

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def human_delay(min_val, max_val):
    mean = (min_val + max_val) / 2
    std_dev = (max_val - min_val) / 4
    delay = random.gauss(mean, std_dev)
    return max(min_val, min(max_val, delay))

# ========================== USER SESSION ==========================

class UserSession:
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.file_path = SESSIONS_DIR / f"{user_id}.json"
        self.data = self.load()
        self.grinders: Dict[str, 'DiscordGrinder'] = {}
        self.running = False
        self._model_index = 0
        self._api_index = 0
        self._failed_keys = set()
    
    def load(self) -> dict:
        if self.file_path.exists():
            with open(self.file_path, 'r') as f:
                data = json.load(f)
                if "api_keys" not in data:
                    data["api_keys"] = []
                if "current_api_index" not in data:
                    data["current_api_index"] = 0
                if "current_model_index" not in data:
                    data["current_model_index"] = 0
                
                if "dc_accounts" not in data:
                    old_tokens = data.get("tokens", [])
                    old_channels = data.get("channels", [])
                    dc_accounts = []
                    for token in old_tokens:
                        dc_accounts.append({
                            "token": token,
                            "name": "Unknown",
                            "channels": list(old_channels)
                        })
                    data["dc_accounts"] = dc_accounts
                    data.pop("tokens", None)
                    data.pop("channels", None)
                    with open(self.file_path, 'w') as fw:
                        json.dump(data, fw, indent=2)
                    logger.info(f"🔄 Migrated user {self.user_id} to dc_accounts format")
                
                return data
        return {
            "dc_accounts": [],
            "custom_words": [],
            "api_keys": [],
            "current_api_index": 0,
            "current_model_index": 0,
            "min_delay": 60,
            "max_delay": 180,
            "reply_chance": 0.4
        }
    
    def save(self):
        with open(self.file_path, 'w') as f:
            json.dump(self.data, f, indent=2)
    
    def add_api_key(self, api_key: str) -> bool:
        if api_key not in self.data["api_keys"]:
            self.data["api_keys"].append(api_key)
            self.save()
            return True
        return False
    
    def remove_api_key(self, index: int) -> bool:
        if 0 <= index < len(self.data["api_keys"]):
            self.data["api_keys"].pop(index)
            self.save()
            return True
        return False
    
    def get_all_keys(self) -> List[str]:
        user_keys = self.data.get("api_keys", [])
        all_keys = user_keys + DEFAULT_API_KEYS
        return [k for k in all_keys if k not in self._failed_keys]
    
    def get_next_api_key(self) -> str:
        all_keys = self.get_all_keys()
        if not all_keys:
            return DEFAULT_API_KEYS[0] if DEFAULT_API_KEYS else None
        
        index = self.data.get("current_api_index", 0) % len(all_keys)
        key = all_keys[index]
        
        self.data["current_api_index"] = (index + 1) % len(all_keys)
        self.save()
        return key
    
    def mark_key_failed(self, key: str):
        if key in DEFAULT_API_KEYS:
            self.rotate_api_key()
        else:
            self._failed_keys.add(key)
            logger.warning(f"❌ API key marked as failed: {key[:15]}...")
    
    def get_next_model(self) -> str:
        index = self.data.get("current_model_index", 0) % len(AGENTROUTER_MODELS)
        model = AGENTROUTER_MODELS[index]
        
        self.data["current_model_index"] = (index + 1) % len(AGENTROUTER_MODELS)
        self.save()
        return model
    
    def rotate_api_key(self):
        all_keys = self.get_all_keys()
        if all_keys:
            current = self.data.get("current_api_index", 0)
            self.data["current_api_index"] = (current + 1) % len(all_keys)
            self.save()
    
    def track_api_failure(self) -> bool:
        if not hasattr(self, '_api_fail_count'):
            self._api_fail_count = 0
        self._api_fail_count += 1
        
        all_keys = self.get_all_keys()
        total_keys = len(all_keys)
        
        if self._api_fail_count >= total_keys * len(AGENTROUTER_MODELS):
            return True
        return False
    
    def reset_api_failures(self):
        self._api_fail_count = 0
    
    async def auto_stop_all_grinders(self):
        if getattr(self, '_auto_stop_sent', False):
            return
        self._auto_stop_sent = True
        self.running = False
        for token, grinder in self.grinders.items():
            grinder.running = False
        logger.warning(f"🛑 Auto-stopped all grinders for user {self.user_id}")
        global tg_bot
        if tg_bot:
            try:
                msg = (
                    "🛑 **AUTO-STOP TRIGGERED!**\n\n"
                    "⚠️ All API keys and models hit rate limit!\n"
                    "Bot has **auto-stopped** grinding.\n\n"
                    "Use `/startgrind` to restart when ready."
                )
                await tg_bot.send_message(chat_id=self.user_id, text=msg)
            except:
                pass
    
    def get_accounts(self) -> list:
        return self.data.get("dc_accounts", [])
    
    def get_all_tokens(self) -> list:
        return [acc["token"] for acc in self.get_accounts()]
    
    def add_token(self, token: str) -> bool:
        existing_tokens = [acc["token"] for acc in self.get_accounts()]
        if token not in existing_tokens:
            self.data["dc_accounts"].append({
                "token": token,
                "name": "Unknown",
                "channels": []
            })
            self.save()
            return True
        return False
    
    def remove_token(self, index: int) -> bool:
        accounts = self.get_accounts()
        if 0 <= index < len(accounts):
            self.data["dc_accounts"].pop(index)
            self.save()
            return True
        return False
    
    def set_account_name(self, token: str, name: str):
        for acc in self.get_accounts():
            if acc["token"] == token:
                acc["name"] = name
                self.save()
                return
    
    def add_channel_to_account(self, account_index: int, channel_id: int) -> bool:
        accounts = self.get_accounts()
        if 0 <= account_index < len(accounts):
            if channel_id not in accounts[account_index]["channels"]:
                accounts[account_index]["channels"].append(channel_id)
                self.save()
                return True
        return False
    
    def remove_channel_from_account(self, account_index: int, channel_index: int) -> bool:
        accounts = self.get_accounts()
        if 0 <= account_index < len(accounts):
            channels = accounts[account_index]["channels"]
            if 0 <= channel_index < len(channels):
                channels.pop(channel_index)
                self.save()
                return True
        return False
    
    def get_account_channels(self, token: str) -> list:
        for acc in self.get_accounts():
            if acc["token"] == token:
                return acc.get("channels", [])
        return []
    
    def add_words(self, words: List[str]):
        for word in words:
            if word not in self.data["custom_words"]:
                self.data["custom_words"].append(word)
        self.save()
    
    def clear_words(self):
        self.data["custom_words"] = []
        self.save()

    def stop_all_grinders(self):
        self.running = False
        for grinder in self.grinders.values():
            grinder.running = False
            try:
                asyncio.create_task(grinder.client.close())
            except:
                pass
        self.grinders.clear()

user_sessions: Dict[int, UserSession] = {}

def get_session(user_id: int) -> UserSession:
    if user_id not in user_sessions:
        user_sessions[user_id] = UserSession(user_id)
    return user_sessions[user_id]

# ========================== AGENTROUTER AI ==========================

def _detect_hinglish(context: List[str]) -> bool:
    hinglish_markers = [
        'bhai', 'yaar', 'kya', 'hai', 'nahi', 'kaise', 'karo', 'acha', 'accha',
        'theek', 'matlab', 'dekho', 'chalo', 'bohot', 'bahut', 'mujhe', 'tujhe',
        'abhi', 'aur', 'lekin', 'kyunki', 'samajh', 'pata', 'sahi', 'galat',
        'bro kya', 'kya baat', 'haan', 'naa', 'kuch', 'sab', 'apna', 'tera',
        'mera', 'uska', 'wala', 'wali', 'kar', 'raha', 'rahi', 'lol bhai',
    ]
    combined = " ".join(context[-10:]).lower()
    count = sum(1 for m in hinglish_markers if m in combined)
    return count >= 3

def get_agentrouter_client(api_key: str = None):
    if not api_key:
        api_key = DEFAULT_API_KEYS[0] if DEFAULT_API_KEYS else None
    return Anthropic(
        api_key=api_key,
        base_url=AGENTROUTER_BASE_URL,
        timeout=30.0,
        max_retries=2
    )

def extract_text_from_response(response) -> str:
    try:
        if hasattr(response, 'content') and response.content:
            for block in response.content:
                if hasattr(block, 'type'):
                    if block.type == "text":
                        return block.text
                    elif block.type == "thinking":
                        continue
                if hasattr(block, 'text'):
                    return block.text
        return ""
    except Exception as e:
        logger.error(f"Error extracting text from response: {e}")
        return ""

async def generate_smart_message(context: List[str], custom_words: List[str] = None, session: 'UserSession' = None, recent_sent: List[str] = None, personality_style: str = None) -> str:
    try:
        api_key = session.get_next_api_key() if session else DEFAULT_API_KEYS[0] if DEFAULT_API_KEYS else None
        if not api_key:
            return random.choice(["this project is looking solid", "bullish on this one", "community is fire here"])
        
        model = session.get_next_model() if session else AGENTROUTER_MODELS[0]
        client = get_agentrouter_client(api_key)
        
        chat_context = "\n".join(context[-15:])
        
        avoid_hint = ""
        if recent_sent:
            avoid_list = list(set(recent_sent[-15:]))
            avoid_hint = f"\n\nNEVER use these phrases (already sent recently): {', '.join(avoid_list)}"
        
        is_hinglish = _detect_hinglish(context)
        lang_hint = "Speak in ENGLISH by default." if not is_hinglish else "The chat is in Hinglish, so use Hinglish (mix of English and Hindi in Roman script). NEVER pure Hindi."
        
        prompt = f"""You are a REAL human community member in a Discord server. You are from INDIA.

LANGUAGE: {lang_hint}

CONVERSATION AWARENESS:
- READ the chat context below.
- Understand what people are discussing.
- Write a STANDALONE message about the project/community.

RECENT CHAT CONTEXT:
{chat_context}

CRITICAL RULES:
1. Keep it 5-20 words.
2. Sound like a REAL person.
3. Use casual language, lowercase okay.
4. NEVER repeat phrases from the avoid list.
{avoid_hint}

Your message (5-20 words, natural, human):"""

        response = client.messages.create(
            model=model,
            max_tokens=60,
            temperature=1.0,
            messages=[
                {"role": "user", "content": prompt}
            ],
            system="You are a casual Discord user from INDIA. Give ONLY the message, nothing else."
        )
        
        result = extract_text_from_response(response)
        result = result.strip('"').strip("'").strip()
        if len(result) > 100:
            result = result[:100]
        if session:
            session.reset_api_failures()
        return result if result else random.choice(["this project is looking solid", "bullish on this one", "community is fire here"])
        
    except Exception as e:
        error_str = str(e)
        logger.error(f"AI Error: {e}")
        
        if "401" in error_str or "invalid_api_key" in error_str or "403" in error_str:
            if session and api_key:
                session.mark_key_failed(api_key)
                session.rotate_api_key()
                return await generate_smart_message(context, custom_words, session, recent_sent, personality_style)
        
        if "503" in error_str or "Service Unavailable" in error_str:
            if session:
                session.rotate_api_key()
                await asyncio.sleep(3)
                try:
                    return await generate_smart_message(context, custom_words, session, recent_sent, personality_style)
                except:
                    pass
            return random.choice(["this project is looking solid", "bullish on this one", "community is fire here"])
        
        project_fallbacks = [
            "this project is looking solid", "love this community",
            "been following this for a while now", "bullish on this one",
            "team has been delivering", "the roadmap looking good",
            "excited for whats coming next", "community is fire here",
        ]
        if recent_sent:
            available = [f for f in project_fallbacks if f.lower() not in [r.lower() for r in recent_sent[-20:]]]
            if available:
                return random.choice(available)
        return random.choice(project_fallbacks)

async def generate_reply(original_msg: str, context: List[str], custom_words: List[str] = None, session: 'UserSession' = None, recent_sent: List[str] = None, personality_style: str = None) -> str:
    try:
        api_key = session.get_next_api_key() if session else DEFAULT_API_KEYS[0] if DEFAULT_API_KEYS else None
        if not api_key:
            return "i agree with what you're saying honestly"
        
        model = session.get_next_model() if session else AGENTROUTER_MODELS[0]
        client = get_agentrouter_client(api_key)
        
        chat_context = "\n".join(context[-15:])
        
        avoid_hint = ""
        if recent_sent:
            avoid_list = list(set(recent_sent[-15:]))
            avoid_hint = f"\n\nBANNED PHRASES (NEVER use these): {', '.join(avoid_list)}"
        
        is_hinglish = _detect_hinglish(context)
        
        if is_hinglish:
            lang_instruction = "Reply in HINGLISH (mix of English and Hindi in Roman script)."
            response_examples = [
                "haan bhai, mai bhi yahi soch raha tha actually",
                "sahi kaha tumne, yeh project kaafi aacha lag raha hai",
                "bhai main bhi busy tha, abhi free hua hu",
                "haan yaar, kal kaafi mast tha, tum batao kya chal raha",
                "theek hai bhai, main bhi soch raha hu iske baare mein"
            ]
        else:
            lang_instruction = "Reply in ENGLISH."
            response_examples = [
                "yeah i feel you, it's been a long week honestly",
                "i was just thinking the same thing, this is really good",
                "not sure about that but i'd love to hear more",
                "same here bro, i've been grinding all day too",
                "honestly i agree with what you're saying about that",
                "that's a really good point, i hadn't thought of it that way",
                "i've been following this project and it's pretty interesting",
                "yeah man, the devs have been putting in some serious work"
            ]
        
        prompt = f"""You are a REAL person chatting on Discord. {lang_instruction}

Message you're replying to: "{original_msg}"

Recent conversation context:
{chat_context}

{avoid_hint}

CRITICAL RULES:
1. Your reply MUST be at least 5 words.
2. Reply MEANINGFULLY to what they said.
3. Sound NATURAL and HUMAN, like a real person.
4. Use casual language.
5. If someone asked a question, ANSWER it.
6. NEVER reply with just 1-2 words like "fr", "lol", "nice", "cool", "tbh", "ngl", "bro", "mood", "alright".

EXAMPLES of good replies:
{chr(10).join(f'- "{ex}"' for ex in random.sample(response_examples, min(3, len(response_examples))))}

Your reply (MUST be a complete sentence, 5+ words):"""

        response = client.messages.create(
            model=model,
            max_tokens=80,
            temperature=1.2,
            messages=[
                {"role": "user", "content": prompt}
            ],
            system="You are a real person on Discord. ALWAYS reply with complete sentences of at least 5 words. Never reply with just 1-2 words. Sound natural and human."
        )
        
        result = extract_text_from_response(response)
        result = result.strip('"').strip("'").strip()
        
        word_count = len(result.split())
        if word_count < 5:
            extensions = [
                f" {result} honestly, that's how i feel about it",
                f" yeah {result}, i completely agree with you",
                f" {result} for real, that's exactly what i was thinking",
                f" i mean {result}, it makes a lot of sense actually",
                f" honestly {result}, i've been saying the same thing",
                f" {result} ngl, that's a really good point",
                f" i think {result}, that's a fair take honestly",
            ]
            result = random.choice(extensions)
        
        if len(result.split()) < 5:
            result = result + " that's what i was thinking too"
        
        if session:
            session.reset_api_failures()
        logger.info(f"Used model: {model} for reply")
        return result
        
    except Exception as e:
        error_str = str(e)
        logger.error(f"Reply AI Error: {e}")
        
        if "401" in error_str or "invalid_api_key" in error_str or "403" in error_str:
            if session and api_key:
                session.mark_key_failed(api_key)
                session.rotate_api_key()
                await asyncio.sleep(1)
                try:
                    return await generate_reply(original_msg, context, custom_words, session, recent_sent, personality_style)
                except:
                    pass
        
        if "503" in error_str or "Service Unavailable" in error_str:
            if session:
                session.rotate_api_key()
                await asyncio.sleep(3)
                try:
                    return await generate_reply(original_msg, context, custom_words, session, recent_sent, personality_style)
                except:
                    pass
        
        fallbacks = [
            "that's actually a really good point, i agree with you",
            "i think you're right about that, it makes a lot of sense",
            "yeah i feel the same way about it honestly",
            "that's interesting, i hadn't thought about it like that",
            "i've been thinking the same thing lately, it's pretty cool",
            "honestly i agree with what you're saying about that",
            "fair enough, i can see where you're coming from on this",
            "yeah that's a solid take, i'm with you on that one",
        ]
        return random.choice(fallbacks)

# ========================== DISCORD GRINDER ==========================

MAX_RECENT_MESSAGES = 30
tg_bot = None

def is_too_similar(msg1: str, msg2: str) -> bool:
    m1 = msg1.lower().strip()
    m2 = msg2.lower().strip()
    
    if m1 == m2:
        return True
    
    if len(m1) > 3 and len(m2) > 3:
        if m1 in m2 or m2 in m1:
            return True
    
    words1 = set(m1.split())
    words2 = set(m2.split())
    if words1 and words2:
        overlap = len(words1 & words2) / max(len(words1), len(words2))
        if overlap >= 0.8:
            return True
    
    return False

def get_non_repeating_response(response: str, recent_sent: List[str]) -> str:
    response_clean = response.strip()
    
    word_count = len(response_clean.split())
    if word_count < 5:
        extensions = [
            " honestly that's how i see it",
            " for real that's what i think",
            " i mean that's a fair point",
            " honestly i agree with that",
            " that's exactly what i was thinking"
        ]
        response_clean = response_clean + random.choice(extensions)
    
    for recent in recent_sent[-30:]:
        if is_too_similar(response_clean, recent):
            modified = response_clean + " tbh"
            
            still_similar = False
            for r2 in recent_sent[-30:]:
                if is_too_similar(modified, r2):
                    still_similar = True
                    break
            
            if not still_similar:
                response_clean = modified
            else:
                unique_fallbacks = [
                    "that's actually a really good point honestly",
                    "i think you're right about that for sure",
                    "yeah i feel the same way about it",
                    "that's interesting, i hadn't thought of that",
                    "i've been thinking the same thing lately",
                    "honestly i agree with what you're saying",
                    "fair enough, i can see your point on that",
                    "yeah that's a solid take honestly",
                ]
                available = [f for f in unique_fallbacks if not any(is_too_similar(f, r) for r in recent_sent[-30:])]
                if available:
                    response_clean = random.choice(available)
            break
    
    recent_sent.append(response_clean)
    if len(recent_sent) > MAX_RECENT_MESSAGES:
        del recent_sent[:len(recent_sent) - MAX_RECENT_MESSAGES]
    
    return response_clean

async def solve_math_question(message: str) -> Optional[str]:
    msg_lower = message.lower().strip()
    
    has_math_expr = bool(re.search(r'\d+\s*[\+\-\*\/x×÷\^\%]\s*\d+', message))
    has_percentage = bool(re.search(r'\d+\s*%\s*(of|ka)\s*\d+', msg_lower))
    
    if not has_math_expr and not has_percentage:
        return None
    
    try:
        pct_match = re.search(r'(\d+(?:\.\d+)?)\s*%\s*(?:of|ka|x)?\s*(\d+(?:\.\d+)?)', msg_lower)
        if pct_match:
            pct = float(pct_match.group(1))
            num = float(pct_match.group(2))
            result = (pct / 100) * num
            result = int(result) if result == int(result) else round(result, 2)
            answers = [f"its {result}", f"{result} bro", f"the answer is {result}", f"{result}"]
            return random.choice(answers)
        
        expr = message
        for remove_word in ['what is', 'whats', 'solve', 'calculate', 'answer', 'kitna hai', '=?', '= ?', '?']:
            expr = re.sub(re.escape(remove_word), '', expr, flags=re.IGNORECASE)
        
        expr = expr.replace('×', '*').replace('÷', '/').replace('x', '*').replace('^', '**')
        sanitized = re.sub(r'[^0-9+\-*/().\s\*]', '', expr).strip()
        
        if not sanitized or not re.search(r'\d', sanitized):
            return None
        
        if re.search(r'[a-zA-Z_]', sanitized):
            return None
        
        result = eval(sanitized, {"__builtins__": {}}, {})
        
        if isinstance(result, float):
            result = int(result) if result == int(result) else round(result, 2)
        
        answers = [
            f"its {result}",
            f"{result} bro",
            f"the answer is {result}",
            f"{result}",
            f"ez, {result}",
            f"{result} 🧠",
        ]
        return random.choice(answers)
        
    except Exception:
        return None

class DiscordGrinder:
    def __init__(self, token: str, session: UserSession):
        self.token = token
        self.session = session
        self.client = discord.Client()
        self.account_name = "Unknown"
        self.message_cache: Dict[int, List[str]] = {}
        self.slowmode_cache: Dict[int, int] = {}
        self.last_message_time: Dict[int, float] = {}
        self.last_activity_time: Dict[int, float] = {}
        self.running = False
        self.is_timed_out = False
        self._stop_requested = False
        
        self.daily_msg_limit = random.randint(600, 650)
        self.daily_msg_count_total = 0
        self.daily_reset_time = time.time()
        
        self.standalone_sent_today = 0
        self.daily_standalone_limit = random.randint(10, 20)
        
        self.reply_tracker: Dict[str, int] = {}
        self.reply_tracker_reset = time.time()
        
        self.paused_channels: Dict[int, float] = {}
        
        if not hasattr(session, 'shared_sent_history'):
            session.shared_sent_history = []
        self.sent_history = session.shared_sent_history
        
        grinder_count = len(session.grinders) if hasattr(session, 'grinders') else 0
        self.personality_index = grinder_count
        
        self.setup_events()
    
    def check_daily_limit(self) -> bool:
        if time.time() - self.daily_reset_time > 86400:
            self.daily_msg_count_total = 0
            self.standalone_sent_today = 0
            self.daily_reset_time = time.time()
            self.daily_msg_limit = random.randint(600, 650)
            self.daily_standalone_limit = random.randint(10, 20)
            self.reply_tracker = {}
            self.reply_tracker_reset = time.time()
        return self.daily_msg_count_total < self.daily_msg_limit
    
    async def _on_daily_limit_reached(self):
        await self.send_tg_alert(
            f"📊 **DAILY LIMIT REACHED!**\n\n"
            f"Account: {self.account_name}\n"
            f"Messages sent: {self.daily_msg_count_total}/{self.daily_msg_limit}\n"
            f"Standalone: {self.standalone_sent_today}/{self.daily_standalone_limit}\n\n"
            f"⏸️ This account will resume after daily reset."
        )
        logger.info(f"[{self.account_name}] 📊 Daily limit reached: {self.daily_msg_count_total}/{self.daily_msg_limit}")

    def can_reply_to_user(self, channel_id: int, user_id: int) -> bool:
        if time.time() - getattr(self, 'reply_tracker_reset', 0) > 86400:
            self.reply_tracker = {}
            self.reply_tracker_reset = time.time()
            
        key = f"{channel_id}_{user_id}"
        count = self.reply_tracker.get(key, 0)
        return count < random.randint(2, 3)

    def track_reply(self, channel_id: int, user_id: int):
        key = f"{channel_id}_{user_id}"
        self.reply_tracker[key] = self.reply_tracker.get(key, 0) + 1

    def is_channel_paused(self, channel_id: int) -> bool:
        if channel_id in self.paused_channels:
            if self.paused_channels[channel_id] == -1:
                return True
            if time.time() < self.paused_channels[channel_id]:
                return True
            else:
                del self.paused_channels[channel_id]
        return False

    def pause_channel(self, channel_id: int, hours: float):
        if hours == -1:
            self.paused_channels[channel_id] = -1
        else:
            self.paused_channels[channel_id] = time.time() + (hours * 3600)
    
    def can_reply_in_channel(self, channel_id: int) -> bool:
        last = self.last_message_time.get(channel_id, 0)
        cooldown = random.uniform(8, 20)
        return time.time() - last > cooldown
        
    async def random_react(self, message):
        emojis = ['👍', '🔥', '❤️', '😂', '💯', '👀', '🙌', '✅', '⚡', '🎯', '💪', '🤝', '😎', '🚀', '💎']
        try:
            await message.add_reaction(random.choice(emojis))
        except:
            pass

    async def send_tg_alert(self, message: str):
        global tg_bot
        if tg_bot:
            try:
                await tg_bot.send_message(
                    chat_id=self.session.user_id,
                    text=f"🚨 **ALERT - {self.account_name}**\n\n{message}",
                )
            except Exception as e:
                logger.error(f"TG Alert error: {e}")
    
    def setup_events(self):
        @self.client.event
        async def on_ready():
            self.account_name = f"{self.client.user.name}"
            logger.info(f"[{self.account_name}] ✅ Logged in!")
            self.running = True
            self.session.set_account_name(self.token, self.account_name)
            await self.send_tg_alert(f"✅ **{self.account_name}** connected! ({len(self.session.get_account_channels(self.token))} channels)")
            asyncio.create_task(self.grinding_loop())
            asyncio.create_task(self.activity_monitor())
        
        @self.client.event
        async def on_disconnect():
            self._disconnect_time = time.time()
            logger.warning(f"[{self.account_name}] ⚠️ Disconnected from Discord!")
        
        @self.client.event
        async def on_resumed():
            disconnect_duration = time.time() - getattr(self, '_disconnect_time', time.time())
            if disconnect_duration > 300:
                logger.info(f"[{self.account_name}] ✅ Reconnected after {disconnect_duration/60:.1f} min!")
                await self.send_tg_alert(f"✅ Reconnected to Discord! (was down {disconnect_duration/60:.1f} min)")
            else:
                logger.info(f"[{self.account_name}] ✅ Reconnected (routine)")
        
        @self.client.event
        async def on_member_update(before, after):
            if after.id != self.client.user.id:
                return
            
            if after.timed_out_until and not before.timed_out_until:
                self.is_timed_out = True
                await self.send_tg_alert(f"⏰ **TIMEOUT RECEIVED!**\nYou are timed out until: {after.timed_out_until}\n\n⚠️ Grinding paused. Try different server!")
            elif before.timed_out_until and not after.timed_out_until:
                self.is_timed_out = False
                await self.send_tg_alert("✅ Timeout ended! Grinding resumed.")
        
        @self.client.event
        async def on_message(message):
            if message.author == self.client.user:
                return
                
            if not getattr(self, 'running', False):
                return
            
            if message.author.bot:
                return
            
            channel_id = message.channel.id
            self.last_activity_time[channel_id] = time.time()
            
            content_lower = message.content.lower()
            our_name = self.client.user.name.lower() if self.client.user else ""
            our_mention = f"<@{self.client.user.id}>" if self.client.user else ""
            
            is_reply_to_us = False
            if message.reference and message.reference.message_id:
                try:
                    ref_msg = await message.channel.fetch_message(message.reference.message_id)
                    if ref_msg.author == self.client.user:
                        is_reply_to_us = True
                except:
                    pass
            if self.client.user in message.mentions:
                is_reply_to_us = True
            
            bot_phrases = [
                'bot hai', 'you are a bot', 'are you a bot', 'bot lagta',
                'definitely a bot', 'ur a bot', 'u bot', 'are u bot',
                'is this a bot', 'bot account', 'chatbot'
            ]
            if any(p in content_lower for p in bot_phrases):
                is_about_us = our_name in content_lower or our_mention in message.content or is_reply_to_us
                if is_about_us:
                    response = random.choice(BOT_DENIAL_RESPONSES)
                    await asyncio.sleep(random.uniform(10, 30))
                    await self.human_typing(message.channel, response)
                    try:
                        await message.reply(response, mention_author=False)
                        self.last_message_time[channel_id] = time.time()
                        self.daily_msg_count_total += 1
                        pause_mins = random.uniform(10, 30)
                        self.pause_channel(channel_id, pause_mins / 60.0)
                        logger.info(f"[{self.account_name}] 🤖 Denied bot accusation in #{message.channel.name}, paused {pause_mins:.0f}min")
                        await self.send_tg_alert(
                            f"🤖 **BOT ACCUSATION!**\n\n"
                            f"From: {message.author.name}\n"
                            f"Channel: #{message.channel.name}\n"
                            f"Message: {message.content[:200]}\n\n"
                            f"✅ Denied & paused channel for {pause_mins:.0f} min"
                        )
                    except Exception as e:
                        logger.error(f"Send error: {e}")
                    return

            if any(role.permissions.manage_messages for role in getattr(message.author, 'roles', [])):
                is_about_us = our_name in content_lower or our_mention in message.content
                warning_words = ["warn", "stop", "spam", "slow down", "timeout", "mute", "ban", "kick", "rule", "violation", "remove"]
                if is_about_us and any(w in content_lower for w in warning_words):
                    perm = any(w in content_lower for w in ["ban", "kick", "remove"])
                    hours = -1 if perm else random.uniform(1.0, 6.0)
                    self.pause_channel(channel_id, hours)
                    
                    pause_str = "PERMANENTLY" if perm else f"for {hours:.1f} hours"
                    await self.send_tg_alert(
                        f"⚠️ **MOD WARNING DETECTED!**\n\n"
                        f"From: {message.author.name}\n"
                        f"Channel: #{message.channel.name}\n"
                        f"Message: {message.content[:200]}\n\n"
                        f"🛑 Action: Paused {pause_str}"
                    )
                    return
            
            if not message.content or len(message.content.strip()) < 2:
                return
            
            if channel_id not in self.message_cache:
                self.message_cache[channel_id] = []
            self.message_cache[channel_id].append(f"{message.author.name}: {message.content}")
            if len(self.message_cache[channel_id]) > 25:
                self.message_cache[channel_id] = self.message_cache[channel_id][-25:]
            
            if hasattr(message.channel, 'slowmode_delay'):
                self.slowmode_cache[channel_id] = message.channel.slowmode_delay
            
            target_channels = self.session.get_account_channels(self.token)
            if target_channels and channel_id not in target_channels:
                return
            
            our_bot_ids = set()
            for grinder in self.session.grinders.values():
                if grinder.client and grinder.client.user:
                    our_bot_ids.add(grinder.client.user.id)
            is_our_account = message.author.id in our_bot_ids
            if is_our_account:
                if random.random() > 0.30:
                    return
                
            if self.is_channel_paused(channel_id):
                return
                
            if not self.can_reply_in_channel(channel_id):
                return
                
            if not self.check_daily_limit():
                return

            should_respond = False
            
            if is_reply_to_us:
                should_respond = True
            else:
                if not self.can_reply_to_user(channel_id, message.author.id):
                    return
                
                chance = 0.50 if "?" in message.content else 0.35
                
                if random.random() < chance:
                    should_respond = True
            
            if not should_respond:
                return
                
            if random.random() < 0.10:
                await self.random_react(message)
                return
                
            self.track_reply(channel_id, message.author.id)
            
            wait_time = random.uniform(3.0, 6.0)
            await asyncio.sleep(wait_time)
            
            if not self.check_daily_limit():
                return
            
            math_answer = await solve_math_question(message.content)
            if math_answer:
                await asyncio.sleep(random.uniform(1.0, 3.0))
                response = math_answer
            else:
                context = self.message_cache.get(channel_id, [])
                custom_words = self.session.data.get("custom_words", [])
                response = await generate_reply(message.content, context, custom_words, self.session, self.sent_history, None)
                
                if not response or response.strip().upper() in ['SKIP', 'IDK', 'IGNORE', 'PASS']:
                    logger.info(f"[{self.account_name}] ⏭️ Skipped unknown question: {message.content[:50]}")
                    return
            
            response = get_non_repeating_response(response, self.sent_history)
            
            await self.human_typing(message.channel, response)
            
            try:
                await message.reply(response, mention_author=False)
                self.last_message_time[channel_id] = time.time()
                self.daily_msg_count_total += 1
                logger.info(f"[{self.account_name}] 💬 Replied ({self.daily_msg_count_total}/{self.daily_msg_limit}): {response[:50]}")
                
                if not self.check_daily_limit():
                    await self._on_daily_limit_reached()
            except Exception as e:
                logger.error(f"Send error: {e}")
    
    async def human_typing(self, channel, text: str):
        typing_time = min(len(text) / 8.0, 3.0)
        
        async with channel.typing():
            await asyncio.sleep(typing_time)
    
    async def grinding_loop(self):
        logger.info(f"[{self.account_name}] 🎮 Started grinding loop")
        
        while self.running and not self._stop_requested:
            try:
                channels = self.session.get_account_channels(self.token)
                if not channels:
                    await asyncio.sleep(30)
                    continue
                
                for channel_id in channels:
                    if not self.running or self._stop_requested:
                        break
                    
                    channel = self.client.get_channel(channel_id)
                    if not channel:
                        continue
                        
                    try:
                        async for msg in channel.history(limit=20):
                            if msg.content and len(msg.content.strip()) > 1:
                                if channel_id not in self.message_cache:
                                    self.message_cache[channel_id] = []
                                self.message_cache[channel_id].append(f"{msg.author.name}: {msg.content}")
                        if channel_id in self.message_cache:
                            self.message_cache[channel_id] = self.message_cache[channel_id][-25:]
                    except Exception:
                        pass
                    
                    if not self.check_daily_limit():
                        continue
                        
                    if self.is_channel_paused(channel_id):
                        continue
                        
                    if not self.can_reply_in_channel(channel_id):
                        continue
                    
                    if self.standalone_sent_today < self.daily_standalone_limit:
                        if random.random() < 0.04:
                            context = self.message_cache.get(channel_id, [])
                            if len(context) >= 3:
                                response = await generate_smart_message(context, [], self.session, self.sent_history, None)
                                response = get_non_repeating_response(response, self.sent_history)
                                
                                await self.human_typing(channel, response)
                                try:
                                    await channel.send(response)
                                    self.last_message_time[channel_id] = time.time()
                                    self.daily_msg_count_total += 1
                                    self.standalone_sent_today += 1
                                    logger.info(f"[{self.account_name}] ✅ Standalone ({self.standalone_sent_today}/{self.daily_standalone_limit}) to {channel.name}: {response[:40]}")
                                    
                                    if not self.check_daily_limit():
                                        await self._on_daily_limit_reached()
                                except Exception as e:
                                    logger.error(f"[{self.account_name}] Send error: {e}")
                    
                    await asyncio.sleep(random.uniform(8, 20))
                
                delay = human_delay(90, 240)
                await asyncio.sleep(delay)
                
            except Exception as e:
                error_msg = str(e).lower()
                if "timed out" in error_msg or "timeout" in error_msg:
                    await self.send_tg_alert("⏰ **TIMEOUT DETECTED!**\nYou got timed out. Try different server!")
                    self.is_timed_out = True
                elif "banned" in error_msg or "403" in error_msg:
                    await self.send_tg_alert("🚫 **BANNED/BLOCKED!**\nThis account may be banned from the server.")
                elif "rate limit" in error_msg or "429" in error_msg:
                    await self.send_tg_alert("⚠️ **RATE LIMITED!**\nSending too fast. Slowing down...")
                else:
                    logger.error(f"Loop error: {e}")
                await asyncio.sleep(60)
        
        logger.info(f"[{self.account_name}] 🛑 Grinding loop stopped")
    
    async def activity_monitor(self):
        no_activity_count = {}
        
        while self.running and not self._stop_requested:
            try:
                await asyncio.sleep(300)
                
                channels = self.session.get_account_channels(self.token)
                for channel_id in channels:
                    last_activity = self.last_activity_time.get(channel_id, 0)
                    
                    if time.time() - last_activity > 600 and last_activity > 0:
                        no_activity_count[channel_id] = no_activity_count.get(channel_id, 0) + 1
                        
                        if no_activity_count[channel_id] >= 2:
                            channel = self.client.get_channel(channel_id)
                            channel_name = channel.name if channel else str(channel_id)
                            await self.send_tg_alert(f"😴 **LOW ACTIVITY**\nChannel: #{channel_name}\n\nNo one chatting for 10+ mins. Consider switching to active server!")
                            no_activity_count[channel_id] = 0
                    else:
                        no_activity_count[channel_id] = 0
                        
            except Exception as e:
                logger.error(f"Activity monitor error: {e}")
                await asyncio.sleep(60)
        
        logger.info(f"[{self.account_name}] 🛑 Activity monitor stopped")
    
    async def start(self):
        max_retries = 50
        backoff = 5
        attempt = 0
        
        while (attempt < max_retries and self.running) or attempt == 0:
            attempt += 1
            try:
                logger.info(f"[{self.account_name}] 🔌 Connecting... (attempt {attempt})")
                await self.client.start(self.token)
                break
            except discord.errors.LoginFailure as e:
                await self.send_tg_alert(f"❌ **LOGIN FAILED!**\nToken invalid or expired.\nError: {e}")
                logger.error(f"Login error (won't retry): {e}")
                return
            except Exception as e:
                logger.error(f"[{self.account_name}] Connection error (attempt {attempt}): {e}")
                
                if not self.running or self._stop_requested:
                    break
                
                await self.send_tg_alert(
                    f"⚠️ **DISCONNECTED!**\n"
                    f"Error: {str(e)[:100]}\n"
                    f"🔄 Retrying in {backoff}s... (attempt {attempt}/{max_retries})"
                )
                
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
        
        if attempt >= max_retries:
            await self.send_tg_alert("❌ **GAVE UP RECONNECTING** after 50 attempts. Use /startgrind to try again.")
            logger.error(f"[{self.account_name}] Max retries reached, giving up.")
    
    def stop(self):
        self.running = False
        self._stop_requested = True
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.client.close())
        except RuntimeError:
            pass

# ========================== TELEGRAM HANDLERS ==========================

async def check_subscription(bot, user_id: int) -> tuple:
    return True, []

def require_subscription(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        return await func(update, context)
    return wrapper

# ----- START COMMAND -----
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name
    
    text = f"""⚡ **{BOT_NAME}** ⚡
_{BOT_TAGLINE}_

━━━━━━━━━━━━━━━━━━━

🙏 Welcome, **{user_name}**!

 **TOKEN MANAGEMENT**
  • /addtoken — Add Discord token
  • /viewtokens — View your tokens
  • /removetoken — Remove a token

📡 **CHANNEL SETUP**
  • /addchannel — Add channel ID
  • /viewchannels — View channels
  • /removechannel — Remove channel

💬 **CUSTOM MESSAGES**
  • /addwords — Add custom msgs
  • /viewwords — View your msgs
  • /clearwords — Clear all msgs

🎮 **GRINDING**
  • /startgrind — Start grinding 🚀
  • /stopgrind — Stop all bots 🛑
  • /status — Live status 📊
  • /stopone — Stop specific account
  • /startone — Start specific account
  • /listacc — List all accounts

⚙️ **SETTINGS**
  • /setdelay `min max` — Message delay
  • /setchance `0.1-1.0` — Reply chance

🔑 **API KEYS (AgentRouter)**
  • /addapi — Add API key
  • /myapi — View your keys
  • /removeapi — Remove key

━━━━━━━━━━━━━━━━━━━

💡 **Quick Start:**
1️⃣ /addtoken → Add Discord token
2️⃣ /addchannel → Add channel ID
3️⃣ /addapi → Add AgentRouter API key
4️⃣ /startgrind → Start grinding! 🔥

━━━━━━━━━━━━━━━━━━━
📞 **Support:** {ADMIN_USERNAME}
"""
    
    if user_id in ADMIN_IDS:
        buttons = [[InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel")]]
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.message.reply_text(text)

# ----- CALLBACK HANDLERS -----
async def callback_verify_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

async def callback_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    if user_id not in ADMIN_IDS:
        await query.answer("❌ Admin only!", show_alert=True)
        return
    
    await query.answer()
    
    session_files = list(SESSIONS_DIR.glob("*.json"))
    total_users = len(session_files)
    
    active_grinders = 0
    total_tokens = 0
    total_channels = 0
    
    for sf in session_files:
        try:
            with open(sf, 'r') as f:
                data = json.load(f)
                if 'dc_accounts' in data:
                    total_tokens += len(data['dc_accounts'])
                    total_channels += sum(len(acc.get('channels', [])) for acc in data['dc_accounts'])
        except:
            pass
    
    for session in user_sessions.values():
        if session.running:
            active_grinders += 1
    
    text = f"""
╔══════════════════════════════╗
║      👑 𝐀𝐃𝐌𝐈𝐍 𝐏𝐀𝐍𝐄𝐋 👑      ║
╚══════════════════════════════╝

📊 **BOT STATISTICS**

▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬

👥 **Total Users:** `{total_users}`
🟢 **Active Grinders:** `{active_grinders}`
🔑 **Total Tokens:** `{total_tokens}`
📡 **Total Channels:** `{total_channels}`

▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬

🛠️ **ADMIN COMMANDS:**
┣━ /broadcast `<msg>` - Sabko msg
┣━ /users - User list
┗━ /stats - Detailed stats

▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬
"""
    
    buttons = [
        [InlineKeyboardButton("🔄 Refresh Stats", callback_data="admin_panel")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_back")]
    ]
    
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))

async def callback_admin_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.delete()

# ----- TOKEN COMMANDS -----
@require_subscription
async def cmd_addtoken(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    
    if not context.args:
        await update.message.reply_text("❌ Usage: /addtoken <discord_token>")
        return
    
    token = context.args[0]
    
    parts = token.split('.')
    if len(parts) != 3:
        await update.message.reply_text("❌ Invalid token format! Discord token must have 3 parts (xxx.xxx.xxx)")
        return
    
    if not all(parts):
        await update.message.reply_text("❌ Invalid token format! Token has empty parts")
        return
    
    account_id = parts[0]
    for acc in session.get_accounts():
        existing_parts = acc["token"].split('.')
        if len(existing_parts) >= 1 and existing_parts[0] == account_id:
            await update.message.reply_text("⚠️ This Discord account already has a token added! Remove old one first with /removetoken")
            return
    
    if session.add_token(token):
        dc_name = "Unknown"
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://discord.com/api/v9/users/@me",
                    headers={"Authorization": token}
                )
                if resp.status_code == 200:
                    user_data = resp.json()
                    dc_name = user_data.get("username", "Unknown")
                    session.set_account_name(token, dc_name)
        except:
            pass
        
        masked = token[:15] + "..." + token[-8:]
        await update.message.reply_text(
            f"✅ **Account Added!**\n\n"
            f" **Name:** {dc_name}\n"
            f"🔑 **Token:** `{masked}`\n"
            f"📡 **Channels:** 0\n\n"
            f"Ab channel add karo:\n"
            f"  • /addchannel `<channel_id>`",
        )
    else:
        await update.message.reply_text("⚠️ Token already exists")

@require_subscription
async def cmd_viewtokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    
    accounts = session.get_accounts()
    if not accounts:
        await update.message.reply_text("📋 No accounts added yet. Use /addtoken")
        return
    
    text = "📋 **Your DC Accounts:**\n\n"
    for i, acc in enumerate(accounts):
        name = acc.get("name", "Unknown")
        ch_count = len(acc.get("channels", []))
        masked = acc["token"][:15] + "..." + acc["token"][-8:]
        status = "🟢" if acc["token"] in session.grinders and session.grinders[acc["token"]].running else "🔴"
        text += f"{status} **{i+1}. {name}**\n"
        text += f"   🔑 `{masked}`\n"
        text += f"   📡 {ch_count} channels\n\n"
    
    await update.message.reply_text(text)

@require_subscription
async def cmd_removetoken(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    
    if not context.args:
        accounts = session.get_accounts()
        if not accounts:
            await update.message.reply_text("📋 No accounts to remove")
            return
        text = "❌ Usage: /removetoken <username>\n\n**Your accounts:**\n"
        for i, acc in enumerate(accounts):
            text += f"  {i+1}. {acc.get('name', 'Unknown')}\n"
        await update.message.reply_text(text)
        return
    
    target = context.args[0].lower().strip()
    accounts = session.get_accounts()
    
    found_index = -1
    found_name = ""
    for i, acc in enumerate(accounts):
        if acc.get("name", "").lower() == target:
            found_index = i
            found_name = acc.get("name", "Unknown")
            break
    
    if found_index == -1:
        await update.message.reply_text(f"❌ Account '{target}' not found! Use /viewtokens to see your accounts")
        return
    
    token = accounts[found_index].get("token", "")
    if token in session.grinders:
        session.grinders[token].stop()
        del session.grinders[token]
    
    if session.remove_token(found_index):
        await update.message.reply_text(f"✅ Account **{found_name}** removed!")
    else:
        await update.message.reply_text("❌ Failed to remove")

# ----- CHANNEL COMMANDS -----
@require_subscription
async def cmd_addchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    
    accounts = session.get_accounts()
    if not accounts:
        await update.message.reply_text("❌ No accounts added! Use /addtoken first")
        return
    
    if not context.args:
        await update.message.reply_text("❌ Usage: /addchannel `<channel_id>`")
        return
    
    try:
        channel_id = int(context.args[0])
    except:
        await update.message.reply_text("❌ Invalid channel ID! Must be a number")
        return
    
    if len(accounts) == 1:
        if session.add_channel_to_account(0, channel_id):
            await update.message.reply_text(
                f"✅ Channel added to **{accounts[0].get('name', 'Account 1')}**!\n"
                f"📡 Total channels: {len(accounts[0]['channels'])}",
            )
        else:
            await update.message.reply_text("⚠️ Channel already exists on this account")
        return
    
    buttons = []
    for i, acc in enumerate(accounts):
        name = acc.get("name", f"Account {i+1}")
        ch_count = len(acc.get("channels", []))
        buttons.append([InlineKeyboardButton(
            f"👤 {name} • {ch_count} ch", 
            callback_data=f"addch_{i}_{channel_id}"
        )])
    buttons.append([InlineKeyboardButton("📡 All Accounts", callback_data=f"addch_all_{channel_id}")])
    
    await update.message.reply_text(
        f"📡 **Add channel** `{channel_id}` **to which account?**\n\nSelect below:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def callback_addchannel_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    session = get_session(user_id)
    
    data = query.data
    parts = data.split("_")
    
    if len(parts) >= 3:
        account_part = parts[1]
        channel_id = int(parts[2])
        
        if account_part == "all":
            added = 0
            for i in range(len(session.get_accounts())):
                if session.add_channel_to_account(i, channel_id):
                    added += 1
            await query.answer(f"✅ Added to {added} accounts!")
            await query.message.edit_text(
                f"✅ Channel `{channel_id}` added to **all {added} accounts!**",
            )
        else:
            account_index = int(account_part)
            accounts = session.get_accounts()
            if 0 <= account_index < len(accounts):
                name = accounts[account_index].get("name", f"Account {account_index+1}")
                if session.add_channel_to_account(account_index, channel_id):
                    ch_count = len(accounts[account_index]["channels"])
                    await query.answer(f"✅ Added to {name}!")
                    await query.message.edit_text(
                        f"✅ Channel added to **{name}**!\n📡 Total channels: {ch_count}",
                    )
                else:
                    await query.answer("⚠️ Channel already exists!")
                    await query.message.edit_text("⚠️ Channel already exists on this account")

@require_subscription
async def cmd_viewchannels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    
    accounts = session.get_accounts()
    if not accounts:
        await update.message.reply_text("📋 No accounts added yet")
        return
    
    text = " **Channels by Account:**\n\n"
    has_channels = False
    
    for i, acc in enumerate(accounts):
        name = acc.get("name", f"Account {i+1}")
        channels = acc.get("channels", [])
        text += f"👤 **{name}**\n"
        if channels:
            has_channels = True
            for j, ch in enumerate(channels):
                text += f"  {j+1}. `{ch}`\n"
        else:
            text += "  _No channels yet_\n"
        text += "\n"
    
    if not has_channels:
        text += "💡 Use /addchannel `<id>` to add channels"
    
    await update.message.reply_text(text)

@require_subscription
async def cmd_removechannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    
    accounts = session.get_accounts()
    
    if not context.args or len(context.args) < 2:
        text = "❌ Usage: /removechannel `<account_num>` `<channel_num>`\n\n"
        for i, acc in enumerate(accounts):
            name = acc.get("name", f"Account {i+1}")
            channels = acc.get("channels", [])
            text += f"👤 **{i+1}. {name}**\n"
            for j, ch in enumerate(channels):
                text += f"    {j+1}. `{ch}`\n"
            if not channels:
                text += "    _No channels_\n"
            text += "\n"
        text += "💡 Example: `/removechannel 1 2` removes channel #2 from account #1"
        await update.message.reply_text(text)
        return
    
    try:
        acc_idx = int(context.args[0]) - 1
        ch_idx = int(context.args[1]) - 1
        
        if 0 <= acc_idx < len(accounts):
            name = accounts[acc_idx].get("name", f"Account {acc_idx+1}")
            if session.remove_channel_from_account(acc_idx, ch_idx):
                await update.message.reply_text(f"✅ Channel removed from **{name}**!")
            else:
                await update.message.reply_text("❌ Invalid channel number! Use /viewchannels")
        else:
            await update.message.reply_text("❌ Invalid account number! Use /viewchannels")
    except:
        await update.message.reply_text("❌ Invalid input! Use: /removechannel `<account_num>` `<channel_num>`")

# ----- WORDS COMMANDS -----
@require_subscription
async def cmd_addwords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    
    if update.message.text:
        full_text = update.message.text
        if full_text.startswith('/addwords'):
            full_text = full_text[9:].strip()
        
        if not full_text:
            await update.message.reply_text(
                "❌ Usage: /addwords followed by your messages\n\n"
                "Example:\n"
                "/addwords good morning everyone\n"
                "hope sab badhiya honge\n"
                "daily grind pe focus\n\n"
                "Each line = 1 separate message that will be sent randomly"
            )
            return
        
        lines = [line.strip() for line in full_text.split('\n') if line.strip()]
        
        for line in lines:
            if line and line not in session.data['custom_words']:
                session.data['custom_words'].append(line)
        
        session.save()
        await update.message.reply_text(
            f"✅ Added {len(lines)} message(s)!\n"
            f"Total messages: {len(session.data['custom_words'])}\n\n"
            "Bot will randomly send ONE of these messages each time 🎲"
        )
    else:
        await update.message.reply_text("❌ Please send text messages")

@require_subscription
async def cmd_viewwords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    
    words = session.data.get("custom_words", [])
    if not words:
        await update.message.reply_text("📋 No custom words added yet")
        return
    
    text = "📋 **Your Custom Words:**\n\n" + ", ".join(words)
    await update.message.reply_text(text)

@require_subscription
async def cmd_clearwords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    session.clear_words()
    await update.message.reply_text("✅ All custom words cleared!")

# ----- GRINDING COMMANDS -----
@require_subscription
async def cmd_startgrind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    
    accounts = session.get_accounts()
    tokens = session.get_all_tokens()
    
    if not accounts:
        await update.message.reply_text("❌ No accounts added! Use /addtoken first")
        return
    
    accounts_with_channels = [acc for acc in accounts if acc.get("channels")]
    if not accounts_with_channels:
        await update.message.reply_text("❌ No channels added! Use /addchannel first")
        return
    
    user_api_keys = session.data.get("api_keys", [])
    if not user_api_keys:
        await update.message.reply_text(
            "🔑 **API Key Required!**\n\n"
            "Grind start karne se pehle apna **AgentRouter API key** add karo!\n\n"
            "**Steps:**\n"
            "1️⃣ Go to: https://agentrouter.org/console/token\n"
            "2️⃣ Sign up / Login\n"
            "3️⃣ Get your API key\n"
            "4️⃣ Send: `/addapi your_key_here`\n\n"
            "🆓 It's FREE! Uske baad grind start hoga ✅",
        )
        return
    
    already_running = [t for t in tokens if t in session.grinders and session.grinders[t].running]
    not_running = [t for t in tokens if t not in already_running]
    not_running_with_channels = [t for t in not_running if session.get_account_channels(t)]
    no_channels = [t for t in not_running if not session.get_account_channels(t)]
    
    if not not_running_with_channels:
        if no_channels:
            names = []
            for t in no_channels:
                for acc in accounts:
                    if acc["token"] == t:
                        names.append(acc.get("name", "Unknown"))
            await update.message.reply_text(
                f"⚠️ These accounts have **no channels** set:\n" +
                "\n".join(f"  • {n}" for n in names) +
                "\n\nUse /addchannel to add channels first!",
            )
        else:
            await update.message.reply_text(f"✅ All {len(tokens)} accounts are already grinding!")
        return
    
    count = len(not_running_with_channels)
    
    if context.args:
        arg = context.args[0].lower()
        if arg == "all":
            count = len(not_running_with_channels)
        else:
            try:
                count = int(arg)
                if count < 1:
                    count = 1
                if count > len(not_running_with_channels):
                    count = len(not_running_with_channels)
            except ValueError:
                target_token = None
                for acc in accounts:
                    if acc.get("name", "").lower() == arg:
                        target_token = acc["token"]
                        break
                
                if target_token:
                    if target_token in already_running:
                        await update.message.reply_text(f"⚠️ Account '{arg}' is already running!")
                        return
                    if target_token not in not_running_with_channels:
                        await update.message.reply_text(f"⚠️ Account '{arg}' has no channels set!")
                        return
                    
                    selected_tokens = [target_token]
                    count = 1
                else:
                    text = f"📊 **Select Accounts to Grind**\n\n"
                    for i, acc in enumerate(accounts):
                        t = acc["token"]
                        name = acc.get("name", f"Account {i+1}")
                        ch_count = len(acc.get("channels", []))
                        if t in already_running:
                            text += f"🟢 {name} — grinding ({ch_count} ch)\n"
                        elif ch_count > 0:
                            text += f"🔴 {name} — ready ({ch_count} ch)\n"
                        else:
                            text += f"⚠️ {name} — no channels!\n"
                    text += f"\n**Usage:**\n"
                    text += f"• `/startgrind 1` - Start 1 account\n"
                    text += f"• `/startgrind all` - Start all ready accounts\n"
                    text += f"• `/startgrind username` - Start a specific account (e.g., `/startgrind kittu7671`)\n"
                    await update.message.reply_text(text)
                    return
    else:
        text = f"📊 **Select Accounts to Grind**\n\n"
        for i, acc in enumerate(accounts):
            t = acc["token"]
            name = acc.get("name", f"Account {i+1}")
            ch_count = len(acc.get("channels", []))
            if t in already_running:
                text += f"🟢 {name} — grinding ({ch_count} ch)\n"
            elif ch_count > 0:
                text += f"🔴 {name} — ready ({ch_count} ch)\n"
            else:
                text += f"⚠️ {name} — no channels!\n"
        text += f"\n**Usage:**\n"
        text += f"• `/startgrind 1` - Start 1 account\n"
        text += f"• `/startgrind all` - Start all ready accounts\n"
        text += f"• `/startgrind username` - Start a specific account (e.g., `/startgrind kittu7671`)\n"
        await update.message.reply_text(text)
        return
    
    session.running = True
    
    if 'selected_tokens' not in locals():
        selected_tokens = not_running_with_channels[:count]
    await update.message.reply_text(f"🚀 Starting {len(selected_tokens)} grinder(s)... (staggered startup for safety)")
    
    for i, token in enumerate(selected_tokens):
        grinder = DiscordGrinder(token, session)
        session.grinders[token] = grinder
        asyncio.create_task(grinder.start())
        if i < len(selected_tokens) - 1:
            stagger = random.uniform(60, 120)
            if (i + 1) % 5 == 0:
                stagger = 300
            await asyncio.sleep(stagger)
    
    total_running = len(already_running) + len(selected_tokens)
    summary = f"✅ **{total_running}/{len(tokens)} accounts now grinding!**\n\n"
    for acc in accounts:
        t = acc["token"]
        name = acc.get("name", f"Unknown")
        ch_count = len(acc.get("channels", []))
        if t in session.grinders and (session.grinders[t].running or t in selected_tokens):
            summary += f"🟢 {name} — {ch_count} channels\n"
        else:
            summary += f"🔴 {name}\n"
    summary += "\nUse /stopgrind to stop"
    await update.message.reply_text(summary)

@require_subscription
async def cmd_stopgrind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    
    if not session.running and not session.grinders:
        await update.message.reply_text("⚠️ Not grinding")
        return
    
    if context.args:
        target = context.args[0].lower().strip()
        found = False
        for token, grinder in list(session.grinders.items()):
            if grinder.account_name.lower() == target:
                grinder.stop()
                del session.grinders[token]
                found = True
                await update.message.reply_text(f"🛑 Stopped **{grinder.account_name}**")
                break
        if not found:
            await update.message.reply_text(f"❌ Account '{target}' not found or not running")
        if not session.grinders:
            session.running = False
        return
    
    session.running = False
    for grinder in session.grinders.values():
        grinder.stop()
    session.grinders.clear()
    
    await update.message.reply_text("🛑 All grinding stopped!")

@require_subscription
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    
    status = "🟢 Running" if session.running else "🔴 Stopped"
    accounts = session.get_accounts()
    total_channels = sum(len(acc.get('channels', [])) for acc in accounts)
    words = len(session.data.get("custom_words", []))
    
    text = f"""📊 **Status Dashboard**

Status: {status}
Accounts: {len(accounts)}
Total Channels: {total_channels}
Custom Words: {words}
Reply Mode: ON (90-95% replies, 5-10% standalone)
"""
    if accounts:
        text += "\n**Per-Account Status:**\n"
        for acc in accounts:
            t = acc["token"]
            name = acc.get("name", "Unknown")
            ch_count = len(acc.get("channels", []))
            
            grinder = session.grinders.get(t)
            if grinder and grinder.running:
                msgs_today = grinder.daily_msg_count_total
                msg_limit = grinder.daily_msg_limit
                standalone = grinder.standalone_sent_today
                standalone_limit = grinder.daily_standalone_limit
                
                text += f"\n🟢 **{name}**\n"
                text += f"   📨 Messages: {msgs_today}/{msg_limit}\n"
                text += f"   ✍️ Standalone: {standalone}/{standalone_limit}\n"
                text += f"   🎯 Reply chance: {grinder.base_reply_chance:.2f}\n"
                
                paused_chs = []
                for cid, p_time in getattr(grinder, 'paused_channels', {}).items():
                    ch_obj = grinder.client.get_channel(cid)
                    ch_name = ch_obj.name if ch_obj else str(cid)
                    if p_time == -1:
                        paused_chs.append(f"#{ch_name} (PERMANENT)")
                    elif p_time > time.time():
                        remaining = int((p_time - time.time()) / 60)
                        paused_chs.append(f"#{ch_name} ({remaining}min left)")
                if paused_chs:
                    text += f"   ⚠️ Paused: {', '.join(paused_chs)}\n"
            else:
                text += f"\n🔴 **{name}** — {ch_count} ch (stopped)\n"
    
    await update.message.reply_text(text)

@require_subscription
async def cmd_setdelay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    
    if len(context.args) < 2:
        await update.message.reply_text("❌ Usage: /setdelay <min> <max>")
        return
    
    try:
        min_d = int(context.args[0])
        max_d = int(context.args[1])
        session.data["min_delay"] = min_d
        session.data["max_delay"] = max_d
        session.save()
        await update.message.reply_text(f"✅ Delay set to {min_d}-{max_d} seconds")
    except:
        await update.message.reply_text("❌ Invalid values")

@require_subscription
async def cmd_setchance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    
    if not context.args:
        await update.message.reply_text("❌ Usage: /setchance <0.1-1.0>")
        return
    
    try:
        chance = float(context.args[0])
        if 0 < chance <= 1:
            session.data["reply_chance"] = chance
            session.save()
            await update.message.reply_text(f"✅ Reply chance set to {chance}")
        else:
            await update.message.reply_text("❌ Value must be between 0.1 and 1.0")
    except:
        await update.message.reply_text("❌ Invalid value")

# ----- API KEY COMMANDS -----
@require_subscription
async def cmd_addapi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    
    if not context.args:
        help_text = """🔑 **Add Your AgentRouter API Key**

**How to get FREE AgentRouter API Key:**
1️⃣ Go to: https://agentrouter.org/console/token
2️⃣ Sign up / Login
3️⃣ Copy your API key

**Usage:**
`/addapi your_api_key_here`

💡 **Benefits:**
• FREE access to Claude models
• No rate limits
• Multiple keys rotate automatically"""
        await update.message.reply_text(help_text)
        return
    
    api_key = context.args[0].strip()
    
    if len(api_key) < 10:
        await update.message.reply_text("❌ API key too short! Make sure you copied the full key.")
        return
    
    if session.add_api_key(api_key):
        total_keys = len(session.data.get("api_keys", []))
        await update.message.reply_text(f"""✅ **API Key Added!**

🔑 Your keys: {total_keys}
🔄 Keys will auto-rotate when rate limited

Use `/myapi` to view your keys""")
    else:
        await update.message.reply_text("⚠️ This key is already added!")

@require_subscription
async def cmd_myapi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    
    user_keys = session.data.get("api_keys", [])
    
    if not user_keys:
        text = """🔑 **Your API Keys**

❌ You haven't added any keys yet!

**Add a key:**
`/addapi your_api_key_here`

**How to get FREE AgentRouter API:**
1. Go to https://agentrouter.org/console/token
2. Create account & get API key
3. Copy & paste here!

🆓 Using default shared keys (may hit rate limits)"""
    else:
        keys_list = ""
        for i, key in enumerate(user_keys, 1):
            masked = key[:8] + "..." + key[-4:]
            keys_list += f"\n{i}. `{masked}`"
        
        current_idx = session.data.get("current_api_index", 0)
        total = len(user_keys) + len(DEFAULT_API_KEYS)
        
        text = f"""🔑 **Your API Keys** ({len(user_keys)})
{keys_list}

🔄 **Rotation Status:**
• Current index: {current_idx + 1}/{total}
• Your keys + defaults = {total} total

**Commands:**
• `/addapi <key>` - Add more keys
• `/removeapi <number>` - Remove a key

💡 More keys = Less rate limiting"""
    
    await update.message.reply_text(text)

@require_subscription
async def cmd_removeapi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    
    if not context.args:
        await update.message.reply_text("❌ Usage: `/removeapi <number>`\n\nUse `/myapi` to see your keys")
        return
    
    try:
        index = int(context.args[0]) - 1
        if session.remove_api_key(index):
            remaining = len(session.data.get("api_keys", []))
            await update.message.reply_text(f"✅ API key removed!\n\n🔑 Remaining keys: {remaining}")
        else:
            await update.message.reply_text("❌ Invalid key number! Use `/myapi` to see your keys")
    except:
        await update.message.reply_text("❌ Please provide a valid number")

# ----- ACCOUNT CONTROL COMMANDS -----
@require_subscription
async def cmd_stopone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    
    if not context.args:
        accounts = session.get_accounts()
        if not accounts:
            await update.message.reply_text("❌ No accounts added")
            return
        text = "🛑 **Stop Specific Account**\n\n"
        for i, acc in enumerate(accounts, 1):
            name = acc.get("name", "Unknown")
            t = acc["token"]
            grinder = session.grinders.get(t)
            status = "🟢 Running" if (grinder and grinder.running) else "🔴 Stopped"
            text += f"{i}. {status} **{name}**\n"
        text += "\n📌 Usage: `/stopone <number>`\nExample: `/stopone 3`"
        await update.message.reply_text(text)
        return
    
    try:
        idx = int(context.args[0]) - 1
        accounts = session.get_accounts()
        if idx < 0 or idx >= len(accounts):
            await update.message.reply_text(f"❌ Invalid number! Range: 1-{len(accounts)}")
            return
        
        token = accounts[idx]["token"]
        name = accounts[idx].get("name", "Unknown")
        grinder = session.grinders.get(token)
        
        if grinder and grinder.running:
            grinder.stop()
            del session.grinders[token]
            await update.message.reply_text(f"🛑 **{name}** stopped!")
        else:
            await update.message.reply_text(f"⚠️ **{name}** is not running")
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid number")

@require_subscription
async def cmd_startone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    
    if not context.args:
        accounts = session.get_accounts()
        if not accounts:
            await update.message.reply_text("❌ No accounts added")
            return
        text = "▶️ **Start Specific Account**\n\n"
        for i, acc in enumerate(accounts, 1):
            name = acc.get("name", "Unknown")
            t = acc["token"]
            grinder = session.grinders.get(t)
            status = "🟢 Running" if (grinder and grinder.running) else "🔴 Stopped"
            channels = len(acc.get("channels", []))
            text += f"{i}. {status} **{name}** ({channels} ch)\n"
        text += "\n📌 Usage: `/startone <number>`\nExample: `/startone 5`"
        await update.message.reply_text(text)
        return
    
    try:
        idx = int(context.args[0]) - 1
        accounts = session.get_accounts()
        if idx < 0 or idx >= len(accounts):
            await update.message.reply_text(f"❌ Invalid number! Range: 1-{len(accounts)}")
            return
        
        token = accounts[idx]["token"]
        name = accounts[idx].get("name", "Unknown")
        channels = accounts[idx].get("channels", [])
        
        if token in session.grinders and session.grinders[token].running:
            await update.message.reply_text(f"⚠️ **{name}** is already running!")
            return
        
        if not channels:
            await update.message.reply_text(f"❌ **{name}** has no channels! Add channels first.")
            return
        
        session.running = True
        grinder = DiscordGrinder(token, session)
        session.grinders[token] = grinder
        asyncio.create_task(grinder.start())
        await update.message.reply_text(f"▶️ **{name}** started! ({len(channels)} channels)")
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid number")

@require_subscription
async def cmd_listacc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    
    accounts = session.get_accounts()
    if not accounts:
        await update.message.reply_text("❌ No accounts added. Use /addtoken")
        return
    
    running_count = 0
    stopped_count = 0
    total_msgs = 0
    
    text = "📋 **All Accounts**\n\n"
    for i, acc in enumerate(accounts, 1):
        t = acc["token"]
        name = acc.get("name", "Unknown")
        channels = len(acc.get("channels", []))
        grinder = session.grinders.get(t)
        
        if grinder and grinder.running:
            running_count += 1
            msgs = grinder.daily_msg_count_total
            limit = grinder.daily_msg_limit
            total_msgs += msgs
            paused = len(getattr(grinder, 'paused_channels', {}))
            
            text += f"**{i}. 🟢 {name}**\n"
            text += f"   📨 {msgs}/{limit} msgs\n"
            text += f"   📺 {channels} channels"
            if paused:
                text += f" | ⚠️ {paused} paused"
            text += "\n\n"
        else:
            stopped_count += 1
            text += f"**{i}. 🔴 {name}** — {channels} ch (stopped)\n\n"
    
    text += f"━━━━━━━━━━━━━━━━\n"
    text += f"🟢 Running: {running_count} | 🔴 Stopped: {stopped_count}\n"
    text += f"📨 Total msgs today: {total_msgs}\n\n"
    text += "**Commands:**\n"
    text += "• `/startone <num>` — Start specific acc\n"
    text += "• `/stopone <num>` — Stop specific acc\n"
    text += "• `/startgrind` — Start all\n"
    text += "• `/stopgrind` — Stop all"
    
    await update.message.reply_text(text)

@require_subscription
async def cmd_pausech(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    
    if not context.args:
        await update.message.reply_text(
            "⏸️ **Pause Channel**\n\n"
            "Usage:\n"
            "• `/pausech <channel_id>` — Pause permanently\n"
            "• `/pausech <channel_id> <hours>` — Pause for X hours\n\n"
            "Example: `/pausech 1031431934699655302 2`",
        )
        return
    
    try:
        channel_id = int(context.args[0])
        hours = float(context.args[1]) if len(context.args) > 1 else -1
        
        count = 0
        for grinder in session.grinders.values():
            if grinder.running:
                grinder.pause_channel(channel_id, hours)
                count += 1
        
        if hours == -1:
            await update.message.reply_text(f"⏸️ Channel `{channel_id}` paused **permanently** on {count} accounts\n\nUse `/unpausech {channel_id}` to resume")
        else:
            await update.message.reply_text(f"⏸️ Channel `{channel_id}` paused for **{hours}h** on {count} accounts")
    except ValueError:
        await update.message.reply_text("❌ Invalid channel ID or hours")

@require_subscription
async def cmd_unpausech(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_session(user_id)
    
    if not context.args:
        paused_list = []
        for grinder in session.grinders.values():
            if grinder.running:
                for cid, p_time in getattr(grinder, 'paused_channels', {}).items():
                    ch = grinder.client.get_channel(cid)
                    ch_name = ch.name if ch else str(cid)
                    status = "PERMANENT" if p_time == -1 else f"{max(0, int((p_time - time.time()) / 60))}min left"
                    paused_list.append(f"• `{cid}` — #{ch_name} ({status})")
        
        if paused_list:
            text = "⏸️ **Paused Channels:**\n\n" + "\n".join(list(set(paused_list)))
            text += "\n\nUsage: `/unpausech <channel_id>`"
        else:
            text = "✅ No paused channels!"
        await update.message.reply_text(text)
        return
    
    try:
        channel_id = int(context.args[0])
        count = 0
        for grinder in session.grinders.values():
            if grinder.running and channel_id in grinder.paused_channels:
                del grinder.paused_channels[channel_id]
                count += 1
        
        await update.message.reply_text(f"▶️ Channel `{channel_id}` unpaused on {count} accounts!")
    except ValueError:
        await update.message.reply_text("❌ Invalid channel ID")

# ========================== CLEANUP FUNCTION ==========================

async def shutdown(sig=None, frame=None):
    """Clean shutdown function"""
    print("\n🛑 Shutting down...")
    
    # Stop all grinders
    for session in user_sessions.values():
        session.stop_all_grinders()
    
    # Wait a bit for cleanup
    await asyncio.sleep(1)
    
    print("✅ Cleanup complete")
    sys.exit(0)

# ========================== MAIN ==========================

def main():
    global tg_bot
    print("🚀 Starting Discord Grinder Telegram Bot...")
    print(f"📊 Available Models: {', '.join(AGENTROUTER_MODELS)}")
    print(f"🔑 Default API Keys: {len(DEFAULT_API_KEYS)}")
    print(f"🌐 Using AgentRouter API")
    print(f"🔗 Base URL: {AGENTROUTER_BASE_URL}")
    print("Press Ctrl+C to stop\n")
    
    # Setup signal handlers for clean shutdown
    signal.signal(signal.SIGINT, lambda s, f: asyncio.create_task(shutdown(s, f)))
    signal.signal(signal.SIGTERM, lambda s, f: asyncio.create_task(shutdown(s, f)))
    
    app = Application.builder().token(TG_BOT_TOKEN).connect_timeout(30).read_timeout(30).write_timeout(30).build()
    
    tg_bot = app.bot
    
    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("addtoken", cmd_addtoken))
    app.add_handler(CommandHandler("viewtokens", cmd_viewtokens))
    app.add_handler(CommandHandler("removetoken", cmd_removetoken))
    app.add_handler(CommandHandler("addchannel", cmd_addchannel))
    app.add_handler(CommandHandler("viewchannels", cmd_viewchannels))
    app.add_handler(CommandHandler("removechannel", cmd_removechannel))
    app.add_handler(CommandHandler("addwords", cmd_addwords))
    app.add_handler(CommandHandler("viewwords", cmd_viewwords))
    app.add_handler(CommandHandler("clearwords", cmd_clearwords))
    app.add_handler(CommandHandler("startgrind", cmd_startgrind))
    app.add_handler(CommandHandler("stopgrind", cmd_stopgrind))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("setdelay", cmd_setdelay))
    app.add_handler(CommandHandler("setchance", cmd_setchance))
    app.add_handler(CommandHandler("addapi", cmd_addapi))
    app.add_handler(CommandHandler("myapi", cmd_myapi))
    app.add_handler(CommandHandler("removeapi", cmd_removeapi))
    app.add_handler(CommandHandler("stopone", cmd_stopone))
    app.add_handler(CommandHandler("startone", cmd_startone))
    app.add_handler(CommandHandler("listacc", cmd_listacc))
    app.add_handler(CommandHandler("pausech", cmd_pausech))
    app.add_handler(CommandHandler("unpausech", cmd_unpausech))
    
    # Callback handlers
    app.add_handler(CallbackQueryHandler(callback_verify_join, pattern="verify_join"))
    app.add_handler(CallbackQueryHandler(callback_addchannel_select, pattern=r"^addch_"))
    app.add_handler(CallbackQueryHandler(callback_admin_panel, pattern="admin_panel"))
    app.add_handler(CallbackQueryHandler(callback_admin_back, pattern="admin_back"))
    
    print("✅ Bot ready! Running...")
    
    try:
        app.run_polling(drop_pending_updates=True)
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped by user")
        for session in user_sessions.values():
            session.stop_all_grinders()
        print("✅ Cleanup complete")

if __name__ == "__main__":
    main()
