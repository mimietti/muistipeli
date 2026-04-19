import gevent
import gevent.monkey
gevent.monkey.patch_all()

from flask import Flask, render_template, request, jsonify  # noqa: F401
from flask_socketio import SocketIO, emit, join_room, leave_room
import random
import os
import re
import time
import requests
import uuid
from dataclasses import dataclass, field
from dotenv import load_dotenv
from collections import defaultdict
load_dotenv()

# ---------------------------------------------------------------------------
# Database (PostgreSQL via psycopg2, optional — skipped if DATABASE_URL unset)
# ---------------------------------------------------------------------------
try:
    import psycopg2
except ImportError:
    psycopg2 = None  # type: ignore

_db_url = os.getenv("DATABASE_URL", "").strip()

def _get_db():
    """Return a new psycopg2 connection, or None if DB not configured."""
    if not _db_url or psycopg2 is None:
        return None
    try:
        return psycopg2.connect(_db_url)
    except Exception as e:
        print(f"[DB] Yhteysvirhe: {e}")
        return None

def _init_db():
    conn = _get_db()
    if not conn:
        print("[DB] DATABASE_URL ei asetettu – leaderboard ei käytössä.")
        return
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS results (
                        id          SERIAL PRIMARY KEY,
                        username    TEXT NOT NULL,
                        play_mode   TEXT NOT NULL,
                        game_mode   TEXT NOT NULL,
                        pairs_found INT NOT NULL DEFAULT 0,
                        time_secs   INT,
                        mistakes    INT,
                        total_time  INT,
                        created_at  TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                # Add columns if table existed without them
                cur.execute("""
                    ALTER TABLE results ADD COLUMN IF NOT EXISTS total_time INT
                """)
                cur.execute("""
                    ALTER TABLE results ADD COLUMN IF NOT EXISTS card_mode TEXT
                """)
                cur.execute("""
                    ALTER TABLE results ADD COLUMN IF NOT EXISTS round_won INT
                """)
                cur.execute("""
                    ALTER TABLE results ADD COLUMN IF NOT EXISTS target_language TEXT
                """)
                cur.execute("""
                    ALTER TABLE results ADD COLUMN IF NOT EXISTS bot_difficulty TEXT
                """)
                cur.execute("""
                    ALTER TABLE results ADD COLUMN IF NOT EXISTS round_result TEXT
                """)
        print("[DB] Tietokanta alustettu.")
    except Exception as e:
        print(f"[DB] Alustusvirhe: {e}")
    finally:
        conn.close()

_init_db()

SOLO_PENALTY_PER_MISTAKE = 3  # seconds

def save_result(username, play_mode, game_mode, pairs_found, time_secs=None, mistakes=None, card_mode=None, round_won=None, target_language=None, bot_difficulty=None, round_result=None):
    total_time = None
    if time_secs is not None and mistakes is not None:
        total_time = time_secs + mistakes * SOLO_PENALTY_PER_MISTAKE
    conn = _get_db()
    if not conn:
        return
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO results (username, play_mode, game_mode, pairs_found, time_secs, mistakes, total_time, card_mode, round_won, target_language, bot_difficulty, round_result)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (username, play_mode, game_mode, pairs_found, time_secs, mistakes, total_time, card_mode, round_won, target_language, bot_difficulty, round_result)
                )
    except Exception as e:
        print(f"[DB] Tallennusvirhe: {e}")
    finally:
        conn.close()

app = Flask(__name__)
socketio = SocketIO(
    app,
    async_mode='gevent',
    cors_allowed_origins="*",
    logger=False,
    engineio_logger=False,
)

VERBOSE_DEBUG = str(os.getenv("VERBOSE_DEBUG", "0")).lower() in {"1", "true", "yes"}
RECONNECT_GRACE_SECONDS = max(30, int(os.getenv("RECONNECT_GRACE_SECONDS", "300")))
PAGE_TRANSITION_GRACE_SECONDS = 5
APP_VERSION = "Beta v0.09 (2026-04-19)"
BOT_USERNAME = "Muistibotti"
BOT_FIRST_FLIP_DELAY_SECONDS = 2.5
BOT_SECOND_FLIP_DELAY_SECONDS = 1.9
FINAL_PAIR_REVEAL_SECONDS = 3.2
BOT_MEMORY_USE_PROBABILITY = {
    "easy": 0.0,
    "medium": 0.55,
    "hard": 0.9,
}
DEFAULT_ROOM_ID = "default"
MAX_PLAYERS = 2


class PixabayConfigError(RuntimeError):
    pass


def debug(message):
    if VERBOSE_DEBUG:
        print(message)


@app.context_processor
def inject_app_version():
    return {"app_version": APP_VERSION}


@dataclass
class RoomState:
    room_id: str
    status: str = "waiting"          # waiting | setup | playing | results
    game_mode: str = "manual"        # word source: manual | theme | language(legacy)
    card_mode: str = "image_word"    # card display: images | image_word | words
    play_mode: str = "local"         # local | bot | queue | solo
    ui_language: str = "en"
    native_language: str = "fi"
    target_language: str = "es"
    image_mode: str = "pixabay"
    word_filter_mode: str = "clear"
    players: dict = field(default_factory=dict)
    player_order: list = field(default_factory=list)
    grid_data: list = field(default_factory=list)
    revealed_cards: list = field(default_factory=list)
    matched_indices: set = field(default_factory=set)
    turn: int = 0
    player_points: dict = field(default_factory=dict)
    round_win: defaultdict = field(default_factory=lambda: defaultdict(int))
    pending_pair: int = 0
    pending_player: str | None = None
    pending_theme: str | None = None
    pending_search_theme: str | None = None
    theme_candidates: list = field(default_factory=list)
    theme_rejected_words: set = field(default_factory=set)
    theme_selection_state: dict = field(default_factory=dict)
    used_image_ids: set = field(default_factory=set)
    lang_generation_in_progress: bool = False
    theme_generation_in_progress: bool = False
    bot_turn_scheduled: bool = False
    bot_difficulty: str = "easy"
    bot_memory: dict = field(default_factory=dict)  # card_index -> pair_id
    current_click_sid: str | None = None
    last_tokens: set = field(default_factory=set)
    solo_start_time: float = 0.0
    solo_mistakes: int = 0
    solo_seen_cards: set = field(default_factory=set)
    rematch_votes: set = field(default_factory=set)
    summary_sids: dict = field(default_factory=dict)  # username -> sid
    queue_round_prepared: bool = False


# --- Global indexes (not game state) ---
rooms: dict = {}
player_room_index: dict = {}   # reconnect_token -> room_id
_sid_to_room_id: dict = {}     # socket sid -> room_id

# --- Matchmaking queue ---
matchmaking_queue: list = []   # [{"sid", "username", "reconnect_token"}]

# --- Caches ---
theme_words_cache: dict = {}
translation_cache: dict = {}
pixabay_cache: dict = {}
theme_translation_cache: dict = {}

TRANSLATION_API_URL = "https://api.mymemory.translated.net/get"
SUPPORTED_LANGUAGES = {
    "fi": {"fi": "Suomi",     "en": "Finnish",    "flag": "🇫🇮"},
    "en": {"fi": "Englanti",  "en": "English",    "flag": "🇬🇧"},
    "es": {"fi": "Espanja",   "en": "Spanish",    "flag": "🇪🇸"},
    "sv": {"fi": "Ruotsi",    "en": "Swedish",    "flag": "🇸🇪"},
    "de": {"fi": "Saksa",     "en": "German",     "flag": "🇩🇪"},
    "fr": {"fi": "Ranska",    "en": "French",     "flag": "🇫🇷"},
    "it": {"fi": "Italia",    "en": "Italian",    "flag": "🇮🇹"},
    "pt": {"fi": "Portugali", "en": "Portuguese", "flag": "🇵🇹"},
}
THEME_TRANSLATION_OVERRIDES = {
    "ravintola": "restaurant", "ruoka": "food", "hedelmät": "fruits",
    "hedelmat": "fruits", "eläimet": "animals", "elaimet": "animals",
    "eläin": "animal", "elain": "animal", "urheilu": "sport",
    "olut": "beer", "juoma": "drink", "juomat": "drinks",
    "keittiö": "kitchen", "keittio": "kitchen",
}
THEME_TRANSLATION_FIXES = {"resturant": "restaurant"}
ABSTRACT_THEME_WORDS = {
    "ability", "advice", "anger", "belief", "concept", "courage", "emotion", "faith",
    "freedom", "friendship", "future", "happiness", "hope", "idea", "justice",
    "knowledge", "logic", "love", "peace", "power", "quality", "spirit", "strategy",
    "strength", "success", "theory", "thought", "truth", "value", "vision", "wisdom"
}
CLEAR_PICTURE_WORD_BLACKLIST = ABSTRACT_THEME_WORDS | {
    "authority", "bodily", "carriage", "consistence", "consistency", "creation",
    "entity", "essence", "ethos", "existence", "expanse", "function",
    "horticulture", "leverage", "macrocosm", "natural", "object", "overall",
    "part", "personality", "physical", "place", "playing", "position", "power",
    "quality", "repertoire", "role", "situation", "spot", "substance", "such",
    "toolkit", "universe", "vegetation", "vehicular", "wild", "world"
}
CLEAR_PICTURE_WORD_SUFFIXES = (
    "ability", "acity", "ality", "ance", "ence", "hood", "ibility", "ion",
    "ism", "ity", "ment", "ness", "ology", "ship", "sion", "tude"
)

RANDOM_WORD_THEMES = [
    "work","restaurant","family","animal", "food", "sport", "nature", "vehicle", "furniture",
    "clothing", "tool", "fruit", "vegetable", "music", "body",
    "weather", "building", "kitchen", "garden", "office", "ocean",
]


# ---------------------------------------------------------------------------
# Room helpers
# ---------------------------------------------------------------------------

def create_room(room_id=DEFAULT_ROOM_ID):
    room = RoomState(room_id=room_id)
    rooms[room_id] = room
    return room


def get_room(room_id=DEFAULT_ROOM_ID):
    return rooms.get(room_id) or create_room(room_id)


def get_default_room():
    return get_room(DEFAULT_ROOM_ID)


def get_room_for_sid(sid):
    room_id = _sid_to_room_id.get(sid, DEFAULT_ROOM_ID)
    return get_room(room_id)


def remove_player_memberships(reconnect_token=None, sid=None, keep_room_id=None):
    for room in rooms.values():
        for existing_sid, info in list(room.players.items()):
            same_sid = sid and existing_sid == sid
            same_token = reconnect_token and info.get("reconnect_token") == reconnect_token
            if not same_sid and not same_token:
                continue
            if keep_room_id and room.room_id == keep_room_id and same_token:
                continue
            if room.current_click_sid == existing_sid:
                room.current_click_sid = None
            del room.players[existing_sid]
            _sid_to_room_id.pop(existing_sid, None)
            try:
                leave_room(room.room_id, sid=existing_sid)
            except Exception:
                pass


def move_sid_to_room(sid, room_id):
    previous_room_id = _sid_to_room_id.get(sid)
    if previous_room_id and previous_room_id != room_id:
        try:
            leave_room(previous_room_id, sid=sid)
        except Exception:
            pass
    _sid_to_room_id[sid] = room_id
    try:
        join_room(room_id, sid=sid)
    except Exception:
        pass


def emit_to_room(event_name, payload=None, room_id=DEFAULT_ROOM_ID):
    socketio.emit(event_name, payload or {}, to=room_id)


def assign_reconnect_token_to_room(reconnect_token, room_id=DEFAULT_ROOM_ID):
    if reconnect_token:
        player_room_index[reconnect_token] = room_id


def clear_reconnect_token_room(reconnect_token):
    if reconnect_token:
        player_room_index.pop(reconnect_token, None)


def get_room_id_for_reconnect_token(reconnect_token):
    return player_room_index.get(reconnect_token) or DEFAULT_ROOM_ID


# ---------------------------------------------------------------------------
# Room state helpers
# ---------------------------------------------------------------------------

def reset_pending_state(room):
    room.pending_pair = 0
    room.pending_player = None
    room.pending_theme = None
    room.pending_search_theme = None
    room.theme_candidates = []
    room.theme_rejected_words = set()
    room.theme_selection_state = {}
    room.used_image_ids = set()
    room.lang_generation_in_progress = False
    room.theme_generation_in_progress = False
    room.current_click_sid = None
    room.bot_turn_scheduled = False
    room.bot_memory = {}
    room.game_mode = "manual"
    room.ui_language = "en"
    room.queue_round_prepared = False
    if not room.grid_data:
        room.status = "waiting"


def is_bot_player(player_or_name, room=None):
    if isinstance(player_or_name, dict):
        return bool(player_or_name.get("is_bot"))
    if isinstance(player_or_name, str):
        if room is None:
            # Search all rooms
            for r in rooms.values():
                for info in r.players.values():
                    if info.get("username") == player_or_name and info.get("is_bot"):
                        return True
            return False
        return any(
            info.get("username") == player_or_name and info.get("is_bot")
            for info in room.players.values()
        )
    return False


def is_effectively_present(player_data):
    if player_data.get("connected", True):
        return True
    disconnected_at = player_data.get("disconnected_at")
    if disconnected_at is None:
        return False
    return (time.monotonic() - disconnected_at) < PAGE_TRANSITION_GRACE_SECONDS


def get_active_player_items(room):
    return [(sid, data) for sid, data in room.players.items() if data.get("connected", True)]


def get_active_players_ordered(room):
    return [data for _, data in get_active_player_items(room)]


def get_active_player_count(room):
    return len(get_active_player_items(room))


def get_effective_player_items(room):
    return [(sid, data) for sid, data in room.players.items() if is_effectively_present(data)]


def get_effective_players_ordered(room):
    return [data for _, data in get_effective_player_items(room)]


def get_effective_player_count(room):
    return len(get_effective_player_items(room))


def is_solo(room):
    return room.play_mode == "solo"


def solo_or_enough_players(room):
    """True if the game can proceed: solo mode OR 2+ players."""
    return is_solo(room) or get_effective_player_count(room) >= 2


def get_human_player_items(room):
    return [(sid, data) for sid, data in room.players.items() if not is_bot_player(data)]


def get_effective_human_player_items(room):
    return [(sid, data) for sid, data in get_effective_player_items(room) if not is_bot_player(data)]


def get_first_human_player_name(room):
    for player in get_effective_players_ordered(room):
        if not is_bot_player(player):
            return player.get("username")
    return room.player_order[0] if room.player_order else None


def queue_can_prepare_round_while_waiting(room):
    return (
        room.play_mode == "queue"
        and not any(is_bot_player(info) for info in room.players.values())
        and len(get_effective_human_player_items(room)) == 1
    )


def clear_round_runtime(room):
    room.grid_data.clear()
    room.revealed_cards.clear()
    room.matched_indices.clear()
    room.turn = 0
    room.player_points.clear()
    room.current_click_sid = None
    room.queue_round_prepared = False
    room.last_tokens = set()


def emit_queue_round_prepared(room):
    room.status = "waiting"
    room.queue_round_prepared = True
    room.pending_player = None
    room.pending_pair = 0
    room.last_tokens = set(v["reconnect_token"] for v in get_effective_players_ordered(room))
    for player in room.players.values():
        if is_bot_player(player):
            continue
        player["in_waiting"] = True
        player["pref_ready"] = True
    payload = build_lobby_payload(room)
    emit_to_room("queue_round_prepared", payload, room_id=room.room_id)
    emit_to_room("player_joined", payload, room_id=room.room_id)
    broadcast_lobby_browser()


def get_active_bot_identity(room):
    for sid, info in get_effective_player_items(room):
        if is_bot_player(info):
            return sid, info
    return None, None


def remove_bot_players(room):
    removed = False
    for sid, info in list(room.players.items()):
        if is_bot_player(info):
            clear_reconnect_token_room(info.get("reconnect_token"))
            _sid_to_room_id.pop(sid, None)
            del room.players[sid]
            removed = True
    return removed


def ensure_bot_opponent(room):
    if any(is_bot_player(info) for info in room.players.values()):
        return
    bot_sid = f"bot:{uuid.uuid4()}"
    bot_token = f"bot-{uuid.uuid4()}"
    room.players[bot_sid] = {
        "username": BOT_USERNAME,
        "reconnect_token": bot_token,
        "connected": True,
        "disconnected_at": None,
        "is_bot": True,
        "room_id": room.room_id
    }
    _sid_to_room_id[bot_sid] = room.room_id
    assign_reconnect_token_to_room(bot_token, room.room_id)
    print(f"[INFO] {BOT_USERNAME} lisättiin bot-vastustajaksi.")


# ---------------------------------------------------------------------------
# Player event resolution
# ---------------------------------------------------------------------------

def resolve_player_for_event(data=None):
    sid = request.sid
    room = get_room_for_sid(sid)
    player_info = room.players.get(sid)
    if player_info:
        player_info["connected"] = True
        player_info["disconnected_at"] = None
        return sid, player_info

    reconnect_token = ((data or {}).get("reconnect_token") or "").strip()
    username_hint = ((data or {}).get("username") or "").strip()

    # Search across all rooms
    for r in rooms.values():
        for existing_sid, info in list(r.players.items()):
            if reconnect_token and info.get("reconnect_token") == reconnect_token:
                updated = {**info, "connected": True, "disconnected_at": None}
                r.players[sid] = updated
                if r.current_click_sid == existing_sid:
                    r.current_click_sid = sid
                if existing_sid != sid and existing_sid in r.players:
                    del r.players[existing_sid]
                    _sid_to_room_id.pop(existing_sid, None)
                _sid_to_room_id[sid] = r.room_id
                return sid, r.players[sid]
            if username_hint and info.get("username") == username_hint:
                updated = {**info, "connected": True, "disconnected_at": None}
                r.players[sid] = updated
                if r.current_click_sid == existing_sid:
                    r.current_click_sid = sid
                if existing_sid != sid and existing_sid in r.players:
                    del r.players[existing_sid]
                    _sid_to_room_id.pop(existing_sid, None)
                _sid_to_room_id[sid] = r.room_id
                return sid, r.players[sid]

    return sid, None


def resolve_room_for_event(data=None, player_info=None):
    if player_info and player_info.get("room_id"):
        return get_room(player_info["room_id"])
    reconnect_token = ((data or {}).get("reconnect_token") or "").strip()
    if reconnect_token:
        return get_room(get_room_id_for_reconnect_token(reconnect_token))
    sid = request.sid
    return get_room_for_sid(sid)


# ---------------------------------------------------------------------------
# Bot / turn scheduling
# ---------------------------------------------------------------------------

def schedule_bot_turn_if_needed(room, delay=BOT_FIRST_FLIP_DELAY_SECONDS):
    if room.bot_turn_scheduled:
        return
    if not room.grid_data or len(room.grid_data) < 2:
        return
    if not room.player_order or room.turn >= len(room.player_order):
        return
    if not is_bot_player(room.player_order[room.turn], room):
        return
    bot_sid, bot_info = get_active_bot_identity(room)
    if not bot_info:
        return

    room.bot_turn_scheduled = True

    def bot_take_turn():
        try:
            socketio.sleep(delay)
            if not room.grid_data or not room.player_order or room.turn >= len(room.player_order):
                return
            if not is_bot_player(room.player_order[room.turn], room):
                return
            available = [
                i for i in range(len(room.grid_data))
                if i not in room.matched_indices and i not in room.revealed_cards
            ]
            if len(available) < 2:
                return
            first_i, second_i = choose_bot_turn_indices(room, available)
            process_card_click(first_i, bot_sid, bot_info, room)
            socketio.sleep(BOT_SECOND_FLIP_DELAY_SECONDS)
            if (room.grid_data and room.player_order
                    and room.turn < len(room.player_order)
                    and is_bot_player(room.player_order[room.turn], room)):
                if second_i not in room.matched_indices and second_i not in room.revealed_cards:
                    process_card_click(second_i, bot_sid, bot_info, room)
                else:
                    remaining = [
                        i for i in range(len(room.grid_data))
                        if i not in room.matched_indices and i not in room.revealed_cards
                    ]
                    if remaining:
                        fallback_i = choose_bot_second_index(room, first_i, remaining)
                        if fallback_i is not None:
                            process_card_click(fallback_i, bot_sid, bot_info, room)
        finally:
            room.bot_turn_scheduled = False
            if (room.grid_data and room.player_order
                    and room.turn < len(room.player_order)
                    and is_bot_player(room.player_order[room.turn], room)):
                schedule_bot_turn_if_needed(room, delay=BOT_FIRST_FLIP_DELAY_SECONDS)

    socketio.start_background_task(bot_take_turn)


def get_bot_memory_probability(room):
    return BOT_MEMORY_USE_PROBABILITY.get(room.bot_difficulty or "easy", 0.0)


def remember_card_for_bot(room, index):
    if index < 0 or index >= len(room.grid_data):
        return
    card = room.grid_data[index]
    pair_id = card.get("pair_id", card.get("word"))
    if pair_id:
        room.bot_memory[index] = pair_id


def forget_matched_cards_from_bot_memory(room):
    if not room.bot_memory:
        return
    for idx in list(room.bot_memory.keys()):
        if idx in room.matched_indices:
            room.bot_memory.pop(idx, None)


def snapshot_bot_memory(room, available=None):
    available_set = set(available) if available is not None else None
    snapshot = {}
    for idx, pair_id in room.bot_memory.items():
        if idx in room.matched_indices:
            continue
        if available_set is not None and idx not in available_set:
            continue
        snapshot[idx] = pair_id
    return snapshot


def get_known_bot_pairs(room, available=None, memory_snapshot=None):
    available_set = set(available) if available is not None else None
    pair_to_indices = defaultdict(list)
    source = memory_snapshot if memory_snapshot is not None else snapshot_bot_memory(room, available)
    for idx, pair_id in source.items():
        if available_set is not None and idx not in available_set:
            continue
        pair_to_indices[pair_id].append(idx)
    return [indices[:2] for indices in pair_to_indices.values() if len(indices) >= 2]


def choose_bot_second_index(room, first_i, available, memory_snapshot=None):
    if not available:
        return None
    source = memory_snapshot if memory_snapshot is not None else snapshot_bot_memory(room)
    pair_id = source.get(first_i)
    probability = get_bot_memory_probability(room)
    if pair_id and random.random() < probability:
        candidates = [
            idx for idx, known_pair in source.items()
            if known_pair == pair_id and idx != first_i and idx in available and idx not in room.matched_indices
        ]
        if candidates:
            return random.choice(candidates)
    return random.choice(available)


def choose_bot_turn_indices(room, available):
    memory_snapshot = snapshot_bot_memory(room, available)
    probability = get_bot_memory_probability(room)
    known_pairs = get_known_bot_pairs(room, available, memory_snapshot=memory_snapshot)
    if known_pairs and random.random() < probability:
        chosen = random.choice(known_pairs)
        first_i = random.choice(chosen)
    else:
        first_i = random.choice(available)
    remaining = [idx for idx in available if idx != first_i]
    second_i = choose_bot_second_index(room, first_i, remaining, memory_snapshot=memory_snapshot)
    if second_i is None:
        second_i = random.choice(remaining)
    return first_i, second_i


# ---------------------------------------------------------------------------
# Theme selection helpers
# ---------------------------------------------------------------------------

def theme_selection_active(room):
    return bool(room.theme_selection_state.get("active"))


def sync_theme_selection_players(room):
    if not theme_selection_active(room):
        return []

    current_players = get_effective_players_ordered(room)
    if not current_players:
        return []

    tss = room.theme_selection_state
    old_tokens = dict(tss.get("player_tokens", {}))
    old_counts = dict(tss.get("counts", {}))
    old_ready = dict(tss.get("ready", {}))
    new_counts, new_ready, new_tokens = {}, {}, {}

    for player in current_players:
        username = player["username"]
        reconnect_token = player.get("reconnect_token")
        previous_name = None
        for old_name, old_token in old_tokens.items():
            if reconnect_token and old_token == reconnect_token:
                previous_name = old_name
                break
        if previous_name is None and username in old_counts:
            previous_name = username
        new_counts[username] = old_counts.get(previous_name, old_counts.get(username, 0))
        new_ready[username] = old_ready.get(previous_name, old_ready.get(username, False))
        new_tokens[username] = reconnect_token

    selected_words = tss.get("selected_words", [])
    rename_map = {}
    for player in current_players:
        username = player["username"]
        reconnect_token = player.get("reconnect_token")
        for old_name, old_token in old_tokens.items():
            if reconnect_token and old_token == reconnect_token and old_name != username:
                rename_map[old_name] = username
    if rename_map:
        for item in selected_words:
            if item.get("chosen_by") in rename_map:
                item["chosen_by"] = rename_map[item["chosen_by"]]

    room.player_order = [p["username"] for p in current_players]
    tss["counts"] = new_counts
    tss["ready"] = new_ready
    tss["player_tokens"] = new_tokens
    return current_players


def build_theme_selection_payload(room, message=None):
    if not theme_selection_active(room):
        return {"active": False}
    sync_theme_selection_players(room)
    tss = room.theme_selection_state
    payload = {
        "active": True,
        "theme": tss.get("theme"),
        "starter_name": tss.get("starter_name"),
        "search_theme": tss.get("search_theme"),
        "candidates": tss.get("candidates", []),
        "candidate_labels": tss.get("candidate_labels", {}),
        "selected_words": tss.get("selected_words", []),
        "rejected_words": tss.get("rejected_words", []),
        "counts": tss.get("counts", {}),
        "ready": tss.get("ready", {}),
        "swap_limit": tss.get("swap_limit", 4),
        "mode": room.game_mode,
    }
    if message:
        payload["message"] = message
    return payload


def emit_theme_selection_state(room, message=None, sid=None):
    payload = build_theme_selection_payload(room, message=message)
    if not payload.get("active"):
        return
    if sid:
        socketio.emit("theme_selection_updated", payload, to=sid)
    else:
        emit_to_room("theme_selection_updated", payload, room_id=room.room_id)


def deactivate_theme_selection(room):
    room.theme_selection_state = {}
    room.theme_generation_in_progress = False
    room.lang_generation_in_progress = False


def normalize_display_label(word):
    return normalize_candidate_word(str(word or "").strip())


def normalize_theme_family_key(word):
    normalized = normalize_candidate_word(word)
    if not normalized:
        return None
    if normalized.endswith("ies") and len(normalized) > 5:
        return normalized[:-3] + "y"
    if normalized.endswith("es") and len(normalized) > 5 and not normalized.endswith("ses"):
        return normalized[:-2]
    if normalized.endswith("s") and len(normalized) > 4 and not normalized.endswith("ss"):
        return normalized[:-1]
    return normalized


def get_theme_display_word(word, entry=None, room=None):
    base_word = str(word or "").strip()
    if not base_word:
        return ""
    ui_language = room.ui_language if room else "en"
    if ui_language == "en":
        return base_word
    if entry and entry.get("type") in {"language", "spanish", "image_word", "words"}:
        pair = entry.get("pair") or {}
        native_word = (pair.get("native_word") or pair.get("finnish_word") or "").strip()
        if native_word:
            return native_word
    translated = translate_word(base_word, "en", ui_language)
    return translated or base_word


def build_candidate_labels(words, room=None):
    return {word: get_theme_display_word(word, room=room) for word in (words or [])}


def filter_theme_candidate_pool(words, room=None, excluded_display_keys=None):
    filtered = []
    labels = {}
    seen_words = set()
    seen_families = set()
    seen_display = set(excluded_display_keys or set())
    for word in (words or []):
        if word in seen_words:
            continue
        seen_words.add(word)
        family_key = normalize_theme_family_key(word)
        if family_key and family_key in seen_families:
            continue
        display_word = get_theme_display_word(word, room=room)
        display_key = normalize_display_label(display_word)
        if display_key and display_key in seen_display:
            continue
        filtered.append(word)
        labels[word] = display_word
        if family_key:
            seen_families.add(family_key)
        if display_key:
            seen_display.add(display_key)
    return filtered, labels


def build_theme_candidate_list(search_theme, candidate_count=24, room=None):
    require_noun = getattr(room, "word_filter_mode", "clear") == "clear"
    raw = fetch_theme_words(search_theme, max_results=96, require_noun=require_noun, exclude_proper=False)
    filtered, seen = [], set()
    for word in raw:
        if word in seen or not is_word_allowed_for_filter_mode(word, room):
            continue
        seen.add(word)
        filtered.append(word)
        if len(filtered) >= max(candidate_count * 2, 24):
            break
    unique_filtered, _ = filter_theme_candidate_pool(filtered, room=room)
    return unique_filtered[:candidate_count]


def prepare_theme_selection(starter_name, room):
    search_theme = room.pending_search_theme or room.pending_theme
    candidates = build_theme_candidate_list(search_theme, candidate_count=48, room=room)
    if len(candidates) < 8:
        message = f"Teemasta '{room.pending_theme}' ei löytynyt tarpeeksi käyttökelpoisia sanoja. Kokeile toista teemaa."
        print(f"[WARNING] {message}")
        room.grid_data.clear()
        reset_pending_state(room)
        emit_to_room("game_setup_error", {"reason": message}, room_id=room.room_id)
        return

    counts = {name: 0 for name in room.player_order}
    ready = {name: is_bot_player(name, room) for name in room.player_order}
    selected_words = []
    rejected_words = []
    remaining_candidates = []
    selected_display_keys = set()

    emit_to_room("theme_generation_started", {
        "theme": room.pending_theme,
        "pair": room.pending_pair + 1,
        "mode": room.game_mode,
        "starter_name": starter_name,
        "phase": "drawing_cards",
        "progress_count": room.pending_pair,
        "total_pairs": 8
    }, room_id=room.room_id)
    socketio.sleep(0)

    generation_start = time.monotonic()
    THEME_FIRST_WORD_TIMEOUT = 10  # seconds

    for word in candidates:
        if len(selected_words) >= 8:
            remaining_candidates.append(word)
            continue
        pair_index = len(selected_words)
        try:
            pair_entry = build_pair_entry_for_mode(word, pair_index, room)
        except PixabayConfigError as e:
            abort_round_due_to_pixabay_error(str(e), room)
            return
        if pair_entry is None:
            rejected_words.append(word)
            room.theme_rejected_words.add(word)
            if not selected_words and (time.monotonic() - generation_start) > THEME_FIRST_WORD_TIMEOUT:
                print(f"[WARNING] Teemalle '{room.pending_theme}' ei löytynyt yhtään sanaa {THEME_FIRST_WORD_TIMEOUT}s kuluessa.")
                reset_pending_state(room)
                emit_to_room("theme_timeout", {"theme": room.pending_theme}, room_id=room.room_id)
                return
            continue
        display_word = get_theme_display_word(word, pair_entry, room)
        display_key = normalize_display_label(display_word)
        if display_key and display_key in selected_display_keys:
            rejected_words.append(word)
            room.theme_rejected_words.add(word)
            continue
        selected_words.append({
            "word": word,
            "display_word": display_word,
            "chosen_by": starter_name,
            "entry": pair_entry
        })
        if display_key:
            selected_display_keys.add(display_key)
        emit_to_room("theme_word_accepted", {
            "theme": room.pending_theme,
            "word": word,
            "pair": len(selected_words),
            "total_pairs": 8
        }, room_id=room.room_id)
        socketio.sleep(0)

    if len(selected_words) < 8:
        message = f"Teemasta '{room.pending_theme}' ei löytynyt tarpeeksi käyttökelpoisia sanoja. Kokeile toista teemaa."
        print(f"[WARNING] {message}")
        room.grid_data.clear()
        reset_pending_state(room)
        emit_to_room("game_setup_error", {"reason": message}, room_id=room.room_id)
        return

    # remaining_candidates no longer needed — word swap phase removed
    del remaining_candidates

    room.theme_selection_state = {
        "active": True,
        "theme": room.pending_theme,
        "search_theme": search_theme,
        "starter_name": starter_name,
        "candidates": [],
        "candidate_labels": {},
        "selected_words": selected_words,
        "rejected_words": rejected_words,
        "counts": counts,
        "ready": ready,
        "player_tokens": {
            p["username"]: p.get("reconnect_token")
            for p in get_effective_players_ordered(room)
        },
        "swap_limit": 0,
    }
    print(f"[INFO] Teeman '{room.pending_theme}' sanat valittu. Käynnistetään suoraan.")
    finalize_theme_selection(room)


# ---------------------------------------------------------------------------
# Utility: Spanish pair
# ---------------------------------------------------------------------------

def append_selected_lang_pair(word, pair_index, room, source_lang=None):
    """Build a language-learning card pair for `word`.

    source_lang: the language the word is provided in (default "en").
    For manual input, pass room.ui_language so the translation uses the
    correct source; Pixabay search is always done in English.
    """
    target_lang = room.target_language or "es"
    src = source_lang or "en"
    target_word = translate_word(word, src, target_lang)
    if not target_word:
        print(f"[INFO] Käännös ({src}→{target_lang}) ei kelpaa sanalle '{word}'")
        return False
    native_lang = room.native_language or "fi"
    # native_word is the word in UI language — if src IS the native lang, use the input word directly
    if src == native_lang:
        native_word = word
    elif native_lang == "en":
        native_word = translate_word(word, src, "en")
    else:
        native_word = translate_word(word, src, native_lang)
    if not native_word:
        print(f"[INFO] Käännös ({src}→{native_lang}) ei kelpaa sanalle '{word}', ohitetaan")
        return False
    # english_word for Pixabay search
    english_word = word if src == "en" else (translate_word(word, src, "en") or word)
    print(f"[INFO] Kokeillaan {target_lang}-pariksi '{word}' ({src}) -> '{target_word}' parille {pair_index + 1}")
    # Words-only mode: skip Pixabay entirely
    if (room.card_mode or "image_word") == "words":
        return {
            "pair_id": pair_index + 1,
            "english_word": english_word,
            "target_word": target_word,
            "native_word": native_word,
            "image_url": None,
        }
    image_paths = fetch_and_save_pixabay_images(english_word, room, required_count=1)
    if not image_paths:
        print(f"[INFO] Pixabay ei löytänyt parille '{word}' sopivaa kuvaa")
        return False
    return {
        "pair_id": pair_index + 1,
        "english_word": english_word,
        "target_word": target_word,
        "native_word": native_word,
        "image_url": image_source_for_card(image_paths[0]),
    }


def abort_round_due_to_pixabay_error(message, room):
    print(f"[ERROR] {message}")
    room.grid_data.clear()
    reset_pending_state(room)
    emit_to_room("game_setup_error", {"reason": message}, room_id=room.room_id)


def next_theme_picker_name(current_name, room):
    if len(room.player_order) < 2:
        return None
    if current_name == room.player_order[0]:
        return room.player_order[1]
    return room.player_order[0]


def lang_setup_still_active(theme, room):
    if room.pending_pair is None or room.pending_pair == 0 and not room.grid_data:
        return False
    if room.game_mode != "theme" or (room.card_mode or "image_word") == "images":
        return False
    if room.pending_theme != theme:
        return False
    if not solo_or_enough_players(room):
        return False
    return True


# ---------------------------------------------------------------------------
# Word/pair normalization
# ---------------------------------------------------------------------------

def normalize_candidate_word(word):
    cleaned = str(word or "").strip().lower()
    if not re.fullmatch(r"[a-zäöåáéíóúàèìòùâêîôûñüæøßçğışõūēīā]{2,24}", cleaned):
        return None
    return cleaned


def normalize_translated_word(word):
    cleaned = str(word or "").strip().lower()
    # Strip leading/trailing non-letter characters (unicode-aware)
    cleaned = re.sub(r"^[^\w]+|[^\w]+$", "", cleaned)
    # Strip common Spanish articles (harmless for other languages)
    cleaned = re.sub(r"^(el|la|los|las|un|una|unos|unas|le|les|der|die|das|il|lo)\s+", "", cleaned, flags=re.IGNORECASE)
    if not cleaned or len(cleaned) < 2 or len(cleaned) > 24:
        return None
    # Reject if it contains digits or looks like a phrase (space = multiple words)
    if re.search(r"[0-9]", cleaned):
        return None
    return cleaned


def is_concrete_theme_word(word):
    return word not in ABSTRACT_THEME_WORDS


def is_word_allowed_for_filter_mode(word, room=None):
    normalized = normalize_candidate_word(word)
    if not normalized:
        return False
    filter_mode = getattr(room, "word_filter_mode", "clear")
    if filter_mode != "clear":
        return is_concrete_theme_word(normalized)
    if normalized in CLEAR_PICTURE_WORD_BLACKLIST:
        return False
    if not is_concrete_theme_word(normalized):
        return False
    if any(normalized.endswith(suffix) for suffix in CLEAR_PICTURE_WORD_SUFFIXES):
        return False
    if len(normalized) > 15 and normalized.endswith(("al", "ic", "ous")):
        return False
    return True


def fetch_random_game_words(target=8, room=None):
    """Pick `target` distinct concrete words from random themes."""
    require_noun = getattr(room, "word_filter_mode", "clear") == "clear"
    # Fetch first 6 themes in parallel, then serial with early-stop.
    # Fetching all themes at once kills the early-stop benefit and overloads Datamuse.
    parallel_limit = 6
    all_themes = random.sample(RANDOM_WORD_THEMES, min(len(RANDOM_WORD_THEMES), target * 2))
    batch, rest = all_themes[:parallel_limit], all_themes[parallel_limit:]
    print(f"[INFO] Haetaan satunnaissanoja teemoista: {', '.join(all_themes)}")
    theme_results = {}
    def _fetch_theme(theme):
        theme_results[theme] = fetch_theme_words(theme, max_results=20, require_noun=require_noun, exclude_proper=True)
    gevent.joinall([gevent.spawn(_fetch_theme, t) for t in batch], timeout=20)
    pool = []
    seen = set()
    for theme in batch:
        for w in theme_results.get(theme, []):
            if w and w not in seen and is_word_allowed_for_filter_mode(w, room):
                seen.add(w)
                pool.append(w)
    for theme in rest:
        if len(pool) >= target * 4:
            break
        for w in fetch_theme_words(theme, max_results=20, require_noun=require_noun, exclude_proper=True):
            if w and w not in seen and is_word_allowed_for_filter_mode(w, room):
                seen.add(w)
                pool.append(w)
    print(f"[INFO] Raaka sanapooli: {len(pool)} sanaa")
    # Pre-warm display-word translations in parallel before filtering (avoids ~80 serial API calls)
    ui_language = room.ui_language if room else "en"
    if ui_language != "en":
        jobs = [gevent.spawn(translate_word, w, "en", ui_language) for w in pool]
        gevent.joinall(jobs, timeout=30)
        print(f"[INFO] Pool-käännökset esivalmistelltu ({len(pool)} sanaa, {ui_language})")
    pool, _ = filter_theme_candidate_pool(pool, room=room)
    if len(pool) < target:
        return pool
    return random.sample(pool, target)


def has_noun_tag(tags):
    return any(isinstance(t, str) and t.lower() == "n" for t in (tags or []))


def has_proper_tag(tags):
    for t in (tags or []):
        if not isinstance(t, str):
            continue
        lowered = t.lower()
        if lowered in {"prop", "place", "geog"} or "prop" in lowered:
            return True
    return False


# ---------------------------------------------------------------------------
# External API helpers
# ---------------------------------------------------------------------------

def fetch_theme_words(theme, max_results=60, require_noun=False, exclude_proper=False):
    theme = str(theme or "").strip()
    if not theme:
        return []
    cache_key = (theme.lower(), int(max_results), bool(require_noun), bool(exclude_proper))
    cached = theme_words_cache.get(cache_key)
    if cached is not None:
        debug(f"[DEBUG] Datamuse-cache osuma teemalle '{theme}'")
        return list(cached)

    print(f"[INFO] Haetaan Datamusesta teemasanat teemalle '{theme}'")
    all_words, seen = [], set()
    # rel_trg = words triggered by (associative): "sport" -> football, basketball, swimming
    # topics  = topic filter: broader set of thematically related words
    # ml      = means like (synonyms): fallback only, tends to return abstract words
    query_variants = [
        {"rel_trg": theme, "max": max_results, "md": "p"},
        {"topics": theme, "max": max_results, "md": "p"},
    ]
    target_min = min(max_results, 32)

    for index, params in enumerate(query_variants):
        try:
            response = requests.get("https://api.datamuse.com/words", params=params, timeout=6)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as e:
            debug(f"[DEBUG] Datamuse-pyyntö epäonnistui ({params}): {e}")
            continue

        for item in payload:
            word = normalize_candidate_word(item.get("word"))
            if not word or word in seen:
                continue
            tags = item.get("tags") or []
            if require_noun and not has_noun_tag(tags):
                continue
            if exclude_proper and has_proper_tag(tags):
                continue
            seen.add(word)
            all_words.append(word)

        if index == 0 and len(all_words) < target_min:
            query_variants.append({"ml": theme, "max": max_results, "md": "p"})

    preview = ", ".join(all_words[:12]) if all_words else "(ei sanoja)"
    print(f"[INFO] Datamuse ehdotti teemalle '{theme}' {len(all_words)} sanaa: {preview}")
    theme_words_cache[cache_key] = list(all_words)
    return all_words


def translate_word(word, source_lang, target_lang):
    normalized_word = normalize_candidate_word(word)
    if not normalized_word:
        return None
    if source_lang == target_lang:
        return normalized_word
    cache_key = (normalized_word, source_lang, target_lang)
    if cache_key in translation_cache:
        return translation_cache[cache_key]

    try:
        response = requests.get(
            TRANSLATION_API_URL,
            params={"q": normalized_word, "langpair": f"{source_lang}|{target_lang}"},
            timeout=5
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[WARNING] Käännös epäonnistui sanalle '{normalized_word}': {e}")
        translation_cache[cache_key] = None
        return None

    translated_text = ((payload.get("responseData") or {}).get("translatedText") or "")
    translated_text = re.sub(r"\s*\(.*?\)\s*", " ", translated_text).strip()
    if any(sep in translated_text for sep in [",", ";", "/", "|"]):
        translation_cache[cache_key] = None
        return None

    normalized_translation = normalize_translated_word(translated_text)
    if not normalized_translation:
        translation_cache[cache_key] = None
        return None
    if target_lang != "es" and normalize_candidate_word(normalized_translation) == normalized_word:
        translation_cache[cache_key] = None
        return None
    translation_cache[cache_key] = normalized_translation
    return normalized_translation


def translate_word_to_spanish(word):
    return translate_word(word, "en", "es")


def translate_word_to_finnish(word):
    return translate_word(word, "en", "fi")


def translate_word_to_english(word, source_lang="fi"):
    normalized = normalize_candidate_word(word)
    if not normalized:
        return None
    if source_lang == "en":
        return normalized
    translated = translate_word(normalized, source_lang, "en")
    return normalize_candidate_word(translated) or normalized


def translate_theme_to_english(theme, ui_language):
    theme_text = str(theme or "").strip()
    if not theme_text:
        return None
    src_lang = str(ui_language or "en").strip().lower()
    # English: just apply manual fixes, no API call needed
    if src_lang == "en":
        translated = THEME_TRANSLATION_FIXES.get(theme_text.lower(), theme_text)
        return translated or theme_text
    theme_key = normalize_candidate_word(theme_text)
    cache_key = (theme_key or theme_text.lower(), src_lang)
    if cache_key in theme_translation_cache:
        return theme_translation_cache[cache_key]
    # Finnish: check local overrides first
    if src_lang == "fi" and theme_key and theme_key in THEME_TRANSLATION_OVERRIDES:
        translated = THEME_TRANSLATION_OVERRIDES[theme_key]
        print(f"[INFO] Teema käännetty paikallisesti: '{theme_text}' -> '{translated}'")
        theme_translation_cache[cache_key] = translated
        return translated
    # Any non-English language: translate via MyMemory
    try:
        response = requests.get(
            TRANSLATION_API_URL,
            params={"q": theme_text, "langpair": f"{src_lang}|en"},
            timeout=10
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[WARNING] Teeman käännös epäonnistui ('{theme_text}', {src_lang}→en): {e}")
        theme_translation_cache[cache_key] = theme_text
        return theme_text
    translated_text = ((payload.get("responseData") or {}).get("translatedText") or "").strip()
    translated_text = re.sub(r"\s*\(.*?\)\s*", " ", translated_text).strip()
    translated_text = re.sub(r"\s+", " ", translated_text)
    if not translated_text or any(sep in translated_text for sep in [";", "/", "|"]):
        theme_translation_cache[cache_key] = theme_text
        return theme_text
    translated_text = THEME_TRANSLATION_FIXES.get(translated_text.lower(), translated_text)
    print(f"[INFO] Teema käännetty ({src_lang}→en): '{theme_text}' -> '{translated_text}'")
    theme_translation_cache[cache_key] = translated_text
    return translated_text


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def image_source_for_card(image_ref):
    if not image_ref:
        return image_ref
    if isinstance(image_ref, str) and image_ref.startswith(("http://", "https://")):
        return image_ref
    return "/" + str(image_ref).replace("\\", "/").lstrip("/")


def _prefetch_pixabay_cache(word):
    """Populate pixabay_cache for word without touching any room state."""
    pixabay_api_key = (os.getenv("PIXABAY_API_KEY") or "").strip()
    if not pixabay_api_key:
        return
    cache_key = normalize_candidate_word(word) or str(word or "").strip().lower()
    if cache_key in pixabay_cache:
        return
    try:
        response = requests.get("https://pixabay.com/api/", params={
            "key": pixabay_api_key, "q": word, "image_type": "photo",
            "orientation": "horizontal", "per_page": 6, "safesearch": "true"
        }, timeout=8)
        if response.ok:
            pixabay_cache[cache_key] = response.json().get("hits") or []
    except Exception:
        pass


def fetch_and_save_pixabay_images(word, room, required_count=2):
    debug(f"[DEBUG] Haetaan Pixabaysta kuvia sanalla: {word}")
    pixabay_api_key = (os.getenv("PIXABAY_API_KEY") or "").strip()
    if not pixabay_api_key:
        raise PixabayConfigError("Pixabay API key is missing. Check PIXABAY_API_KEY.")
    url = "https://pixabay.com/api/"
    params = {
        "key": pixabay_api_key,
        "q": word,
        "image_type": "photo",
        "orientation": "horizontal",
        "per_page": 6,
        "safesearch": "true"
    }
    cache_key = normalize_candidate_word(word) or str(word or "").strip().lower()
    hits = pixabay_cache.get(cache_key)
    if hits is None:
        try:
            response = requests.get(url, params=params, timeout=8)
            if response.status_code in {400, 401, 403}:
                raise PixabayConfigError("Pixabay rejected the request. Check PIXABAY_API_KEY.")
            response.raise_for_status()
        except requests.RequestException as e:
            print(f"[ERROR] Pixabay-pyyntö epäonnistui: {e}")
            return None
        debug(f"[DEBUG] Pixabay HTTP status: {response.status_code}")
        try:
            data = response.json()
        except Exception as e:
            print(f"[ERROR] Pixabay JSON decode error: {e}")
            return None
        hits = data.get("hits") or []
        pixabay_cache[cache_key] = list(hits)
    else:
        debug(f"[DEBUG] Pixabay-cache osuma sanalle: {word}")

    selected_hits, local_ids, skipped = [], set(), 0
    for hit in hits:
        image_id = hit.get("id")
        if image_id is None:
            continue
        if image_id in room.used_image_ids or image_id in local_ids:
            skipped += 1
            continue
        selected_hits.append(hit)
        local_ids.add(image_id)
        if len(selected_hits) >= max(required_count + 2, required_count):
            break

    if skipped:
        print(f"[INFO] Ohitettiin {skipped} jo käytettyä Pixabay-kuvaa sanalle '{word}'")

    if len(selected_hits) >= required_count:
        image_refs = []
        for hit in selected_hits:
            img_url = hit.get("webformatURL") or hit.get("previewURL") or hit.get("largeImageURL")
            if not img_url:
                continue
            image_refs.append(img_url)
            room.used_image_ids.add(hit["id"])
            if len(image_refs) >= required_count:
                break
        if len(image_refs) >= required_count:
            return image_refs
        print(f"[INFO] Sanalle '{word}' ei löytynyt tarpeeksi käytettäviä Pixabay-kuvia")
        return None
    else:
        print(f"[INFO] Sanalle '{word}' ei löytynyt tarpeeksi uusia Pixabay-kuvia")
        return None


def get_image_pair_display_word(word, room):
    if room.game_mode in {"theme", "random"}:
        return get_theme_display_word(word, room=room)
    return word


def append_word_images_to_grid(word, room, pair_index=None, display_word=None):
    search_word = word
    if room.game_mode == "manual":
        search_word = translate_word_to_english(word, "fi" if room.ui_language == "fi" else "en") or word
        if normalize_candidate_word(search_word) != normalize_candidate_word(word):
            print(f"[INFO] Manual-pelin Pixabay-haku englanniksi: '{word}' -> '{search_word}'")
    result = fetch_and_save_pixabay_images(search_word, room)
    if not result:
        print(f"[INFO] Pixabay-haku epäonnistui sanalle '{word}'")
        return False
    print(f"[INFO] Pixabaysta löytyi kuvat sanalle '{word}': {result}")
    resolved_pair_index = room.pending_pair if pair_index is None else pair_index
    resolved_display_word = display_word or get_image_pair_display_word(word, room)
    for path in result:
        room.grid_data.append({
            "pair_id": resolved_pair_index + 1,
            "image": image_source_for_card(path),
            "word": word,
            "display_word": resolved_display_word,
        })
    return True


def append_lang_learning_pair_to_grid(pair, room, card_mode=None):
    mode = card_mode or room.card_mode or "image_word"
    common = {
        "word": pair["english_word"],
        "target_word": pair["target_word"],
        "native_word": pair.get("native_word"),
    }
    # Card A: always the target language word
    room.grid_data.append({
        "pair_id": pair["pair_id"],
        "card_type": "word",
        "text": pair["target_word"],
        **common,
    })
    # Card B: image (image_word mode) or native language word (words mode)
    if mode == "words":
        room.grid_data.append({
            "pair_id": pair["pair_id"],
            "card_type": "word",
            "text": pair.get("native_word") or pair["english_word"],
            **common,
        })
    else:
        room.grid_data.append({
            "pair_id": pair["pair_id"],
            "card_type": "image",
            "image": pair.get("image_url"),
            **common,
        })


def build_theme_pair_entry(word, room):
    result = fetch_and_save_pixabay_images(word, room)
    if not result:
        print(f"[INFO] Pixabay-haku epäonnistui sanalle '{word}'")
        return None
    print(f"[INFO] Pixabaysta löytyi kuvat sanalle '{word}': {result}")
    return {
        "type": "theme",
        "word": word,
        "display_word": get_theme_display_word(word, room=room),
        "images": [image_source_for_card(path) for path in result]
    }


def build_pair_entry_for_mode(word, pair_index, room):
    card_mode = room.card_mode or "image_word"
    if card_mode == "images":
        return build_theme_pair_entry(word, room)
    if card_mode in {"image_word", "words"}:
        pair = append_selected_lang_pair(word, pair_index, room)
        if not pair:
            return None
        return {"type": card_mode, "pair": pair}
    return None


def append_pair_entry_to_grid(entry, pair_index, room):
    if not entry:
        return
    if entry.get("type") == "theme":
        for image in entry.get("images", []):
            room.grid_data.append({
                "pair_id": pair_index + 1,
                "image": image,
                "word": entry.get("word"),
                "display_word": entry.get("display_word") or entry.get("word"),
            })
        return
    if entry.get("type") in {"image_word", "words", "language", "spanish"}:
        pair = dict(entry.get("pair") or {})
        pair["pair_id"] = pair_index + 1
        append_lang_learning_pair_to_grid(pair, room, card_mode=entry.get("type"))


# ---------------------------------------------------------------------------
# Core game logic
# ---------------------------------------------------------------------------

def launch_grid_round(room):
    room.matched_indices = set()
    room.revealed_cards = []
    room.turn = 0
    room.bot_memory = {}
    if not room.queue_round_prepared:
        random.shuffle(room.grid_data)
    has_bot = any(info.get("is_bot") for info in room.players.values())
    if queue_can_prepare_round_while_waiting(room) and not has_bot:
        emit_queue_round_prepared(room)
        return
    room.queue_round_prepared = False
    room.player_points = {name: 0 for name in room.player_order}
    if is_solo(room) or has_bot:
        room.solo_start_time = time.time()
        room.solo_mistakes = 0
        room.solo_seen_cards = set()
    room.status = "playing"
    for p in room.players.values():
        p["in_waiting"] = False
    emit_to_room("init_grid", {
        "cards": room.grid_data,
        "turn": room.player_order[0] if room.player_order else None,
        "players": room.player_order,
        "solo": is_solo(room),
        "play_mode": room.play_mode,
        "game_mode": room.game_mode,
        "card_mode": room.card_mode,
        "image_pair_tracker": build_image_pair_tracker_entries(room),
        "target_language": room.target_language,
        "native_language": room.native_language,
        "bot_difficulty": room.bot_difficulty,
        "word_filter_mode": room.word_filter_mode,
        "room_id": room.room_id,
    }, room_id=room.room_id)
    schedule_bot_turn_if_needed(room)


def conclude_round(winner_label, room, surrendered_by=None):
    points_payload = {name: room.player_points.get(name, 0) for name in room.player_order}
    if (isinstance(winner_label, str)
            and winner_label not in {"Tasapeli", "Tie"}
            and winner_label in room.player_order):
        room.round_win[winner_label] += 1
        print(f"[INFO] Voittaja: {winner_label}. Erävoitot: {dict(room.round_win)}")
    else:
        print(f"[INFO] Kierros päättyi. Tulos: {winner_label}")

    has_bot = any(info.get("is_bot") for info in room.players.values())
    elapsed = round(time.time() - room.solo_start_time) if room.solo_start_time else None
    solo_time = elapsed if is_solo(room) else None
    bot_time = elapsed if has_bot else None

    # --- Save to leaderboard ---
    if not surrendered_by:
        card_mode = room.card_mode or "image_word"
        target_lang = room.target_language if card_mode != "images" else None
        if is_solo(room) and room.player_order:
            save_result(
                username=room.player_order[0],
                play_mode="solo",
                game_mode=room.game_mode,
                pairs_found=room.player_points.get(room.player_order[0], 0),
                time_secs=solo_time,
                mistakes=room.solo_mistakes,
                card_mode=card_mode,
                target_language=target_lang
            )
        else:
            for name in room.player_order:
                if is_bot_player(name, room):
                    continue  # don't save bot's own result
                if has_bot:
                    save_result(
                        username=name,
                        play_mode="bot",
                        game_mode=room.game_mode,
                        pairs_found=room.player_points.get(name, 0),
                        time_secs=bot_time,
                        mistakes=room.solo_mistakes,
                        card_mode=card_mode,
                        target_language=target_lang,
                        bot_difficulty=room.bot_difficulty
                    )
                else:
                    round_won = 1 if name == winner_label else 0
                    round_result = "tie" if winner_label in {"Tasapeli", "Tie"} else ("win" if name == winner_label else "loss")
                    save_result(
                        username=name,
                        play_mode="multiplayer",
                        game_mode=room.game_mode,
                        pairs_found=room.player_points.get(name, 0),
                        round_won=round_won,
                        card_mode=card_mode,
                        target_language=target_lang,
                        round_result=round_result
                    )

    emit_to_room("game_over", {
        "winner": winner_label,
        "points": points_payload,
        "round_win": dict(room.round_win),
        "surrendered_by": surrendered_by,
        "solo_time": solo_time,
        "solo_mistakes": room.solo_mistakes if is_solo(room) else None
    }, room_id=room.room_id)


def build_image_pair_tracker_entries(room):
    if (room.card_mode or "image_word") != "images":
        return []
    entries = []
    by_key = {}
    for index, card in enumerate(room.grid_data or []):
        key = card.get("pair_id", card.get("word"))
        label = card.get("display_word") or card.get("word") or ""
        if not key or not label:
            continue
        if key not in by_key:
            by_key[key] = {"key": str(key), "label": label, "indices": []}
            entries.append(by_key[key])
        by_key[key]["indices"].append(index)
    return entries

    try:
        room.grid_data.clear()
        room.revealed_cards.clear()
        room.matched_indices.clear()
    except Exception:
        pass
    room.turn = 0
    room.current_click_sid = None
    room.bot_turn_scheduled = False
    room.bot_memory = {}
    room.player_points = {}
    room.status = "results"
    reset_pending_state(room)


def finalize_theme_selection(room):
    sync_theme_selection_players(room)
    selected_words = list(room.theme_selection_state.get("selected_words", []))
    if len(selected_words) < 8:
        return
    room.grid_data.clear()
    for pair_index, item in enumerate(selected_words):
        append_pair_entry_to_grid(item.get("entry"), pair_index, room)
    deactivate_theme_selection(room)
    room.pending_player = None
    room.pending_pair = 0
    launch_grid_round(room)


def generate_theme_pair(room):
    existing_words = {item["word"] for item in room.grid_data}
    attempts = 0

    while attempts < 40:
        if not room.theme_candidates:
            search_theme = room.pending_search_theme or room.pending_theme
            room.theme_candidates = fetch_theme_words(search_theme, max_results=100)
            if not room.theme_candidates:
                break

        word = room.theme_candidates.pop(0)
        if word in existing_words or word in room.theme_rejected_words:
            attempts += 1
            continue

        pair_index = room.pending_pair
        print(f"[INFO] Kokeillaan teemasanaksi '{word}' parille {pair_index + 1}")
        try:
            selection_ok = append_word_images_to_grid(word, room)
        except PixabayConfigError as e:
            abort_round_due_to_pixabay_error(str(e), room)
            return
        if selection_ok:
            emit_to_room("theme_word_accepted", {
                "theme": room.pending_theme,
                "word": word,
                "pair": pair_index + 1,
                "total_pairs": 8
            }, room_id=room.room_id)
            socketio.sleep(0)
            room.pending_pair += 1
            existing_words.add(word)
            if room.pending_pair < 8:
                attempts = 0
                continue
            break
        else:
            room.theme_rejected_words.add(word)
            attempts += 1

    room.theme_generation_in_progress = False
    if room.pending_pair >= 8:
        try:
            ask_next_word(room)
        finally:
            room.theme_generation_in_progress = False
        return

    message = f"Teemasta '{room.pending_theme}' ei löytynyt tarpeeksi käyttökelpoisia sanoja."
    print(f"[WARNING] {message}")
    room.grid_data.clear()
    reset_pending_state(room)
    emit_to_room("game_setup_error", {"reason": message}, room_id=room.room_id)


def generate_lang_learning_pairs(theme, room, target_pairs=8):
    existing_words = set()
    search_theme = room.pending_search_theme or theme
    candidates = fetch_theme_words(search_theme, max_results=60)
    if not candidates:
        message = f"Teemasta '{theme}' ei löytynyt tarpeeksi käyttökelpoisia sanoja."
        print(f"[WARNING] {message}")
        room.grid_data.clear()
        reset_pending_state(room)
        emit_to_room("game_setup_error", {"reason": message}, room_id=room.room_id)
        room.lang_generation_in_progress = False
        return

    for word in candidates:
        if not lang_setup_still_active(theme, room):
            print("[INFO] Kielipelin generointi lopetettiin keskeytyneen pelin vuoksi")
            room.lang_generation_in_progress = False
            return

        if word in existing_words or word in room.theme_rejected_words:
            continue

        pair_index = room.pending_pair
        try:
            pair = append_selected_lang_pair(word, pair_index, room)
        except PixabayConfigError as e:
            abort_round_due_to_pixabay_error(str(e), room)
            room.lang_generation_in_progress = False
            return

        if not pair:
            room.theme_rejected_words.add(word)
            continue

        english_word = pair.get("english_word", word)
        append_lang_learning_pair_to_grid(pair, room)
        existing_words.add(english_word)
        emit_to_room("theme_word_accepted", {
            "theme": room.pending_theme,
            "word": english_word,
            "pair": room.pending_pair + 1,
            "total_pairs": target_pairs,
            "mode": "language"
        }, room_id=room.room_id)
        socketio.sleep(0)
        room.pending_pair += 1

        if not lang_setup_still_active(theme, room):
            print("[INFO] Kielipelin generointi lopetettiin keskeytyneen pelin vuoksi")
            room.lang_generation_in_progress = False
            return

        if room.pending_pair >= target_pairs:
            try:
                ask_next_word(room)
            finally:
                room.lang_generation_in_progress = False
            return

    room.lang_generation_in_progress = False
    message = f"Teemasta '{theme}' ei löytynyt tarpeeksi käyttökelpoisia sanoja. Kokeile toista teemaa."
    print(f"[WARNING] {message}")
    room.grid_data.clear()
    reset_pending_state(room)
    emit_to_room("game_setup_error", {"reason": message}, room_id=room.room_id)


def process_card_click(index, resolved_sid, clicker, room):
    clicker_name = (clicker or {}).get("username")
    current_player_name = (
        room.player_order[room.turn]
        if room.player_order and 0 <= room.turn < len(room.player_order)
        else None
    )
    if not clicker_name or clicker_name != current_player_name:
        debug(f"[DEBUG] Hylättiin klikkaus ei-aktiiviselta: {clicker_name} (vuoro: {current_player_name})")
        return
    if index in room.matched_indices or index in room.revealed_cards:
        return

    debug(f"[DEBUG] Kortti klikattu: index {index}, sana: {room.grid_data[index]['word']}")
    if len(room.revealed_cards) == 0:
        room.current_click_sid = resolved_sid
    elif len(room.revealed_cards) == 1 and room.current_click_sid != resolved_sid:
        debug("[DEBUG] Hylättiin toisen kortin klikkaus eri asiakkaalta")
        return
    room.revealed_cards.append(index)
    emit_to_room("reveal_card", {"index": index, "card": room.grid_data[index]}, room_id=room.room_id)
    remember_card_for_bot(room, index)

    if len(room.revealed_cards) == 2:
        idx1, idx2 = room.revealed_cards
        word1 = room.grid_data[idx1]["word"]
        word2 = room.grid_data[idx2]["word"]
        match_key1 = room.grid_data[idx1].get("pair_id", word1)
        match_key2 = room.grid_data[idx2].get("pair_id", word2)

        if match_key1 == match_key2:
            room.matched_indices.update(room.revealed_cards)
            forget_matched_cards_from_bot_memory(room)
            debug(f"[DEBUG] Pari löytyi: {word1}")
            emit_to_room("pair_found", {"indices": room.revealed_cards, "word": word1}, room_id=room.room_id)
            room.revealed_cards = []
            room.current_click_sid = None
            current_player_name = (
                room.player_order[room.turn] if 0 <= room.turn < len(room.player_order) else None
            )
            if current_player_name is not None:
                room.player_points[current_player_name] = room.player_points.get(current_player_name, 0) + 1
                debug(f"[DEBUG] Piste {current_player_name}. Pisteet: {room.player_points}")

            if len(room.matched_indices) == len(room.grid_data):
                print("[INFO] Kaikki parit löytyneet – peli ohi!")
                if room.player_points:
                    max_pts = max(room.player_points.values())
                    winners = [n for n, p in room.player_points.items() if p == max_pts]
                    winner_label = winners[0] if len(winners) == 1 else "Tasapeli"
                    def conclude_after_last_pair():
                        socketio.sleep(FINAL_PAIR_REVEAL_SECONDS)
                        conclude_round(winner_label, room)
                    socketio.start_background_task(conclude_after_last_pair)
                    return
                else:
                    print("[ERROR] Ei voittajaa, player_points on tyhjää.")
        else:
            debug(f"[DEBUG] Ei paria: {word1} vs {word2}")
            has_bot = any(info.get("is_bot") for info in room.players.values())
            clicker_is_human = not is_bot_player(clicker or {})
            if (is_solo(room) or (has_bot and clicker_is_human)):
                # Mistake only if both cards have been seen before
                if idx1 in room.solo_seen_cards and idx2 in room.solo_seen_cards:
                    room.solo_mistakes += 1
                room.solo_seen_cards.add(idx1)
                room.solo_seen_cards.add(idx2)
            indices_to_hide = list(room.revealed_cards)
            room.revealed_cards = []
            room.current_click_sid = None

            def hide_later():
                socketio.sleep(2)
                emit_to_room("hide_cards", {
                    "indices": indices_to_hide,
                    "solo_mistakes": room.solo_mistakes if is_solo(room) else None
                }, room_id=room.room_id)

            socketio.start_background_task(hide_later)
            if room.player_order:
                room.turn = (room.turn + 1) % len(room.player_order)

        next_turn_name = room.player_order[room.turn] if room.player_order else None
        debug(f"[DEBUG] Vuoro nyt: {next_turn_name}")
        emit_to_room("update_turn", {"turn": next_turn_name}, room_id=room.room_id)
        schedule_bot_turn_if_needed(room)


def ask_next_word(room):
    debug(f"[DEBUG] ask_next_word: pending_pair={room.pending_pair}, grid={len(room.grid_data)}, mode={room.game_mode}")
    if room.pending_pair >= 8:
        print("[INFO] Kaikki sanat annettu, peli voidaan aloittaa")
        deactivate_theme_selection(room)
        room.pending_player = None
        room.pending_pair = 0
        launch_grid_round(room)
        return

    if not is_solo(room) and len(room.player_order) < 2 and not queue_can_prepare_round_while_waiting(room):
        print("[WARNING] Pelaajia liian vähän, peli keskeytetään")
        emit_to_room("game_aborted", {"reason": "Toinen pelaaja poistui. Peli keskeytetty."}, room_id=room.room_id)
        return

    first_player = get_first_human_player_name(room) or (room.player_order[0] if room.player_order else None)
    room.pending_player = first_player

    if room.game_mode == "theme":
        if theme_selection_active(room):
            emit_theme_selection_state(room)
            return
        card_mode = room.card_mode or "image_word"
        if card_mode == "images":
            print(f"[INFO] Generoidaan teemasanat teemalle '{room.pending_theme}'")
            for p in room.players.values():
                p["in_waiting"] = False
            emit_to_room("theme_generation_started", {
                "theme": room.pending_theme,
                "pair": room.pending_pair + 1,
                "mode": "theme",
                "card_mode": card_mode,
                "starter_name": first_player,
                "phase": "drawing_cards",
                "progress_count": room.pending_pair,
                "total_pairs": 8
            }, room_id=room.room_id)
            socketio.sleep(0)
            if room.theme_generation_in_progress:
                debug("[DEBUG] Teemagenerointi jo käynnissä")
                return
            room.theme_generation_in_progress = True
            socketio.start_background_task(generate_theme_pair, room)
        else:
            lang_name = (SUPPORTED_LANGUAGES.get(room.target_language) or {}).get("en", room.target_language)
            print(f"[INFO] Generoidaan kielipeli ({lang_name}, {card_mode}) teemalle '{room.pending_theme}'")
            for p in room.players.values():
                p["in_waiting"] = False
            emit_to_room("theme_generation_started", {
                "theme": room.pending_theme,
                "pair": room.pending_pair + 1,
                "mode": "theme",
                "card_mode": card_mode,
                "starter_name": first_player,
                "phase": "drawing_cards",
                "progress_count": room.pending_pair,
                "total_pairs": 8
            }, room_id=room.room_id)
            socketio.sleep(0)
            if room.lang_generation_in_progress:
                debug("[DEBUG] Kielipelin generointi jo käynnissä")
                return
            room.lang_generation_in_progress = True
            socketio.start_background_task(generate_lang_learning_pairs, room.pending_theme, room)
        return

    print(f"[INFO] Pyydetään sana pelaajalta {first_player}, pari {room.pending_pair + 1}")
    for p in room.players.values():
        p["in_waiting"] = False
    emit_to_room("ask_for_word", {
        "player": first_player,
        "pair": room.pending_pair + 1
    }, room_id=room.room_id)


# ---------------------------------------------------------------------------
# Matchmaking queue
# ---------------------------------------------------------------------------

def join_matchmaking_queue(sid, username, reconnect_token, card_mode="image_word", target_language=""):
    mapped_room_id = get_room_id_for_reconnect_token(reconnect_token)
    mapped_room = rooms.get(mapped_room_id)
    if mapped_room:
        active_humans = [
            info for info in mapped_room.players.values()
            if not is_bot_player(info) and info.get("connected", False)
        ]
        if len(active_humans) >= 2:
            print(f"[INFO] Estetty jonoon liittyminen: {username} on jo matchatussa huoneessa {mapped_room_id}.")
            return
    for entry in matchmaking_queue:
        if entry["reconnect_token"] == reconnect_token:
            return
    matchmaking_queue.append({
        "sid": sid, "username": username, "reconnect_token": reconnect_token,
        "card_mode": card_mode or "image_word",
        "target_language": target_language or "",
    })
    print(f"[INFO] {username} liittyi jonoon (card_mode={card_mode}, lang={target_language}). Jonossa: {len(matchmaking_queue)}")
    socketio.emit("queue_status", {"position": len(matchmaking_queue), "waiting": len(matchmaking_queue)}, to=sid)
    try_match_from_queue()


def leave_matchmaking_queue(sid):
    global matchmaking_queue
    before = len(matchmaking_queue)
    matchmaking_queue = [e for e in matchmaking_queue if e["sid"] != sid]
    if len(matchmaking_queue) < before:
        print(f"[INFO] SID {sid} poistui jonosta.")


def leave_matchmaking_queue_by_token(reconnect_token):
    global matchmaking_queue
    if not reconnect_token:
        return
    before = len(matchmaking_queue)
    matchmaking_queue = [e for e in matchmaking_queue if e.get("reconnect_token") != reconnect_token]
    if len(matchmaking_queue) < before:
        print(f"[INFO] reconnect_token {reconnect_token} poistui jonosta.")


def try_match_from_queue():
    if len(matchmaking_queue) < 2:
        return
    # Find first compatible pair (same card_mode + target_language)
    for i in range(len(matchmaking_queue)):
        for j in range(i + 1, len(matchmaking_queue)):
            p1, p2 = matchmaking_queue[i], matchmaking_queue[j]
            if (p1.get("card_mode") == p2.get("card_mode") and
                    p1.get("target_language") == p2.get("target_language")):
                matchmaking_queue.pop(j)
                matchmaking_queue.pop(i)
                break
        else:
            continue
        break
    else:
        return  # No compatible pair found
    room_id = str(uuid.uuid4())[:8]
    room = create_room(room_id)
    room.play_mode = "queue"
    room.card_mode = p1.get("card_mode") or "image_word"
    tl = p1.get("target_language") or ""
    if tl:
        room.target_language = tl
    print(f"[INFO] Matchmaking: {p1['username']} vs {p2['username']} -> huone {room_id} (card_mode={room.card_mode}, lang={tl})")
    for entry in [p1, p2]:
        sid = entry["sid"]
        token = entry["reconnect_token"]
        username = entry["username"]
        # Remove player from any old room they were in before matchmaking
        old_room_id = player_room_index.get(token, DEFAULT_ROOM_ID)
        if old_room_id in rooms and old_room_id != room_id:
            old_room = rooms[old_room_id]
            old_room.players.pop(sid, None)
            try:
                leave_room(old_room_id, sid=sid)
            except Exception:
                pass
        room.players[sid] = {
            "username": username,
            "reconnect_token": token,
            "connected": True,
            "disconnected_at": None,
            "room_id": room_id
        }
        move_sid_to_room(sid, room_id)
        assign_reconnect_token_to_room(token, room_id)
    emit_to_room("match_found", {
        "room_id": room_id,
        "players": [p1["username"], p2["username"]]
    }, room_id=room_id)
    payload = build_lobby_payload(room)
    emit_to_room("player_joined", payload, room_id=room_id)


# ---------------------------------------------------------------------------
# Lobby / grid building
# ---------------------------------------------------------------------------

def build_lobby_payload(room):
    players_ordered = get_active_players_ordered(room)
    usernames = [v["username"] for v in players_ordered]
    infos = [{"username": v["username"], "reconnect_token": v.get("reconnect_token"), "in_waiting": v.get("in_waiting", False), "pref_card_mode": v.get("pref_card_mode"), "pref_target_language": v.get("pref_target_language")} for v in players_ordered]
    last_token = infos[-1]["reconnect_token"] if infos else None
    return {
        "room_id": room.room_id,
        "play_mode": room.play_mode,
        "card_mode": room.card_mode,
        "target_language": room.target_language,
        "bot_difficulty": room.bot_difficulty,
        "word_filter_mode": room.word_filter_mode,
        "players": usernames,
        "players_info": infos,
        "last_joined_token": last_token,
        "queue_round_prepared": room.queue_round_prepared,
    }


def get_available_rooms():
    """Rooms with exactly 1 waiting connected human player (available for direct join)."""
    result = []
    for room_id, room in rooms.items():
        if room.status != "waiting":
            continue
        if not room.queue_round_prepared or len(room.grid_data) < 16:
            continue
        human_players = [
            (sid, info) for sid, info in room.players.items()
            if not is_bot_player(info) and info.get("connected", False)
        ]
        if len(human_players) != 1:
            continue
        _, info = human_players[0]
        if not info.get("pref_ready", False):
            continue
        result.append({
            "room_id": room_id,
            "username": info.get("username"),
            "card_mode": room.card_mode or info.get("pref_card_mode") or "image_word",
            "target_language": room.target_language or "",
        })
    return result


def broadcast_lobby_browser():
    socketio.emit("lobby_browser_updated", {"rooms": get_available_rooms()})


def generate_grid():
    # Placeholder – generates random tiles for pre-game preview
    pass


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/waiting")
def waiting():
    return render_template("waiting_room.html")


@app.route("/game")
def game():
    return render_template("game.html")


@app.route("/summary")
def summary():
    return render_template("summary.html")


@app.route("/release-notes")
def release_notes():
    return render_template("release_notes.html")


@app.route("/leaderboard")
def leaderboard():
    conn = _get_db()
    db_available = conn is not None
    # sections: list of {play, card_mode, target_language, rows}
    sections = []
    multi_top = []
    if conn:
        try:
            with conn.cursor() as cur:
                bot_name = BOT_USERNAME
                for play_mode, query, args in [
                    ("solo",
                     """SELECT card_mode, COALESCE(target_language,'') AS tl,
                               username, time_secs, mistakes, total_time
                        FROM results
                        WHERE play_mode = 'solo' AND game_mode != 'random' AND total_time IS NOT NULL
                          AND username != %s
                        ORDER BY card_mode, tl, total_time ASC""",
                     (bot_name,)),
                    ("bot",
                     """SELECT card_mode, COALESCE(target_language,'') AS tl,
                               COALESCE(bot_difficulty, 'easy') AS bd,
                               username, pairs_found, mistakes, time_secs
                        FROM results
                        WHERE play_mode = 'bot' AND pairs_found IS NOT NULL
                          AND username != %s
                        ORDER BY card_mode, tl,
                                 CASE COALESCE(bot_difficulty, 'easy')
                                   WHEN 'hard' THEN 0
                                   WHEN 'medium' THEN 1
                                   ELSE 2
                                 END ASC,
                                 pairs_found DESC,
                                 mistakes ASC NULLS LAST,
                                 time_secs ASC NULLS LAST""",
                     (bot_name,)),
                    ("random",
                     """SELECT card_mode, COALESCE(target_language,'') AS tl,
                               username, time_secs, mistakes, total_time
                        FROM results
                        WHERE game_mode = 'random' AND play_mode != 'bot' AND total_time IS NOT NULL
                          AND username != %s
                        ORDER BY card_mode, tl, total_time ASC""",
                     (bot_name,)),
                ]:
                    cur.execute(query, args)
                    rows_all = cur.fetchall()
                    # group by (card_mode, target_language), keep top 10 each
                    grouped = {}
                    for row in rows_all:
                        cm = row[0] or "images"
                        tl = row[1] or ""
                        key = (cm, tl)
                        if key not in grouped:
                            grouped[key] = []
                        if len(grouped[key]) < 10:
                            grouped[key].append(row[2:])  # drop grouping prefix
                    # define canonical order
                    cm_order = ["images", "image_word", "words"]
                    seen = set()
                    for cm in cm_order:
                        for key in sorted(grouped.keys()):
                            if key[0] == cm and key not in seen:
                                seen.add(key)
                                sections.append({
                                    "play": play_mode,
                                    "card_mode": key[0],
                                    "target_language": key[1],
                                    "rows": grouped[key]
                                })
                cur.execute("""
                    SELECT username,
                           SUM(CASE
                                 WHEN COALESCE(round_result, CASE WHEN round_won = 1 THEN 'win' ELSE 'loss' END) = 'win'
                                 THEN 1 ELSE 0
                               END) AS wins,
                           SUM(CASE WHEN round_result = 'tie' THEN 1 ELSE 0 END) AS ties,
                           COUNT(*) AS rounds
                    FROM results
                    WHERE play_mode = 'multiplayer' AND round_won IS NOT NULL
                      AND username != %s
                    GROUP BY username
                    ORDER BY wins DESC, ties DESC, rounds ASC
                    LIMIT 10
                """, (bot_name,))
                multi_top = cur.fetchall()
        except Exception as e:
            print(f"[DB] Leaderboard query error: {e}")
        finally:
            conn.close()

    return render_template("leaderboard.html",
                           sections=sections,
                           multi_top=multi_top,
                           db_available=db_available,
                           SOLO_PENALTY=SOLO_PENALTY_PER_MISTAKE)


# ---------------------------------------------------------------------------
# Socket events – lobby / join
# ---------------------------------------------------------------------------

@socketio.on("join")
def on_join(data):
    username = data["username"]
    sid = request.sid
    reconnect_token = data.get("reconnect_token")
    wants_bot = bool((data or {}).get("bot_mode"))
    wants_solo = bool((data or {}).get("solo_mode"))
    requested_play_mode = "solo" if wants_solo else ("bot" if wants_bot else "queue")
    preferred_card_mode = str((data or {}).get("card_mode") or "image_word").strip().lower()
    if preferred_card_mode not in {"images", "image_word", "words"}:
        preferred_card_mode = "image_word"
    preferred_target_language = str((data or {}).get("target_language") or "").strip().lower()
    if preferred_card_mode == "images":
        preferred_target_language = ""

    if not reconnect_token:
        print(f"[WARNING] HYLÄTTY join ilman reconnect_tokenia: {username}")
        return

    existing_player_snapshot = None
    for existing_room in rooms.values():
        for info in existing_room.players.values():
            if info.get("reconnect_token") == reconnect_token:
                existing_player_snapshot = dict(info)
                break
        if existing_player_snapshot:
            break

    room_id = get_room_id_for_reconnect_token(reconnect_token)
    existing_room = get_room(room_id)

    # Check if the token already exists in the resolved room (reconnect case — always reuse).
    existing_room_belongs_to_self = (
        room_id != DEFAULT_ROOM_ID and
        room_id in rooms and
        any(
            info.get("reconnect_token") == reconnect_token
            for info in rooms[room_id].players.values()
        )
    )

    if existing_room_belongs_to_self and room_id in rooms:
        existing_room = rooms[room_id]
        existing_has_bot = any(is_bot_player(info) for info in existing_room.players.values())
        should_rotate_private_room = (
            existing_room.play_mode != requested_play_mode
            or (requested_play_mode == "solo" and existing_has_bot)
        )
        if should_rotate_private_room:
            prefix = "solo" if wants_solo else ("bot" if wants_bot else "queue")
            room_id = f"{prefix}-{str(uuid.uuid4())[:8]}"
            assign_reconnect_token_to_room(reconnect_token, room_id)
            existing_room_belongs_to_self = True  # new room belongs to us

    if not wants_bot and not wants_solo and not existing_room_belongs_to_self:
        # Each queue player gets their own private room so others can see and join them
        room_id = f"queue-{str(uuid.uuid4())[:8]}"
        assign_reconnect_token_to_room(reconnect_token, room_id)
    if (wants_solo or wants_bot) and not existing_room_belongs_to_self and (
        room_id == DEFAULT_ROOM_ID or
        (room_id in rooms and get_effective_human_player_items(rooms[room_id]))
    ):
        prefix = "solo" if wants_solo else "bot"
        room_id = f"{prefix}-{str(uuid.uuid4())[:8]}"
        assign_reconnect_token_to_room(reconnect_token, room_id)

    room = get_room(room_id)

    # Remove stale entries for this player from any previously tracked room/socket room.
    remove_player_memberships(reconnect_token=reconnect_token, sid=sid)

    active_human_players = get_effective_human_player_items(room)

    if wants_solo:
        room.play_mode = "solo"
    elif wants_bot and not get_effective_human_player_items(room):
        room.play_mode = "bot"
        ensure_bot_opponent(room)
    else:
        room.play_mode = "queue"

    if get_effective_player_count(room) >= MAX_PLAYERS:
        print(f"[INFO] Hylätty liittyminen, peli on täynnä: {username}")
        emit("join_rejected", {"reason": "Peli on täynnä. Odota seuraavaa erää tai avaa uusi peli."})
        return

    preserved_waiting_state = existing_player_snapshot.get("in_waiting") if existing_player_snapshot else None
    preserved_ready_state = existing_player_snapshot.get("pref_ready") if existing_player_snapshot else None
    room.players[sid] = {
        "username": username,
        "reconnect_token": reconnect_token,
        "connected": True,
        "disconnected_at": None,
        "room_id": room_id,
        "in_waiting": preserved_waiting_state if preserved_waiting_state is not None else True,
        "pref_card_mode": (existing_player_snapshot or {}).get("pref_card_mode") or preferred_card_mode,
        "pref_target_language": (existing_player_snapshot or {}).get("pref_target_language") if existing_player_snapshot and existing_player_snapshot.get("pref_target_language") is not None else preferred_target_language,
        "pref_ready": preserved_ready_state if preserved_ready_state is not None else (requested_play_mode != "queue" or len(active_human_players) > 0),
    }
    move_sid_to_room(sid, room_id)
    assign_reconnect_token_to_room(reconnect_token, room_id)

    if requested_play_mode == "queue" and len(active_human_players) == 0:
        room.card_mode = room.players[sid].get("pref_card_mode") or preferred_card_mode
        room.target_language = room.players[sid].get("pref_target_language") or preferred_target_language

    print(f"[INFO] {username} liittyi peliin (huone: {room_id}).")
    payload = build_lobby_payload(room)
    payload["username"] = username
    emit_to_room("player_joined", payload, room_id=room_id)
    if theme_selection_active(room):
        emit_theme_selection_state(room, sid=sid)
    broadcast_lobby_browser()


@socketio.on("request_lobby_state")
def handle_request_lobby_state():
    room = get_room_for_sid(request.sid)
    emit("lobby_state", build_lobby_payload(room))
    if theme_selection_active(room):
        emit("theme_selection_updated", build_theme_selection_payload(room))


@socketio.on("leave_game")
def handle_leave_game(data=None):
    data = data or {}
    sid, player_info = resolve_player_for_event(data)
    if not player_info:
        return {"ok": False}
    room = resolve_room_for_event(data, player_info)

    username = player_info.get("username", "Unknown")
    reconnect_token = player_info.get("reconnect_token")

    for existing_sid, info in list(room.players.items()):
        if existing_sid == sid or (reconnect_token and info.get("reconnect_token") == reconnect_token):
            clear_reconnect_token_room(info.get("reconnect_token"))
            _sid_to_room_id.pop(existing_sid, None)
            del room.players[existing_sid]
    try:
        leave_room(room.room_id)
    except Exception:
        pass

    print(f"[INFO] {username} poistui pelistä käyttäjän pyynnöstä.")
    payload = build_lobby_payload(room)
    payload["username"] = username
    emit_to_room("player_joined", payload, room_id=room.room_id)
    broadcast_lobby_browser()

    if not get_effective_human_player_items(room):
        remove_bot_players(room)

    if not is_solo(room) and get_effective_player_count(room) < 2 and (
            theme_selection_active(room) or room.grid_data or room.pending_pair > 0):
        print("[INFO] Pelaaja poistui kesken erän – keskeytetään")
        emit_to_room("game_aborted", {"reason": "Toinen pelaaja poistui. Peli keskeytetty."}, room_id=room.room_id)
        clear_round_runtime(room)
        reset_pending_state(room)

    if len(room.players) == 0:
        print("[INFO] Kaikki pelaajat poistuneet – nollataan pelitila")
        clear_round_runtime(room)
        reset_pending_state(room)

    return {"ok": True}


@socketio.on("start_game_clicked")
def handle_start_game():
    room = get_room_for_sid(request.sid)
    if get_effective_player_count(room) == MAX_PLAYERS:
        print("[INFO] Molemmat pelaajat liittyneet, aloitetaan peli")
        emit_to_room("start_game", room_id=room.room_id)
    else:
        print("[WARNING] Pelaajia ei ole tarpeeksi")


@socketio.on("request_grid")
def handle_grid_request(data=None):
    _, player_info = resolve_player_for_event(data)
    room = resolve_room_for_event(data, player_info)
    token_short = str(data.get("reconnect_token", ""))[:8] if data else "none"
    grid_size = len(room.grid_data) if room and room.grid_data else 0
    player_found = "found" if player_info else "missing"
    room_id = room.room_id if room else "NONE"
    print(f"[INFO] request_grid: token={token_short}, room={room_id}, grid_size={grid_size}, solo={is_solo(room) if room else 'N/A'}, player={player_found}")

    if room.queue_round_prepared and queue_can_prepare_round_while_waiting(room):
        emit("queue_round_prepared", build_lobby_payload(room))
        emit("no_grid", {"reason": "queue_round_prepared"})
        return

    if not solo_or_enough_players(room):
        print("[WARNING] Ei tarpeeksi pelaajia ruudukon palauttamiseen.")
        emit("no_grid", {"reason": "Pelaajia liian vähän"})
        return

    if not room.grid_data or len(room.grid_data) < 16:
        debug("[DEBUG] Ruudukko ei ole valmis")
        emit("no_grid", {"reason": "Grid ei valmis"})
        if theme_selection_active(room):
            emit("theme_selection_updated", build_theme_selection_payload(room))
            return
        if room.pending_pair > 0 or room.pending_player:
            try:
                if room.game_mode == "theme":
                    emit_to_room("theme_generation_started", {
                        "theme": room.pending_theme,
                        "pair": room.pending_pair + 1,
                        "mode": room.game_mode,
                        "starter_name": room.pending_player or (room.player_order[0] if room.player_order else None),
                        "progress_count": room.pending_pair,
                        "total_pairs": 8
                    }, room_id=room.room_id)
                    socketio.sleep(0)
                    return
                if len(room.player_order) < 1:
                    room.player_order[:] = [v["username"] for v in get_effective_players_ordered(room)]
                target_player = room.pending_player or get_first_human_player_name(room)
                if target_player and room.pending_pair < 8:
                    debug(f"[DEBUG] request_grid: toistetaan ask_for_word → {target_player}, pari {room.pending_pair + 1}")
                    emit_to_room("ask_for_word", {"player": target_player, "pair": room.pending_pair + 1}, room_id=room.room_id)
            except Exception as e:
                print(f"[WARNING] request_grid fallback epäonnistui: {e}")
        return

    current_player_name = (
        room.player_order[room.turn] if room.player_order and 0 <= room.turn < len(room.player_order) else None
    )
    print(f"[INFO] request_grid: lähetetään init_grid → room={room.room_id}, cards={len(room.grid_data)}, card_mode={room.card_mode}")
    emit("init_grid", {
        "cards": room.grid_data,
        "turn": current_player_name,
        "players": room.player_order,
        "matched": list(room.matched_indices),
        "revealed": room.revealed_cards,
        "points": room.player_points,
        "solo": is_solo(room),
        "play_mode": room.play_mode,
        "game_mode": room.game_mode,
        "card_mode": room.card_mode,
        "image_pair_tracker": build_image_pair_tracker_entries(room),
        "target_language": room.target_language,
        "native_language": room.native_language,
        "bot_difficulty": room.bot_difficulty,
        "word_filter_mode": room.word_filter_mode,
        "room_id": room.room_id,
    })
    schedule_bot_turn_if_needed(room)


@socketio.on("ready_for_game")
def handle_ready_for_game():
    room = get_room_for_sid(request.sid)
    if room.grid_data and len(room.grid_data) >= 16:
        current_player_name = (
            room.player_order[room.turn] if room.player_order and 0 <= room.turn < len(room.player_order) else None
        )
        emit("init_grid", {
            "cards": room.grid_data,
            "turn": current_player_name,
            "players": room.player_order,
            "play_mode": room.play_mode,
            "game_mode": room.game_mode,
            "card_mode": room.card_mode,
            "image_pair_tracker": build_image_pair_tracker_entries(room),
            "target_language": room.target_language,
            "native_language": room.native_language,
            "bot_difficulty": room.bot_difficulty,
            "word_filter_mode": room.word_filter_mode,
        })


# ---------------------------------------------------------------------------
# Socket events – game setup
# ---------------------------------------------------------------------------

@socketio.on("start_custom_game")
def handle_start_custom_game(data=None):
    data = data or {}
    room = get_room_for_sid(request.sid)
    queue_prepare_mode = queue_can_prepare_round_while_waiting(room)
    print(f"[INFO] Uusi erä käynnistetään. mode={data.get('mode', 'manual')}, players={get_effective_player_count(room)}")

    current_tokens = set(v["reconnect_token"] for v in get_effective_players_ordered(room))
    if room.grid_data and current_tokens != room.last_tokens:
        print("[INFO] Pelaajien reconnect-tokenit vaihtuneet – nollataan")
        clear_round_runtime(room)
        reset_pending_state(room)

    room.last_tokens = set(v["reconnect_token"] for v in get_effective_players_ordered(room))
    room.player_order = [v["username"] for v in get_effective_players_ordered(room)]

    if room.grid_data:
        if queue_prepare_mode and room.status == "waiting":
            print("[INFO] Korvataan odotushuoneen valmis kierros uusilla valinnoilla.")
            clear_round_runtime(room)
            reset_pending_state(room)
        else:
            print("[WARNING] Uuden erän pyyntö hylätty: peli on jo käynnissä")
            return

    mode = str(data.get("mode", "manual")).strip().lower()
    theme = str(data.get("theme", "")).strip()
    ui_language = str(data.get("ui_language", "")).strip().lower()
    card_mode = str(data.get("card_mode", "")).strip().lower()
    bot_difficulty = str(data.get("bot_difficulty", room.bot_difficulty or "easy")).strip().lower()
    word_filter_mode = str(data.get("word_filter_mode", room.word_filter_mode or "clear")).strip().lower()
    # "spanish" / "language" legacy: word_source=theme + card_mode=image_word
    if mode in {"spanish", "language"}:
        mode = "theme"
        if not card_mode:
            card_mode = "image_word"
        data.setdefault("target_language", "es")
    if mode not in {"manual", "theme", "random"}:
        mode = "manual"
    if card_mode not in {"images", "image_word", "words"}:
        card_mode = "image_word"
    all_ui_langs = {"fi", "en"} | set(SUPPORTED_LANGUAGES.keys())
    room.ui_language = ui_language if ui_language in all_ui_langs else "en"
    room.native_language = room.ui_language
    room.card_mode = card_mode
    if room.play_mode == "bot":
        room.bot_difficulty = bot_difficulty if bot_difficulty in BOT_MEMORY_USE_PROBABILITY else "easy"
    else:
        room.bot_difficulty = "easy"
    room.word_filter_mode = word_filter_mode if word_filter_mode in {"clear", "broad"} else "clear"
    if card_mode in {"image_word", "words"}:
        tl = str(data.get("target_language", "es")).strip().lower()
        room.target_language = tl if tl in SUPPORTED_LANGUAGES else "es"
    else:
        room.target_language = ""
    if mode == "theme" and not theme:
        emit_to_room("game_setup_error", {"reason": "Teema puuttuu."}, room_id=room.room_id)
        return

    print(f"[INFO] Aloitetaan sanojen keruu — word_source={mode}, card_mode={card_mode}")
    room.pending_pair = 0
    room.pending_player = None
    room.grid_data.clear()
    room.bot_memory = {}
    room.game_mode = mode
    room.pending_theme = theme if mode == "theme" else None
    room.pending_search_theme = translate_theme_to_english(theme, room.ui_language) if mode == "theme" else None
    room.theme_candidates = []
    room.theme_rejected_words = set()
    room.status = "setup"
    room.queue_round_prepared = False
    if queue_prepare_mode:
        broadcast_lobby_browser()
        starter = room.players.get(request.sid)
        if starter:
            starter["in_waiting"] = False
            starter["pref_card_mode"] = room.card_mode
            starter["pref_target_language"] = room.target_language
            starter["pref_ready"] = False

    if room.game_mode == "theme":
        starter_name = room.players.get(request.sid, {}).get("username")
        emit_to_room("theme_generation_started", {
            "theme": room.pending_theme,
            "pair": room.pending_pair + 1,
            "mode": room.game_mode,
            "card_mode": room.card_mode,
            "starter_name": starter_name,
            "phase": "finding_words",
            "progress_count": room.pending_pair,
            "total_pairs": 8
        }, room_id=room.room_id)
        socketio.sleep(0)
        prepare_theme_selection(starter_name, room)
        return
    if room.game_mode == "random":
        def run_random_game():
            # Fetch a larger pool so we can skip words that fail without showing errors
            candidates = fetch_random_game_words(target=24, room=room)
            print(f"[INFO] Satunnaissana-kandidaatit: {', '.join(candidates)}")

            # UI signal: drawing phase starts — emit before pre-warming so user sees progress sooner
            emit_to_room("random_drawing_started", {}, room_id=room.room_id)

            # Pre-warm translation cache AND Pixabay cache in parallel
            if room.card_mode in {"image_word", "words"}:
                target_lang = room.target_language or "es"
                native_lang = room.native_language or "fi"
                def _warm(word):
                    translate_word(word, "en", target_lang)
                    if native_lang != "en":
                        translate_word(word, "en", native_lang)
                    _prefetch_pixabay_cache(word)
                jobs = [gevent.spawn(_warm, w) for w in candidates]
                gevent.joinall(jobs, timeout=30)
                print(f"[INFO] Käännökset ja kuvat esivalmisteltu ({len(candidates)} sanaa)")
            else:
                pix_jobs = [gevent.spawn(_prefetch_pixabay_cache, w) for w in candidates]
                gevent.joinall(pix_jobs, timeout=30)

            pair_index = 0
            for word in candidates:
                if pair_index >= 8:
                    break
                if room.card_mode in {"image_word", "words"}:
                    pair = append_selected_lang_pair(word, pair_index, room, source_lang="en")
                    if not pair:
                        print(f"[INFO] Satunnainen sana ohitettu (käännös/kuva): '{word}'")
                        continue
                    append_lang_learning_pair_to_grid(pair, room)
                else:
                    if not append_word_images_to_grid(word, room):
                        print(f"[INFO] Satunnainen sana ohitettu (Pixabay): '{word}'")
                        continue
                pair_index += 1
                room.pending_pair = pair_index
                emit_to_room("word_accepted", {"word": word, "pair": pair_index, "total_pairs": 8}, room_id=room.room_id)
                socketio.sleep(0)
            if pair_index < 8:
                emit_to_room("game_setup_error", {"reason": "Satunnaisia sanoja ei löytynyt tarpeeksi. Yritä uudelleen."}, room_id=room.room_id)
                room.grid_data.clear()
                reset_pending_state(room)
                return
            print(f"[INFO] Kaikki {pair_index} paria valmis, käynnistetään kierros")
            launch_grid_round(room)
        socketio.start_background_task(run_random_game)
        return
    ask_next_word(room)


# ---------------------------------------------------------------------------
# Socket events – theme selection
# ---------------------------------------------------------------------------

@socketio.on("select_theme_word")
def handle_select_theme_word(data):
    room = get_room_for_sid(request.sid)
    if room.game_mode != "theme" or not theme_selection_active(room):
        emit("theme_selection_failed", {"reason": "selection_inactive"})
        return

    _, player_info = resolve_player_for_event(data)
    if not player_info:
        emit("theme_selection_failed", {"reason": "player_missing"})
        return
    sync_theme_selection_players(room)

    username = player_info["username"]
    word = normalize_candidate_word((data or {}).get("word"))
    replace_word = normalize_candidate_word((data or {}).get("replace_word"))
    if not word:
        emit("theme_selection_failed", {"reason": "invalid_word"})
        return

    tss = room.theme_selection_state
    counts = tss.get("counts", {})
    ready = tss.get("ready", {})
    selected_words = tss.get("selected_words", [])
    rejected_words = tss.get("rejected_words", [])
    candidates = tss.get("candidates", [])
    swap_limit = int(tss.get("swap_limit", 4))

    if counts.get(username, 0) >= swap_limit:
        emit("theme_selection_failed", {"reason": "quota_full"})
        return
    if not replace_word:
        emit("theme_selection_failed", {"reason": "replace_missing"})
        return
    if word not in candidates:
        emit("theme_selection_failed", {"reason": "unknown_word"})
        return
    selected_index = next(
        (i for i, item in enumerate(selected_words) if item.get("word") == replace_word), -1
    )
    if selected_index < 0:
        emit("theme_selection_failed", {"reason": "replace_missing"})
        return
    if any(item.get("word") == word for item in selected_words) or word in rejected_words:
        emit("theme_selection_failed", {"reason": "word_unavailable"})
        return

    mode_label = "teemasanan" if room.game_mode == "theme" else "kielipelin sanan"
    print(f"[INFO] {username} vaihtaa {mode_label}n '{replace_word}' -> '{word}'")
    try:
        selection_ok = build_pair_entry_for_mode(word, selected_index, room)
    except PixabayConfigError as e:
        abort_round_due_to_pixabay_error(str(e), room)
        return
    if not selection_ok:
        print(f"[INFO] Sana '{word}' hylättiin")
        rejected_words.append(word)
        room.theme_rejected_words.add(word)
        emit_to_room("theme_selection_updated", build_theme_selection_payload(room, message=f"word_rejected:{word}"), room_id=room.room_id)
        emit("theme_selection_failed", {"reason": "image_missing", "word": word})
        return

    previous_item = selected_words[selected_index]
    previous_word = previous_item.get("word")
    next_display_word = get_theme_display_word(word, selection_ok, room)
    next_display_key = normalize_display_label(next_display_word)
    for i, item in enumerate(selected_words):
        if i == selected_index:
            continue
        if normalize_display_label(item.get("display_word", item.get("word"))) == next_display_key:
            emit("theme_selection_failed", {"reason": "word_unavailable"})
            return

    selected_words[selected_index] = {
        "word": word,
        "display_word": next_display_word,
        "chosen_by": username,
        "entry": selection_ok
    }
    counts[username] = counts.get(username, 0) + 1
    for player_name in list(ready.keys()):
        ready[player_name] = is_bot_player(player_name, room)
    ready[username] = True
    tss["candidates"] = [c for c in candidates if c != word]
    if previous_word and previous_word not in rejected_words and previous_word not in tss["candidates"]:
        tss["candidates"].append(previous_word)
    candidate_labels = tss.setdefault("candidate_labels", {})
    candidate_labels.pop(word, None)
    if previous_word and previous_word not in rejected_words:
        candidate_labels[previous_word] = (
            previous_item.get("display_word") or get_theme_display_word(previous_word, previous_item.get("entry"), room)
        )
    emit_theme_selection_state(room, message=f"word_swapped:{previous_word}:{word}")
    if room.player_order and all(ready.get(name, False) for name in room.player_order):
        print(f"[INFO] Kaikki valmiina vaihdon jälkeen – aloitetaan {room.game_mode}-erä")
        finalize_theme_selection(room)


@socketio.on("set_theme_ready")
def handle_set_theme_ready(data):
    room = get_room_for_sid(request.sid)
    if room.game_mode != "theme" or not theme_selection_active(room):
        emit("theme_selection_failed", {"reason": "selection_inactive"})
        return

    _, player_info = resolve_player_for_event(data)
    if not player_info:
        emit("theme_selection_failed", {"reason": "player_missing"})
        return
    sync_theme_selection_players(room)

    username = player_info["username"]
    ready = room.theme_selection_state.get("ready", {})
    ready[username] = bool((data or {}).get("ready", True))
    emit_theme_selection_state(room, message=f"ready:{username}" if ready[username] else f"unready:{username}")
    if room.player_order and all(ready.get(name, False) for name in room.player_order):
        print(f"[INFO] Kaikki valmiina – aloitetaan {room.game_mode}-erä teemalla '{room.pending_theme}'")
        finalize_theme_selection(room)


# ---------------------------------------------------------------------------
# Socket events – word input
# ---------------------------------------------------------------------------

@socketio.on("word_given")
def handle_word_given(data):
    room = get_room_for_sid(request.sid)
    if room.pending_pair >= 8:
        debug(f"[DEBUG] word_given hylätty, kaikki parit jo annettu (pending_pair={room.pending_pair})")
        return
    sender_name = (room.players.get(request.sid) or {}).get("username")
    expected_player = room.pending_player or (room.player_order[0] if room.player_order else None)
    if expected_player and sender_name != expected_player:
        debug(f"[DEBUG] word_given hylätty väärältä pelaajalta: {sender_name}, odotettu: {expected_player}")
        return
    word = normalize_candidate_word(data["word"])
    if not word:
        print("[WARNING] Käyttäjän sana ei kelpaa, pyydetään uusi sana")
        emit_to_room("word_failed", {"player": expected_player, "pair": room.pending_pair + 1, "reason": "invalid_word"}, room_id=room.room_id)
        return
    pair_index = room.pending_pair
    print(f"[INFO] Vastaanotettu sana '{word}' parille {pair_index + 1}")

    # Language manual mode: translate and build lang pair instead of image-only
    if room.game_mode == "manual" and room.card_mode in {"image_word", "words"}:
        try:
            pair = append_selected_lang_pair(word, pair_index, room, source_lang=room.ui_language)
        except PixabayConfigError as e:
            abort_round_due_to_pixabay_error(str(e), room)
            return
        if pair:
            append_lang_learning_pair_to_grid(pair, room)
            room.pending_pair += 1
            ask_next_word(room)
        else:
            print(f"[WARNING] Käännös tai kuva epäonnistui sanalle '{word}'")
            emit_to_room("word_failed", {"player": expected_player, "pair": room.pending_pair + 1}, room_id=room.room_id)
        return

    try:
        image_append_ok = append_word_images_to_grid(word, room)
    except PixabayConfigError as e:
        abort_round_due_to_pixabay_error(str(e), room)
        return
    if image_append_ok:
        room.pending_pair += 1
        ask_next_word(room)
    else:
        print(f"[WARNING] Pixabay ei löytänyt kuvia sanalle '{word}'")
        emit_to_room("word_failed", {"player": expected_player, "pair": room.pending_pair + 1}, room_id=room.room_id)


@socketio.on("ask_for_word")
def handle_client_request_ask_for_word(data):
    room = get_room_for_sid(request.sid)
    target_player = data.get("player")
    pair = int(data.get("pair", 0))
    debug(f"[DEBUG] Client pyysi ask_for_word: player={target_player}, pair={pair}")
    socketio.sleep(0.3)
    try:
        emit_to_room("ask_for_word", {"player": target_player, "pair": pair}, room_id=room.room_id)
    except Exception as e:
        print(f"[ERROR] ask_for_word uudelleenlähetys epäonnistui: {e}")


# ---------------------------------------------------------------------------
# Socket events – game play
# ---------------------------------------------------------------------------

@socketio.on("card_clicked")
def handle_card_click(data):
    index = data["index"]
    resolved_sid, clicker = resolve_player_for_event(data)
    room = resolve_room_for_event(data, clicker)
    process_card_click(index, resolved_sid, clicker, room)


@socketio.on("surrender_round")
def handle_surrender_round(data=None):
    _, player_info = resolve_player_for_event(data)
    if not player_info:
        emit("round_surrender_failed", {"reason": "player_missing"})
        return

    room = resolve_room_for_event(data, player_info)
    if not room.grid_data or not solo_or_enough_players(room):
        emit("round_surrender_failed", {"reason": "round_not_active"})
        return

    surrendering_player = player_info["username"]

    if is_solo(room):
        print(f"[INFO] {surrendering_player} keskeytti yksinpelin.")
        room.current_click_sid = None
        conclude_round(None, room, surrendered_by=surrendering_player)
        return

    opponents = [name for name in room.player_order if name != surrendering_player]
    if not opponents:
        emit("round_surrender_failed", {"reason": "opponent_missing"})
        return

    winner = opponents[0]
    print(f"[INFO] {surrendering_player} luovutti. Voittaja: {winner}")
    room.current_click_sid = None
    conclude_round(winner, room, surrendered_by=surrendering_player)


# ---------------------------------------------------------------------------
# Socket events – summary / rematch
# ---------------------------------------------------------------------------

def _broadcast_rematch_status(room):
    emit_to_room("rematch_status", {
        "votes": list(room.rematch_votes),
        "players": list(room.summary_sids.keys()),
    }, room_id=room.room_id)


@socketio.on("join_summary")
def on_join_summary(data):
    reconnect_token = data.get("reconnect_token")
    username = data.get("username")
    if not reconnect_token or not username:
        return
    room_id = get_room_id_for_reconnect_token(reconnect_token)
    room = rooms.get(room_id)
    if not room or room.status != "results":
        emit("summary_error", {"reason": "room_not_found"})
        return
    sid = request.sid
    _sid_to_room_id[sid] = room_id
    try:
        join_room(room_id)
    except Exception:
        pass
    room.summary_sids[username] = sid
    _broadcast_rematch_status(room)


@socketio.on("want_rematch")
def on_want_rematch(data):
    reconnect_token = data.get("reconnect_token")
    username = data.get("username")
    if not reconnect_token or not username:
        return
    room_id = get_room_id_for_reconnect_token(reconnect_token)
    room = rooms.get(room_id)
    if not room or room.status != "results":
        return
    room.rematch_votes.add(username)
    present = set(room.summary_sids.keys())
    _broadcast_rematch_status(room)
    if present and room.rematch_votes >= present:
        # All present players want rematch — reset and let them return to waiting room
        room.rematch_votes.clear()
        room.summary_sids.clear()
        room.status = "waiting"
        emit_to_room("rematch_ready", {}, room_id=room_id)


@socketio.on("leave_summary")
def on_leave_summary(data):
    reconnect_token = data.get("reconnect_token")
    username = data.get("username")
    if not reconnect_token or not username:
        return
    room_id = get_room_id_for_reconnect_token(reconnect_token)
    room = rooms.get(room_id)
    if not room:
        return
    sid = request.sid
    room.summary_sids.pop(username, None)
    room.rematch_votes.discard(username)
    _sid_to_room_id.pop(sid, None)
    try:
        leave_room(room_id)
    except Exception:
        pass
    _broadcast_rematch_status(room)


# ---------------------------------------------------------------------------
# Socket events – connect / disconnect
# ---------------------------------------------------------------------------

@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    room = get_room_for_sid(sid)
    player_info = room.players.get(sid)

    if not player_info:
        debug(f"[DEBUG] Tuntematon SID {sid} poistui pelistä")
        return

    username = player_info["username"]
    reconnect_token = player_info.get("reconnect_token")
    player_info["connected"] = False
    player_info["disconnected_at"] = time.monotonic()
    leave_matchmaking_queue(sid)
    # Clean up summary state if player disconnects from summary
    if username in room.summary_sids and room.summary_sids.get(username) == sid:
        room.summary_sids.pop(username, None)
        room.rematch_votes.discard(username)
        _broadcast_rematch_status(room)
    try:
        leave_room(room.room_id)
    except Exception:
        pass

    print(f"[INFO] {username} poistui, odotetaan reconnectia ({RECONNECT_GRACE_SECONDS} s)...")
    payload = build_lobby_payload(room)
    payload["username"] = username
    emit_to_room("player_joined", payload, room_id=room.room_id)

    def remove_later(sid_to_remove, uname, expected_token, r):
        gevent.sleep(RECONNECT_GRACE_SECONDS)
        if sid_to_remove not in r.players:
            return
        current_token = r.players[sid_to_remove].get("reconnect_token")
        if current_token != expected_token:
            print(f"[INFO] {uname} reconnectasi uudella SID:llä, vanhaa ei poisteta")
            return
        print(f"[INFO] {uname} poistetaan pelaajalistasta (ei reconnectia)")
        clear_reconnect_token_room(r.players[sid_to_remove].get("reconnect_token"))
        _sid_to_room_id.pop(sid_to_remove, None)
        del r.players[sid_to_remove]

        if not get_effective_human_player_items(r):
            remove_bot_players(r)
        payload2 = build_lobby_payload(r)
        payload2["username"] = uname
        emit_to_room("player_joined", payload2, room_id=r.room_id)
        broadcast_lobby_browser()

        if not is_solo(r) and get_effective_player_count(r) < 2 and (
                theme_selection_active(r) or r.grid_data or r.pending_pair > 0):
            print("[INFO] Pelaajia liian vähän kesken erän – keskeytetään")
            emit_to_room("game_aborted", {"reason": "Toinen pelaaja poistui. Peli keskeytetty."}, room_id=r.room_id)
            r.grid_data.clear()
            r.revealed_cards.clear()
            r.matched_indices.clear()
            r.turn = 0
            r.player_points.clear()
            reset_pending_state(r)
            return

        if len(r.players) == 0:
            print("[INFO] Kaikki pelaajat poistuneet – nollataan pelitila")
            r.grid_data.clear()
            r.revealed_cards.clear()
            r.matched_indices.clear()
            r.turn = 0
            r.player_points.clear()
            reset_pending_state(r)

    socketio.start_background_task(remove_later, sid, username, reconnect_token, room)


# ---------------------------------------------------------------------------
# Socket events – matchmaking queue
# ---------------------------------------------------------------------------

@socketio.on("join_queue")
def handle_join_queue(data=None):
    data = data or {}
    sid = request.sid
    room = get_room_for_sid(sid)
    player_info = room.players.get(sid)
    if not player_info:
        _, player_info = resolve_player_for_event(data)
    if not player_info:
        emit("queue_error", {"reason": "player_missing"})
        return
    username = player_info["username"]
    reconnect_token = player_info.get("reconnect_token", "")
    resolved_room = resolve_room_for_event(data, player_info)
    active_humans = [
        info for info in resolved_room.players.values()
        if not is_bot_player(info) and info.get("connected", False)
    ]
    if len(active_humans) >= 2:
        print(f"[INFO] Ohitetaan join_queue: {username} on jo kahden pelaajan huoneessa {resolved_room.room_id}.")
        return
    mapped_room_id = get_room_id_for_reconnect_token(reconnect_token)
    mapped_room = rooms.get(mapped_room_id)
    if mapped_room:
        mapped_active_humans = [
            info for info in mapped_room.players.values()
            if not is_bot_player(info) and info.get("connected", False)
        ]
        if len(mapped_active_humans) >= 2:
            print(f"[INFO] Ohitetaan join_queue: {username} on jo matchatussa huoneessa {mapped_room_id}.")
            return
    card_mode = str(data.get("card_mode") or "image_word").strip().lower()
    if card_mode not in {"images", "image_word", "words"}:
        card_mode = "image_word"
    target_language = str(data.get("target_language") or "").strip().lower()
    player_info["in_waiting"] = True
    player_info["pref_card_mode"] = card_mode
    player_info["pref_target_language"] = "" if card_mode == "images" else target_language
    player_info["pref_ready"] = (
        card_mode == "images"
        or bool(player_info["pref_target_language"])
    )
    room.card_mode = player_info["pref_card_mode"] or room.card_mode
    room.target_language = player_info["pref_target_language"] or ""
    emit("queue_joined", {"username": username})
    payload = build_lobby_payload(room)
    emit_to_room("player_joined", payload, room_id=room.room_id)
    broadcast_lobby_browser()


@socketio.on("leave_queue")
def handle_leave_queue(data=None):
    leave_matchmaking_queue(request.sid)
    emit("queue_left", {})


@socketio.on("preference_changed")
def handle_preference_changed(data=None):
    data = data or {}
    sid = request.sid
    room = get_room_for_sid(sid)
    if not room or sid not in room.players:
        return
    card_mode = str(data.get("card_mode") or "").strip().lower()
    if card_mode in {"images", "image_word", "words"}:
        room.players[sid]["pref_card_mode"] = card_mode
    target_language = str(data.get("target_language") or "").strip().lower()
    if room.players[sid].get("pref_card_mode") == "images":
        target_language = ""
    room.players[sid]["pref_target_language"] = target_language
    room.players[sid]["pref_ready"] = True
    connected_humans = [
        (player_sid, info) for player_sid, info in room.players.items()
        if not is_bot_player(info) and info.get("connected", False)
    ]
    if room.play_mode == "queue" and len(connected_humans) == 1 and connected_humans[0][0] == sid:
        if room.queue_round_prepared:
            clear_round_runtime(room)
            reset_pending_state(room)
        room.card_mode = room.players[sid].get("pref_card_mode") or room.card_mode
        room.target_language = room.players[sid].get("pref_target_language") or ""
    payload = build_lobby_payload(room)
    emit_to_room("player_joined", payload, room_id=room.room_id)
    broadcast_lobby_browser()


@socketio.on("request_lobby_browser")
def handle_request_lobby_browser():
    emit("lobby_browser_updated", {"rooms": get_available_rooms()})


@socketio.on("join_room_direct")
def handle_join_room_direct(data=None):
    data = data or {}
    target_room_id = str(data.get("room_id") or "").strip()
    sid = request.sid
    current_room = get_room_for_sid(sid)
    player_info = current_room.players.get(sid)
    if not player_info:
        return
    username = player_info.get("username", "")
    reconnect_token = player_info.get("reconnect_token", "")

    if not target_room_id or target_room_id not in rooms:
        emit("direct_join_failed", {"reason": "Huone ei löydy."})
        return
    target_room = rooms[target_room_id]
    if target_room.status != "waiting":
        emit("direct_join_failed", {"reason": "Peli on jo alkanut."})
        broadcast_lobby_browser()
        return
    if not target_room.queue_round_prepared or len(target_room.grid_data) < 16:
        emit("direct_join_failed", {"reason": "Huone ei ole vielä valmis."})
        broadcast_lobby_browser()
        return
    human_count = sum(1 for info in target_room.players.values() if not is_bot_player(info))
    if human_count >= MAX_PLAYERS:
        emit("direct_join_failed", {"reason": "Huone on täynnä."})
        broadcast_lobby_browser()
        return

    leave_matchmaking_queue(sid)
    leave_matchmaking_queue_by_token(reconnect_token)
    for target_sid, target_info in list(target_room.players.items()):
        if is_bot_player(target_info):
            continue
        leave_matchmaking_queue(target_sid)
        leave_matchmaking_queue_by_token(target_info.get("reconnect_token"))

    # Remove stale memberships before moving the player to the target room.
    remove_player_memberships(reconnect_token=reconnect_token, sid=sid)

    # Add to target room
    target_room.players[sid] = {
        "username": username,
        "reconnect_token": reconnect_token,
        "connected": True,
        "disconnected_at": None,
        "room_id": target_room_id,
        "in_waiting": True,
        "pref_card_mode": target_room.card_mode,
        "pref_target_language": target_room.target_language,
        "pref_ready": True,
    }
    move_sid_to_room(sid, target_room_id)
    assign_reconnect_token_to_room(reconnect_token, target_room_id)

    print(f"[INFO] {username} liittyi huoneeseen {target_room_id} suoraan.")
    payload = build_lobby_payload(target_room)
    payload["username"] = username
    emit_to_room("player_joined", payload, room_id=target_room_id)
    broadcast_lobby_browser()
    target_room.player_order = [info["username"] for info in get_effective_players_ordered(target_room)]
    target_room.last_tokens = set(v["reconnect_token"] for v in get_effective_players_ordered(target_room))
    launch_grid_round(target_room)


@socketio.on("start_waiting_bot_round")
def handle_start_waiting_bot_round(data=None):
    data = data or {}
    _, player_info = resolve_player_for_event(data)
    if not player_info:
        emit("queue_bot_failed", {"reason": "Pelaajaa ei löytynyt."})
        return

    room = resolve_room_for_event(data, player_info)
    if room.play_mode != "queue" or not room.queue_round_prepared or len(room.grid_data) < 16:
        emit("queue_bot_failed", {"reason": "Kierros ei ole vielä valmis."})
        return

    human_players = get_effective_human_player_items(room)
    if len(human_players) != 1 or human_players[0][1].get("reconnect_token") != player_info.get("reconnect_token"):
        emit("queue_bot_failed", {"reason": "Vain huoneen perustaja voi käynnistää bottipelin."})
        return

    remove_bot_players(room)
    room.play_mode = "bot"
    requested_difficulty = str(data.get("bot_difficulty") or room.bot_difficulty or "easy").strip().lower()
    room.bot_difficulty = requested_difficulty if requested_difficulty in BOT_MEMORY_USE_PROBABILITY else "easy"
    ensure_bot_opponent(room)
    room.player_order = [info["username"] for info in get_effective_players_ordered(room)]
    room.last_tokens = set(v["reconnect_token"] for v in get_effective_players_ordered(room))
    broadcast_lobby_browser()
    launch_grid_round(room)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"[INFO] Muistipeli käynnistyy portissa {port} (host=0.0.0.0)")
    socketio.run(app, host="0.0.0.0", port=port, debug=False, use_reloader=False)
