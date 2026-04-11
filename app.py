import warnings

warnings.filterwarnings("ignore", message=".*Eventlet is deprecated.*")

import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
import random
import os
import re
import time
import requests
import uuid
from dataclasses import dataclass, field
from dotenv import load_dotenv   # <-- TÃ„MÃ„
from collections import defaultdict
load_dotenv()                   # <--

app = Flask(__name__)
# Salli yhteydet myÃ¶s muista koneista/osoitteista (kehitystÃ¤ varten)
socketio = SocketIO(
    app,
    async_mode='eventlet',
    cors_allowed_origins="*",
    logger=False,
    engineio_logger=False,
)

VERBOSE_DEBUG = str(os.getenv("VERBOSE_DEBUG", "0")).lower() in {"1", "true", "yes"}
RECONNECT_GRACE_SECONDS = max(30, int(os.getenv("RECONNECT_GRACE_SECONDS", "300")))
PAGE_TRANSITION_GRACE_SECONDS = 5
APP_VERSION = "Beta v0.0.4 (2026-04-08)"
BOT_USERNAME = "Muistibotti"
BOT_FIRST_FLIP_DELAY_SECONDS = 1.5
BOT_SECOND_FLIP_DELAY_SECONDS = 1.9
current_ui_language = "en"
DEFAULT_ROOM_ID = "default"


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
    status: str = "waiting"
    game_mode: str = "manual"
    play_mode: str = "queue"
    ui_language: str = "en"
    native_language: str = "fi"
    target_language: str = "es"
    image_mode: str = "pixabay"
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
    spanish_generation_in_progress: bool = False
    theme_generation_in_progress: bool = False
    bot_turn_scheduled: bool = False
    current_click_sid: str | None = None
    last_tokens: set = field(default_factory=set)

players = {}  # { sid: {"username": ..., "reconnect_token": ...} }
max_players = 2
grid_data = []
revealed_cards = []
matched_indices = set()
turn = 0
player_points = {}
round_win = defaultdict(int)
player_order = []  # Pelaajien jÃ¤rjestys
current_game_mode = "manual"
pending_theme = None
pending_search_theme = None
theme_candidates = []
theme_rejected_words = set()
theme_selection_state = {}
used_pixabay_image_ids = set()
# Prevent double-starting Spanish generation when multiple clients trigger asks
spanish_generation_in_progress = False
# Prevent double-starting Theme generation as well
theme_generation_in_progress = False
# Ensure two-card selections come from the same player/turn
current_click_sid = None
theme_words_cache = {}
translation_cache = {}
pixabay_cache = {}
theme_translation_cache = {}
bot_turn_scheduled = False
rooms = {}
player_room_index = {}

SPANISH_TRANSLATION_API_URL = "https://api.mymemory.translated.net/get"
THEME_TRANSLATION_OVERRIDES = {
    "ravintola": "restaurant",
    "ruoka": "food",
    "hedelmät": "fruits",
    "hedelmat": "fruits",
    "eläimet": "animals",
    "elaimet": "animals",
    "eläin": "animal",
    "elain": "animal",
    "urheilu": "sport",
    "olut": "beer",
    "juoma": "drink",
    "juomat": "drinks",
    "keittiö": "kitchen",
    "keittio": "kitchen",
}
THEME_TRANSLATION_FIXES = {
    "resturant": "restaurant",
}
ABSTRACT_THEME_WORDS = {
    "ability", "advice", "anger", "belief", "concept", "courage", "emotion", "faith",
    "freedom", "friendship", "future", "happiness", "hope", "idea", "justice",
    "knowledge", "logic", "love", "peace", "power", "quality", "spirit", "strategy",
    "strength", "success", "theory", "thought", "truth", "value", "vision", "wisdom"
}


def create_room(room_id=DEFAULT_ROOM_ID):
    room = RoomState(
        room_id=room_id,
        game_mode=current_game_mode,
        ui_language=current_ui_language,
        players=players,
        player_order=player_order,
        grid_data=grid_data,
        revealed_cards=revealed_cards,
        matched_indices=matched_indices,
        turn=turn,
        player_points=player_points,
        round_win=round_win,
        pending_theme=pending_theme,
        pending_search_theme=pending_search_theme,
        theme_candidates=theme_candidates,
        theme_rejected_words=theme_rejected_words,
        theme_selection_state=theme_selection_state,
        used_image_ids=used_pixabay_image_ids,
        spanish_generation_in_progress=spanish_generation_in_progress,
        theme_generation_in_progress=theme_generation_in_progress,
        bot_turn_scheduled=bot_turn_scheduled,
        current_click_sid=current_click_sid,
    )
    rooms[room_id] = room
    return room


def get_room(room_id=DEFAULT_ROOM_ID):
    return rooms.get(room_id) or create_room(room_id)


def get_default_room():
    return get_room(DEFAULT_ROOM_ID)


def get_room_for_request():
    sid = request.sid
    player_info = players.get(sid)
    if player_info and player_info.get("room_id"):
        return get_room(player_info["room_id"])
    reconnect_token = ((request.args or {}).get("reconnect_token") or "").strip()
    return get_room(get_room_id_for_reconnect_token(reconnect_token))


def sync_default_room_from_globals():
    room = get_default_room()
    room.status = "playing" if grid_data else ("setup" if ('pending_pair' in globals() or theme_selection_state) else "waiting")
    room.game_mode = current_game_mode
    room.ui_language = current_ui_language
    room.players = players
    room.player_order = player_order
    room.grid_data = grid_data
    room.revealed_cards = revealed_cards
    room.matched_indices = matched_indices
    room.turn = turn
    room.player_points = player_points
    room.round_win = round_win
    room.pending_theme = pending_theme
    room.pending_search_theme = pending_search_theme
    room.theme_candidates = theme_candidates
    room.theme_rejected_words = theme_rejected_words
    room.theme_selection_state = theme_selection_state
    room.used_image_ids = used_pixabay_image_ids
    room.pending_pair = globals().get("pending_pair", 0)
    room.pending_player = globals().get("pending_player")
    room.spanish_generation_in_progress = spanish_generation_in_progress
    room.theme_generation_in_progress = theme_generation_in_progress
    room.bot_turn_scheduled = bot_turn_scheduled
    room.current_click_sid = current_click_sid
    room.last_tokens = set(getattr(handle_start_custom_game, "last_tokens", set()))
    return room


def sync_globals_from_default_room():
    global current_game_mode, pending_theme, pending_search_theme, theme_candidates, theme_rejected_words
    global theme_selection_state, used_pixabay_image_ids, spanish_generation_in_progress, theme_generation_in_progress
    global current_click_sid, bot_turn_scheduled, current_ui_language, player_order, grid_data, revealed_cards, players
    global matched_indices, turn, player_points, round_win
    room = get_default_room()
    players = room.players
    current_game_mode = room.game_mode
    current_ui_language = room.ui_language
    pending_theme = room.pending_theme
    pending_search_theme = room.pending_search_theme
    theme_candidates = room.theme_candidates
    theme_rejected_words = room.theme_rejected_words
    theme_selection_state = room.theme_selection_state
    used_pixabay_image_ids = room.used_image_ids
    spanish_generation_in_progress = room.spanish_generation_in_progress
    theme_generation_in_progress = room.theme_generation_in_progress
    current_click_sid = room.current_click_sid
    bot_turn_scheduled = room.bot_turn_scheduled
    player_order = room.player_order
    grid_data = room.grid_data
    revealed_cards = room.revealed_cards
    matched_indices = room.matched_indices
    turn = room.turn
    player_points = room.player_points
    round_win = room.round_win
    globals()['pending_pair'] = room.pending_pair
    if room.pending_player is None:
        globals().pop('pending_player', None)
    else:
        globals()['pending_player'] = room.pending_player
    return room


def assign_reconnect_token_to_room(reconnect_token, room_id=DEFAULT_ROOM_ID):
    if reconnect_token:
        player_room_index[reconnect_token] = room_id


def clear_reconnect_token_room(reconnect_token):
    if reconnect_token:
        player_room_index.pop(reconnect_token, None)


def get_room_id_for_reconnect_token(reconnect_token):
    return player_room_index.get(reconnect_token) or DEFAULT_ROOM_ID


def reset_pending_state():
    global current_game_mode, pending_theme, pending_search_theme, theme_candidates, theme_rejected_words, theme_selection_state, used_pixabay_image_ids, spanish_generation_in_progress, theme_generation_in_progress, current_click_sid, bot_turn_scheduled, current_ui_language
    globals().pop('pending_pair', None)
    globals().pop('pending_words', None)
    globals().pop('pending_player', None)
    current_game_mode = "manual"
    pending_theme = None
    pending_search_theme = None
    theme_candidates = []
    theme_rejected_words = set()
    theme_selection_state = {}
    used_pixabay_image_ids = set()
    spanish_generation_in_progress = False
    theme_generation_in_progress = False
    current_click_sid = None
    bot_turn_scheduled = False
    current_ui_language = "en"
    room = sync_default_room_from_globals()
    room.status = "waiting" if not grid_data else room.status


def is_bot_player(player_or_name):
    room = get_default_room()
    if isinstance(player_or_name, dict):
        return bool(player_or_name.get("is_bot"))
    if isinstance(player_or_name, str):
        return any(
            info.get("username") == player_or_name and info.get("is_bot")
            for info in room.players.values()
        )
    return False


def get_human_player_items():
    room = get_default_room()
    return [(sid, data) for sid, data in room.players.items() if not is_bot_player(data)]


def get_effective_human_player_items():
    return [(sid, data) for sid, data in get_effective_player_items() if not is_bot_player(data)]


def remove_bot_players():
    removed = False
    for sid, info in list(players.items()):
        if is_bot_player(info):
            clear_reconnect_token_room(info.get("reconnect_token"))
            del players[sid]
            removed = True
    if removed:
        sync_default_room_from_globals()
    return removed


def ensure_bot_opponent():
    if any(is_bot_player(info) for info in players.values()):
        return
    bot_sid = f"bot:{uuid.uuid4()}"
    bot_token = f"bot-{uuid.uuid4()}"
    players[bot_sid] = {
        "username": BOT_USERNAME,
        "reconnect_token": bot_token,
        "connected": True,
        "disconnected_at": None,
        "is_bot": True,
        "room_id": DEFAULT_ROOM_ID
    }
    assign_reconnect_token_to_room(bot_token, DEFAULT_ROOM_ID)
    sync_default_room_from_globals()
    print(f"[INFO] {BOT_USERNAME} lisättiin bot-vastustajaksi.")


def get_first_human_player_name():
    for player in get_effective_players_ordered():
        if not is_bot_player(player):
            return player.get("username")
    return player_order[0] if player_order else None


def get_active_bot_identity():
    for sid, info in get_effective_player_items():
        if is_bot_player(info):
            return sid, info
    return None, None


def schedule_bot_turn_if_needed(delay=BOT_FIRST_FLIP_DELAY_SECONDS):
    global bot_turn_scheduled
    sync_globals_from_default_room()
    if bot_turn_scheduled:
        return
    if not grid_data or len(grid_data) < 2:
        return
    if not player_order or turn >= len(player_order):
        return
    if not is_bot_player(player_order[turn]):
        return
    bot_sid, bot_info = get_active_bot_identity()
    if not bot_info:
        return

    bot_turn_scheduled = True
    sync_default_room_from_globals()

    def bot_take_turn():
        global bot_turn_scheduled
        try:
            sync_globals_from_default_room()
            socketio.sleep(delay)
            if not grid_data or not player_order or turn >= len(player_order):
                return
            if not is_bot_player(player_order[turn]):
                return
            available_indices = [
                index for index in range(len(grid_data))
                if index not in matched_indices and index not in revealed_cards
            ]
            if len(available_indices) < 2:
                return
            first_index, second_index = random.sample(available_indices, 2)
            process_card_click(first_index, bot_sid, bot_info)
            socketio.sleep(BOT_SECOND_FLIP_DELAY_SECONDS)
            sync_globals_from_default_room()
            if grid_data and player_order and turn < len(player_order) and is_bot_player(player_order[turn]):
                process_card_click(second_index, bot_sid, bot_info)
        finally:
            bot_turn_scheduled = False
            sync_default_room_from_globals()
            if grid_data and player_order and turn < len(player_order) and is_bot_player(player_order[turn]):
                schedule_bot_turn_if_needed(delay=BOT_FIRST_FLIP_DELAY_SECONDS)

    socketio.start_background_task(bot_take_turn)


def get_active_player_items():
    room = get_default_room()
    return [(sid, data) for sid, data in room.players.items() if data.get("connected", True)]


def get_active_players_ordered():
    return [data for _, data in get_active_player_items()]


def get_active_player_count():
    return len(get_active_player_items())


def is_effectively_present(player_data):
    if player_data.get("connected", True):
        return True
    disconnected_at = player_data.get("disconnected_at")
    if disconnected_at is None:
        return False
    return (time.monotonic() - disconnected_at) < PAGE_TRANSITION_GRACE_SECONDS


def get_effective_player_items():
    room = get_default_room()
    return [(sid, data) for sid, data in room.players.items() if is_effectively_present(data)]


def get_effective_players_ordered():
    return [data for _, data in get_effective_player_items()]


def get_effective_player_count():
    return len(get_effective_player_items())


def resolve_player_for_event(data=None):
    sid = request.sid
    player_info = players.get(sid)
    if player_info:
        player_info["connected"] = True
        player_info["disconnected_at"] = None
        return sid, player_info

    reconnect_token = ((data or {}).get("reconnect_token") or "").strip()
    username_hint = ((data or {}).get("username") or "").strip()
    matched_sid = None

    for existing_sid, info in list(players.items()):
        if reconnect_token and info.get("reconnect_token") == reconnect_token:
            matched_sid = existing_sid
            player_info = info
            break
        if username_hint and info.get("username") == username_hint:
            matched_sid = existing_sid
            player_info = info
            break

    if not player_info:
        return sid, None

    players[sid] = {
        **player_info,
        "connected": True,
        "disconnected_at": None
    }
    if matched_sid is not None and matched_sid != sid and matched_sid in players:
        del players[matched_sid]
    return sid, players[sid]


def theme_selection_active():
    sync_globals_from_default_room()
    return bool(theme_selection_state.get("active"))


def sync_theme_selection_players():
    global player_order
    sync_globals_from_default_room()
    if not theme_selection_active():
        return []

    current_players = get_effective_players_ordered()
    if not current_players:
        return []

    old_tokens = dict(theme_selection_state.get("player_tokens", {}))
    old_counts = dict(theme_selection_state.get("counts", {}))
    old_ready = dict(theme_selection_state.get("ready", {}))
    new_counts = {}
    new_ready = {}
    new_tokens = {}

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

    selected_words = theme_selection_state.get("selected_words", [])
    rename_map = {}
    for player in current_players:
        username = player["username"]
        reconnect_token = player.get("reconnect_token")
        for old_name, old_token in old_tokens.items():
            if reconnect_token and old_token == reconnect_token and old_name != username:
                rename_map[old_name] = username

    if rename_map:
        for item in selected_words:
            chosen_by = item.get("chosen_by")
            if chosen_by in rename_map:
                item["chosen_by"] = rename_map[chosen_by]

    player_order = [player["username"] for player in current_players]
    theme_selection_state["counts"] = new_counts
    theme_selection_state["ready"] = new_ready
    theme_selection_state["player_tokens"] = new_tokens
    sync_default_room_from_globals()
    return current_players


def build_theme_selection_payload(message=None):
    sync_globals_from_default_room()
    if not theme_selection_active():
        return {"active": False}
    sync_theme_selection_players()
    return {
        "active": True,
        "theme": theme_selection_state.get("theme"),
        "starter_name": theme_selection_state.get("starter_name"),
        "mode": current_game_mode,
        "candidates": list(theme_selection_state.get("candidates", [])),
        "selected_words": [
            {
                "word": item.get("word"),
                "display_word": item.get("display_word", item.get("word")),
                "chosen_by": item.get("chosen_by")
            }
            for item in theme_selection_state.get("selected_words", [])
        ],
        "candidate_words": [
            {
                "word": word,
                "display_word": theme_selection_state.get("candidate_labels", {}).get(word, word)
            }
            for word in theme_selection_state.get("candidates", [])
        ],
        "rejected_words": list(theme_selection_state.get("rejected_words", [])),
        "counts": dict(theme_selection_state.get("counts", {})),
        "ready": dict(theme_selection_state.get("ready", {})),
        "swap_limit": int(theme_selection_state.get("swap_limit", 1)),
        "players": list(player_order),
        "message": message
    }


def emit_theme_selection_state(message=None, sid=None):
    payload = build_theme_selection_payload(message=message)
    if sid:
        socketio.emit("theme_selection_updated", payload, to=sid)
        return
    socketio.emit("theme_selection_updated", payload)


def deactivate_theme_selection():
    global theme_selection_state, theme_generation_in_progress, spanish_generation_in_progress
    sync_globals_from_default_room()
    theme_selection_state = {}
    theme_generation_in_progress = False
    spanish_generation_in_progress = False
    sync_default_room_from_globals()


def get_theme_display_word(word, entry=None):
    base_word = str(word or "").strip()
    if not base_word:
        return ""
    if current_ui_language != "fi":
        return base_word
    if entry and entry.get("type") == "spanish":
        pair = entry.get("pair") or {}
        finnish_word = (pair.get("finnish_word") or "").strip()
        if finnish_word:
            return finnish_word
    translated = translate_word_to_finnish(base_word)
    return translated or base_word


def normalize_display_label(word):
    return normalize_candidate_word(str(word or "").strip())


def build_candidate_labels(words):
    labels = {}
    for word in words or []:
        labels[word] = get_theme_display_word(word)
    return labels


def build_theme_candidate_list(search_theme, candidate_count=24):
    raw_candidates = fetch_theme_words(search_theme, max_results=72, require_noun=False)
    filtered = []
    seen = set()
    for word in raw_candidates:
        if word in seen:
            continue
        if not is_concrete_theme_word(word):
            continue
        seen.add(word)
        filtered.append(word)
        if len(filtered) >= candidate_count:
            break
    return filtered


def prepare_theme_selection(starter_name):
    global theme_selection_state
    sync_globals_from_default_room()
    search_theme = pending_search_theme or pending_theme
    candidates = build_theme_candidate_list(search_theme, candidate_count=24)
    if len(candidates) < 8:
        message = f"Teemasta '{pending_theme}' ei lÃ¶ytynyt tarpeeksi kÃ¤yttÃ¶kelpoisia sanoja. Kokeile toista teemaa."
        print(f"[WARNING] {message}")
        grid_data.clear()
        reset_pending_state()
        socketio.emit("game_setup_error", {"reason": message})
        return

    counts = {name: 0 for name in player_order}
    ready = {name: is_bot_player(name) for name in player_order}
    selected_words = []
    rejected_words = []
    remaining_candidates = []

    socketio.emit("theme_generation_started", {
        "theme": pending_theme,
        "pair": pending_pair + 1,
        "mode": current_game_mode,
        "starter_name": starter_name,
        "phase": "drawing_cards",
        "progress_count": pending_pair,
        "total_pairs": 8
    })
    socketio.sleep(0)

    for word in candidates:
        if len(selected_words) >= 8:
            remaining_candidates.append(word)
            continue
        pair_index = len(selected_words)
        try:
            pair_entry = build_pair_entry_for_mode(word, pair_index)
        except PixabayConfigError as e:
            abort_round_due_to_pixabay_error(str(e))
            return
        if pair_entry is None:
            rejected_words.append(word)
            theme_rejected_words.add(word)
            continue
        display_word = get_theme_display_word(word, pair_entry)
        display_key = normalize_display_label(display_word)
        if display_key and any(normalize_display_label(item.get("display_word", item.get("word"))) == display_key for item in selected_words):
            remaining_candidates.append(word)
            continue
        selected_words.append({
            "word": word,
            "display_word": display_word,
            "chosen_by": "System",
            "entry": pair_entry
        })
        socketio.emit("theme_word_accepted", {
            "theme": pending_theme,
            "word": word,
            "pair": len(selected_words),
            "total_pairs": 8,
            "mode": current_game_mode
        })
        socketio.sleep(0)

    if len(selected_words) < 8:
        message = f"Teemasta '{pending_theme}' ei lÃƒÂ¶ytynyt tarpeeksi kÃƒÂ¤yttÃƒÂ¶kelpoisia sanoja. Kokeile toista teemaa."
        print(f"[WARNING] {message}")
        grid_data.clear()
        reset_pending_state()
        socketio.emit("game_setup_error", {"reason": message})
        return

    theme_selection_state = {
        "active": True,
        "theme": pending_theme,
        "search_theme": search_theme,
        "starter_name": starter_name,
        "candidates": remaining_candidates,
        "candidate_labels": build_candidate_labels(remaining_candidates),
        "selected_words": selected_words,
        "rejected_words": rejected_words,
        "counts": counts,
        "ready": ready,
        "player_tokens": {
            player["username"]: player.get("reconnect_token")
            for player in get_effective_players_ordered()
        },
        "swap_limit": 1,
    }
    print(f"[INFO] Teeman '{pending_theme}' sanavalinta aloitettu tilassa '{current_game_mode}'. Ehdokkaita: {len(candidates)}")
    sync_default_room_from_globals()
    emit_theme_selection_state()
    if player_order and all(ready.get(name, False) for name in player_order):
        print(f"[INFO] Kaikki pelaajat valmiina â€“ aloitetaan {current_game_mode}-erÃ¤ teemalla '{pending_theme}'")
        finalize_theme_selection()


def append_selected_spanish_pair(word, pair_index):
    spanish_word = translate_word_to_spanish(word)
    if not spanish_word:
        print(f"[INFO] Espanjan kaannos ei kelpaa sanalle '{word}', haetaan korvaaja")
        return False

    finnish_word = translate_word_to_finnish(word)
    print(f"[INFO] Kokeillaan espanjapariksi '{word}' -> '{spanish_word}' parille {pair_index + 1}")
    image_paths = fetch_and_save_pixabay_images(word, pair_index, required_count=1)
    if not image_paths:
        print(f"[INFO] Pixabay ei loytanyt espanjaparille '{word}' sopivaa kuvaa, haetaan korvaaja")
        return False

    pair = {
        "pair_id": pair_index + 1,
        "english_word": word,
        "spanish_word": spanish_word,
        "finnish_word": finnish_word,
        "image_url": image_source_for_card(image_paths[0])
    }
    return pair


def abort_round_due_to_pixabay_error(message):
    print(f"[ERROR] {message}")
    grid_data.clear()
    reset_pending_state()
    socketio.emit("game_setup_error", {"reason": message})


def next_theme_picker_name(current_name):
    if len(player_order) < 2:
        return None
    if current_name == player_order[0]:
        return player_order[1]
    return player_order[0]


def spanish_setup_still_active(theme):
    active_pair = globals().get('pending_pair')
    if active_pair is None:
        return False
    if current_game_mode != "spanish":
        return False
    if pending_theme != theme:
        return False
    if get_effective_player_count() < 2:
        return False
    return True


def normalize_candidate_word(word):
    cleaned = str(word or "").strip().lower()
    if not re.fullmatch(r"[a-z]{2,18}", cleaned):
        return None
    return cleaned


def normalize_spanish_word(word):
    cleaned = str(word or "").strip().lower()
    cleaned = re.sub(r"^[^a-zÃ¡Ã©Ã­Ã³ÃºÃ¼Ã±]+|[^a-zÃ¡Ã©Ã­Ã³ÃºÃ¼Ã±]+$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(el|la|los|las|un|una|unos|unas)\s+", "", cleaned, flags=re.IGNORECASE)
    if not re.fullmatch(r"[a-zÃ¡Ã©Ã­Ã³ÃºÃ¼Ã±]{2,18}", cleaned, flags=re.IGNORECASE):
        return None
    return cleaned


def is_concrete_theme_word(word):
    return word not in ABSTRACT_THEME_WORDS


def has_noun_tag(tags):
    for tag in tags or []:
        if isinstance(tag, str) and tag.lower() == "n":
            return True
    return False


def has_proper_tag(tags):
    for tag in tags or []:
        if not isinstance(tag, str):
            continue
        lowered = tag.lower()
        if lowered in {"prop", "place", "geog"} or "prop" in lowered:
            return True
    return False


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
    all_words = []
    seen = set()
    # Request part-of-speech tags (md=p) so we can filter nouns and proper nouns.
    query_variants = [{"ml": theme, "max": max_results, "md": "p"}]
    fallback_variant = {"topics": theme, "max": max_results, "md": "p"}
    target_min = min(max_results, 32)

    for index, params in enumerate(query_variants):
        try:
            response = requests.get("https://api.datamuse.com/words", params=params, timeout=6)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as e:
            debug(f"[DEBUG] Datamuse-pyyntÃ¶ epÃ¤onnistui ({params}): {e}")
            continue
        except ValueError as e:
            debug(f"[DEBUG] Datamuse palautti virheellistÃ¤ JSONia ({params}): {e}")
            continue

        for item in payload:
            word = normalize_candidate_word(item.get("word"))
            if not word or word in seen:
                continue
            tags = item.get("tags") or []

            # Require a noun when requested.
            if require_noun and not has_noun_tag(tags):
                debug(f"[DEBUG] Hylattiin ei-substantiivi '{word}' (tags={tags})")
                continue

            # Optionally exclude proper nouns / places (Datamuse often marks these with 'prop').
            if exclude_proper and has_proper_tag(tags):
                debug(f"[DEBUG] Hylattiin erisnimi '{word}' (tags={tags})")
                continue

            seen.add(word)
            all_words.append(word)

        if index == 0 and len(all_words) < target_min:
            query_variants.append(fallback_variant)

    preview = ", ".join(all_words[:12]) if all_words else "(ei sanoja)"
    print(f"[INFO] Datamuse ehdotti teemalle '{theme}' {len(all_words)} sanaa: {preview}")
    theme_words_cache[cache_key] = list(all_words)
    return all_words


def translate_word(word, source_lang, target_lang):
    normalized_word = normalize_candidate_word(word)
    if not normalized_word:
        return None
    cache_key = (normalized_word, source_lang, target_lang)
    if cache_key in translation_cache:
        return translation_cache[cache_key]

    try:
        response = requests.get(
            SPANISH_TRANSLATION_API_URL,
            params={"q": normalized_word, "langpair": f"{source_lang}|{target_lang}"},
            timeout=5
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[WARNING] Kaannos epÃ¤onnistui sanalle '{normalized_word}' ({source_lang}->{target_lang}): {e}")
        translation_cache[cache_key] = None
        return None

    translated_text = (
        (payload.get("responseData") or {}).get("translatedText")
        or ""
    )
    translated_text = re.sub(r"\s*\(.*?\)\s*", " ", translated_text).strip()
    if any(separator in translated_text for separator in [",", ";", "/", "|"]):
        translation_cache[cache_key] = None
        return None

    normalized_translation = normalize_spanish_word(translated_text)
    if not normalized_translation:
        translation_cache[cache_key] = None
        return None
    if (
        target_lang != "es"
        and normalize_candidate_word(normalized_translation) == normalized_word
    ):
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
    theme_key = normalize_candidate_word(theme_text)
    cache_key = (theme_key or theme_text.lower(), ui_language)
    if cache_key in theme_translation_cache:
        return theme_translation_cache[cache_key]
    if ui_language != "fi":
        translated = THEME_TRANSLATION_FIXES.get(theme_text.lower(), theme_text)
        theme_translation_cache[cache_key] = translated
        return translated

    if theme_key and theme_key in THEME_TRANSLATION_OVERRIDES:
        translated = THEME_TRANSLATION_OVERRIDES[theme_key]
        print(f"[INFO] Teema käännettiin paikallisella sanastolla: '{theme_text}' -> '{translated}'")
        theme_translation_cache[cache_key] = translated
        return translated

    try:
        response = requests.get(
            SPANISH_TRANSLATION_API_URL,
            params={"q": theme_text, "langpair": "fi|en"},
            timeout=10
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[WARNING] Teeman kaanto suomesta englanniksi epÃ¤onnistui ('{theme_text}'): {e}")
        theme_translation_cache[cache_key] = theme_text
        return theme_text

    translated_text = ((payload.get("responseData") or {}).get("translatedText") or "").strip()
    translated_text = re.sub(r"\s*\(.*?\)\s*", " ", translated_text).strip()
    translated_text = re.sub(r"\s+", " ", translated_text)
    if not translated_text or any(separator in translated_text for separator in [";", "/", "|"]):
        theme_translation_cache[cache_key] = theme_text
        return theme_text
    translated_text = THEME_TRANSLATION_FIXES.get(translated_text.lower(), translated_text)

    print(f"[INFO] Teema kaannettiin suomesta englanniksi: '{theme_text}' -> '{translated_text}'")
    theme_translation_cache[cache_key] = translated_text
    return translated_text


def image_source_for_card(image_ref):
    if not image_ref:
        return image_ref
    if isinstance(image_ref, str) and image_ref.startswith(("http://", "https://")):
        return image_ref
    return "/" + str(image_ref).replace("\\", "/").lstrip("/")


def append_word_images_to_grid(word, pair_index):
    global grid_data
    search_word = word
    if current_game_mode == "manual":
        search_word = translate_word_to_english(word, "fi" if current_ui_language == "fi" else "en") or word
        if normalize_candidate_word(search_word) != normalize_candidate_word(word):
            print(f"[INFO] Manual-pelin Pixabay-haku englanniksi: '{word}' -> '{search_word}'")
    result = fetch_and_save_pixabay_images(search_word, pair_index)
    if not result:
        print(f"[INFO] Pixabay-haku epaonnistui sanalle '{word}'")
        return False
    print(f"[INFO] Pixabaysta loytyi kuvat sanalle '{word}': {result}")
    for path in result:
        grid_data.append({"image": image_source_for_card(path), "word": word})
    return True


def append_spanish_learning_pair_to_grid(pair):
    global grid_data
    grid_data.append({
        "pair_id": pair["pair_id"],
        "card_type": "word",
        "text": pair["spanish_word"],
        "word": pair["english_word"],
        "spanish_word": pair["spanish_word"],
        "finnish_word": pair.get("finnish_word")
    })
    grid_data.append({
        "pair_id": pair["pair_id"],
        "card_type": "image",
        "image": pair["image_url"],
        "word": pair["english_word"],
        "spanish_word": pair["spanish_word"],
        "finnish_word": pair.get("finnish_word")
    })


def build_theme_pair_entry(word, pair_index):
    result = fetch_and_save_pixabay_images(word, pair_index)
    if not result:
        print(f"[INFO] Pixabay-haku epaonnistui sanalle '{word}'")
        return None
    print(f"[INFO] Pixabaysta loytyi kuvat sanalle '{word}': {result}")
    return {
        "type": "theme",
        "word": word,
        "images": [image_source_for_card(path) for path in result]
    }


def build_pair_entry_for_mode(word, pair_index):
    if current_game_mode == "theme":
        return build_theme_pair_entry(word, pair_index)
    if current_game_mode == "spanish":
        pair = append_selected_spanish_pair(word, pair_index)
        if not pair:
            return None
        return {
            "type": "spanish",
            "pair": pair
        }
    return None


def append_pair_entry_to_grid(entry, pair_index):
    global grid_data
    if not entry:
        return
    if entry.get("type") == "theme":
        for image in entry.get("images", []):
            grid_data.append({
                "pair_id": pair_index + 1,
                "image": image,
                "word": entry.get("word")
            })
        return
    if entry.get("type") == "spanish":
        pair = dict(entry.get("pair") or {})
        pair["pair_id"] = pair_index + 1
        append_spanish_learning_pair_to_grid(pair)


def launch_grid_round():
    global matched_indices, revealed_cards, turn, player_points
    sync_globals_from_default_room()
    matched_indices = set()
    revealed_cards = []
    turn = 0
    player_points = {name: 0 for name in player_order}
    random.shuffle(grid_data)
    room = get_default_room()
    room.status = "playing"
    sync_default_room_from_globals()
    socketio.emit("init_grid", {
        "cards": grid_data,
        "turn": player_order[0] if player_order else None,
        "players": player_order
    })
    schedule_bot_turn_if_needed()


def conclude_round(winner_label, surrendered_by=None):
    global turn, player_points, current_click_sid, bot_turn_scheduled
    room = get_default_room()
    points_payload = {name: player_points.get(name, 0) for name in player_order}
    if isinstance(winner_label, str) and winner_label not in {"Tasapeli", "Tie"} and winner_label in player_order:
        round_win[winner_label] += 1
        print(f"[INFO] Voittaja: {winner_label}. ErÃ¤voitot: {dict(round_win)}")
    else:
        print(f"[INFO] Kierros pÃ¤Ã¤ttyi. Tulos: {winner_label}")

    socketio.emit("game_over", {
        "winner": winner_label,
        "points": points_payload,
        "round_win": dict(round_win),
        "surrendered_by": surrendered_by
    })

    try:
        grid_data.clear()
        revealed_cards.clear()
        matched_indices.clear()
    except Exception:
        pass
    turn = 0
    current_click_sid = None
    bot_turn_scheduled = False
    player_points = {}
    room.grid_data = grid_data
    room.revealed_cards = revealed_cards
    room.matched_indices = matched_indices
    room.turn = turn
    room.player_points = player_points
    room.status = "results"
    reset_pending_state()


def finalize_theme_selection():
    global grid_data
    sync_globals_from_default_room()
    sync_theme_selection_players()
    selected_words = list(theme_selection_state.get("selected_words", []))
    if len(selected_words) < 8:
        return
    grid_data.clear()
    for pair_index, item in enumerate(selected_words):
        append_pair_entry_to_grid(item.get("entry"), pair_index)
    deactivate_theme_selection()
    globals().pop('pending_player', None)
    globals().pop('pending_pair', None)
    sync_default_room_from_globals()
    launch_grid_round()


def generate_theme_pair():
    global pending_pair, pending_theme, pending_search_theme, theme_candidates, theme_rejected_words, theme_generation_in_progress
    sync_globals_from_default_room()
    existing_words = {item["word"] for item in grid_data}
    attempts = 0

    while attempts < 40:
        if not theme_candidates:
            search_theme = pending_search_theme or pending_theme
            theme_candidates = fetch_theme_words(search_theme, max_results=100)
            if not theme_candidates:
                break

        word = theme_candidates.pop(0)
        if word in existing_words or word in theme_rejected_words:
            attempts += 1
            continue

        print(f"[INFO] Kokeillaan teemasanaksi '{word}' parille {pair_index+1}")
        try:
            selection_ok = append_word_images_to_grid(word, pair_index)
        except PixabayConfigError as e:
            abort_round_due_to_pixabay_error(str(e))
            return
        if selection_ok:
            socketio.emit("theme_word_accepted", {
                "theme": pending_theme,
                "word": word,
                "pair": pair_index + 1,
                "total_pairs": 8
            })
            socketio.sleep(0)
            pending_pair += 1
            sync_default_room_from_globals()
            # Allow next pair generation
            theme_generation_in_progress = False
            ask_next_word()
            return

        print(f"[INFO] Pixabay ei loytanyt teemasanalle '{word}' sopivia kuvia, haetaan korvaaja")
        theme_rejected_words.add(word)
        attempts += 1

    message = f"Teemasta '{pending_theme}' ei lÃ¶ytynyt tarpeeksi kÃ¤yttÃ¶kelpoisia sanoja. Kokeile toista teemaa."
    print(f"[WARNING] {message}")
    grid_data.clear()
    reset_pending_state()
    socketio.emit("game_setup_error", {"reason": message})


def generate_spanish_learning_pairs(theme, target_pairs=8):
    global pending_pair, pending_theme, pending_search_theme, theme_candidates, theme_rejected_words
    sync_globals_from_default_room()
    existing_words = {item.get("word") for item in grid_data if item.get("word")}
    attempts = 0
    max_attempts = 160
    search_theme = pending_search_theme or theme

    while spanish_setup_still_active(theme) and pending_pair < target_pairs and attempts < max_attempts:
        if not theme_candidates:
            # For Spanish study mode, prefer nouns but do not immediately drop proper nouns.
            theme_candidates = fetch_theme_words(search_theme, max_results=120, require_noun=False, exclude_proper=False)
            theme_candidates = [
                candidate for candidate in theme_candidates
                if candidate not in existing_words and candidate not in theme_rejected_words
            ]
            if not theme_candidates:
                break

        english_word = theme_candidates.pop(0)
        attempts += 1

        if english_word in existing_words or english_word in theme_rejected_words:
            continue
        if not is_concrete_theme_word(english_word):
            theme_rejected_words.add(english_word)
            continue

        spanish_word = translate_word_to_spanish(english_word)
        if not spanish_word:
            print(f"[INFO] Espanjan kaannos ei kelpaa sanalle '{english_word}', haetaan korvaaja")
            theme_rejected_words.add(english_word)
            continue

        finnish_word = translate_word_to_finnish(english_word)

        if not spanish_setup_still_active(theme):
            print("[INFO] Espanjapelin generointi keskeytettiin, koska pelitila muuttui")
            return

        print(f"[INFO] Kokeillaan espanjapariksi '{english_word}' -> '{spanish_word}' parille {pending_pair + 1}")
        try:
            image_paths = fetch_and_save_pixabay_images(english_word, pending_pair, required_count=1)
        except PixabayConfigError as e:
            abort_round_due_to_pixabay_error(str(e))
            return
        if not image_paths:
            print(f"[INFO] Pixabay ei loytanyt espanjaparille '{english_word}' sopivaa kuvaa, haetaan korvaaja")
            theme_rejected_words.add(english_word)
            continue

        if not spanish_setup_still_active(theme):
            print("[INFO] Espanjapelin generointi keskeytettiin kuvanhaun aikana, koska pelitila muuttui")
            return

        pair = {
            "pair_id": pending_pair + 1,
            "english_word": english_word,
            "spanish_word": spanish_word,
            "finnish_word": finnish_word,
            "image_url": "/" + image_paths[0]
        }
        append_spanish_learning_pair_to_grid(pair)
        existing_words.add(english_word)
        socketio.emit("theme_word_accepted", {
            "theme": pending_theme,
            "word": english_word,
            "pair": pending_pair + 1,
            "total_pairs": target_pairs,
            "mode": "spanish"
        })
        socketio.sleep(0)
        pending_pair += 1
        sync_default_room_from_globals()

    if not spanish_setup_still_active(theme):
        print("[INFO] Espanjapelin generointi lopetettiin siististi keskeytyneen pelin vuoksi")
        return

    if pending_pair >= target_pairs:
        try:
            ask_next_word()
        finally:
            # Mark Spanish generation as finished so a new round can start cleanly
            global spanish_generation_in_progress
            spanish_generation_in_progress = False
        return

    message = f"Teemasta '{pending_theme}' ei lÃ¶ytynyt tarpeeksi kÃ¤yttÃ¶kelpoisia sanoja. Kokeile toista teemaa."
    print(f"[WARNING] {message}")
    grid_data.clear()
    reset_pending_state()
    socketio.emit("game_setup_error", {"reason": message})


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


def build_lobby_payload():
    room = sync_default_room_from_globals()
    players_ordered = get_active_players_ordered()
    usernames = [v["username"] for v in players_ordered]
    infos = [{"username": v["username"], "reconnect_token": v.get("reconnect_token")}
             for v in players_ordered]
    last_token = infos[-1]["reconnect_token"] if infos else None
    return {
        "room_id": room.room_id,
        "players": usernames,
        "players_info": infos,
        "last_joined_token": last_token
    }

@socketio.on("join")
def on_join(data):
    global players
    room = get_default_room()
    username = data["username"]
    sid = request.sid
    reconnect_token = data.get("reconnect_token")
    wants_bot = bool((data or {}).get("bot_mode"))
    room_id = get_room_id_for_reconnect_token(reconnect_token)
    if not reconnect_token:
        print(f"[WARNING] HYLÃ„TTY join ilman reconnect_tokenia: {username}, SID: {sid}")
        return
    # Sama selain voi vaihtaa sivua tai nimeÃ¤; reconnect_token yksilÃ¶i istunnon.
    for k, v in list(players.items()):
        if v.get("reconnect_token") == reconnect_token:
            del players[k]
    if wants_bot and not get_effective_human_player_items():
        ensure_bot_opponent()
    if get_effective_player_count() >= max_players:
        print(f"[INFO] Hylätty liittyminen, peli on täynnä: {username}")
        emit("join_rejected", {
            "reason": "Peli on täynnä. Odota seuraavaa erää tai avaa uusi peli."
        })
        return
    players[sid] = {
        "username": username,
        "reconnect_token": reconnect_token,
        "connected": True,
        "disconnected_at": None,
        "room_id": room_id
    }
    room.players = players
    assign_reconnect_token_to_room(reconnect_token, room_id)
    sync_default_room_from_globals()
    print(f"[INFO] {username} liittyi peliin.")
    payload = build_lobby_payload()
    payload["username"] = username
    socketio.emit("player_joined", payload)
    if theme_selection_active():
        emit_theme_selection_state()


@socketio.on("request_lobby_state")
def handle_request_lobby_state():
    emit("lobby_state", build_lobby_payload())
    if theme_selection_active():
        emit("theme_selection_updated", build_theme_selection_payload())


@socketio.on("leave_game")
def handle_leave_game(data=None):
    global turn, player_points
    room = get_default_room()
    data = data or {}
    sid, player_info = resolve_player_for_event(data)
    if not player_info:
        return {"ok": False}

    username = player_info.get("username", "Unknown")
    reconnect_token = player_info.get("reconnect_token")

    for existing_sid, info in list(players.items()):
        if existing_sid == sid or (reconnect_token and info.get("reconnect_token") == reconnect_token):
            clear_reconnect_token_room(info.get("reconnect_token"))
            del players[existing_sid]
    room.players = players

    print(f"[INFO] {username} poistui pelistÃ¤ kÃ¤yttÃ¤jÃ¤n pyynnÃ¶stÃ¤.")
    sync_default_room_from_globals()
    payload = build_lobby_payload()
    payload["username"] = username
    socketio.emit("player_joined", payload)

    if not get_effective_human_player_items():
        remove_bot_players()

    if get_effective_player_count() < 2 and (theme_selection_active() or grid_data or 'pending_pair' in globals()):
        print("[INFO] Pelaaja poistui pelistÃ¤ kesken erÃ¤n â€“ keskeytetÃ¤Ã¤n nykyinen pelitila")
        socketio.emit("game_aborted", {"reason": "Toinen pelaaja poistui. Peli keskeytetty."})
        grid_data.clear()
        revealed_cards.clear()
        matched_indices.clear()
        turn = 0
        player_points.clear()
        reset_pending_state()

    if len(players) == 0:
        print("[INFO] Kaikki pelaajat poistuneet â€“ nollataan pelitila")
        grid_data.clear()
        revealed_cards.clear()
        matched_indices.clear()
        turn = 0
        player_points.clear()
        reset_pending_state()

    return {"ok": True}

@socketio.on("start_game_clicked")
def handle_start_game():
    if get_effective_player_count() == max_players:
        print("[INFO] Molemmat pelaajat liittyneet, aloitetaan peli")
        # Luo pelidata jo tÃ¤ssÃ¤
        generate_grid()
        emit("start_game", broadcast=True)
    else:
        print("[WARNING] Pelaajia ei ole tarpeeksi pelin aloittamiseen")


@socketio.on("request_grid")
def handle_grid_request():
    global player_order, turn
    sync_globals_from_default_room()
    debug("[DEBUG] request_grid vastaanotettu â€“ tarkistetaan pelitila")
    debug(f"[DEBUG] Pelaajat: {players}")
    debug(f"[DEBUG] Grid sisÃ¤ltÃ¤Ã¤ {len(grid_data)} korttia")

    if get_effective_player_count() < 2:
        print("[WARNING] Ei tarpeeksi pelaajia ruudukon palauttamiseen.")
        emit("no_grid", {"reason": "Pelaajia liian vÃ¤hÃ¤n"})
        return

    # Jos ruudukko ei ole valmis, ilmoita siitÃ¤ ja tarvittaessa toista kÃ¤ynnissÃ¤ oleva sanapyyntÃ¶
    if not grid_data or len(grid_data) < 16:
        debug("[DEBUG] Ruudukko ei ole valmis â€“ ei lÃ¤hetetÃ¤ init_grid")
        emit("no_grid", {"reason": "Grid ei valmis"})
        if theme_selection_active():
            emit("theme_selection_updated", build_theme_selection_payload())
            return
        # Jos custom-pelin sanojen kysely on kÃ¤ynnissÃ¤, toista viimeisin pyyntÃ¶
        if 'pending_pair' in globals():
            try:
                if current_game_mode in {"theme", "spanish"}:
                    emit(
                        "theme_generation_started",
                        {
                            "theme": pending_theme,
                            "pair": pending_pair + 1,
                            "mode": current_game_mode,
                            "starter_name": pending_player or (player_order[0] if player_order else None),
                            "progress_count": pending_pair,
                            "total_pairs": 8
                        },
                        broadcast=True
                    )
                    socketio.sleep(0)
                    return
                if len(player_order) < 1:
                    # Fallback jÃ¤rjestys
                    player_order[:] = [v["username"] for v in get_effective_players_ordered()]
                target_player = pending_player or get_first_human_player_name() or (player_order[0] if player_order else None)
                if target_player is not None and pending_pair < 8:
                    debug(f"[DEBUG] request_grid: toistetaan ask_for_word pelaajalle {target_player}, pari {pending_pair+1}")
                    emit("ask_for_word", {"player": target_player, "pair": pending_pair + 1}, broadcast=True)
            except Exception as e:
                debug(f"[DEBUG] request_grid: ask_for_word toisto epÃ¤onnistui: {e}")
        return

    # Muodosta samassa muodossa kuin custom-pelissÃ¤
    if not player_order:
        # Fallback: kÃ¤ytÃ¤ liittymisjÃ¤rjestystÃ¤ players-sanakirjasta
        player_order = [v["username"] for v in get_effective_players_ordered()]
    current_turn_name = player_order[turn] if player_order and 0 <= turn < len(player_order) else (player_order[0] if player_order else None)
    emit("init_grid", {
        "cards": grid_data,
        "turn": current_turn_name,
        "players": player_order
    })
    schedule_bot_turn_if_needed()

@socketio.on("ready_for_game")
def handle_ready_for_game():
    debug("[DEBUG] Client ilmoitti olevansa valmis peliin")
    emit("start_game", broadcast=True)

def generate_grid():
    global grid_data, revealed_cards, matched_indices, turn, player_points, player_order
    room = get_default_room()
    print("[INFO] Peli kÃ¤ynnistyy â€“ luodaan ruudukko")
    images = []
    for filename in sorted(os.listdir("static/images")):
        if filename.endswith(".jpg") or filename.endswith(".png"):
            word = filename.split("_")[0]
            path = f"/static/images/{filename}"
            images.append({"image": path, "word": word})

    # ðŸ‘‰ Ryhmittele sanojen mukaan
    word_dict = {}
    for item in images:
        word = item["word"]
        word_dict.setdefault(word, []).append(item)

    # ðŸ‘‰ Valitse 8 satunnaista sanaa, joilla on vÃ¤hintÃ¤Ã¤n 2 kuvaa
    valid_pairs = [v for v in word_dict.values() if len(v) >= 2]
    selected = random.sample(valid_pairs, 8)
    selected_images = []
    for pair in selected:
        # Ota vain 2 kuvaa per sana
        selected_images.extend(pair[:2])
    random.shuffle(selected_images)
    grid_data = selected_images[:16]  # Varmista ettÃ¤ kortteja on tasan 16
    revealed_cards = []
    matched_indices = set()
    # Aseta vuorot ja pisteet kÃ¤yttÃ¤jien nimien mukaan
    player_order = [v["username"] for v in get_effective_players_ordered()]
    turn = 0
    player_points = {name: 0 for name in player_order}
    room.grid_data = grid_data
    room.revealed_cards = revealed_cards
    room.matched_indices = matched_indices
    room.player_order = player_order
    room.turn = turn
    room.player_points = player_points
    room.status = "playing"
    sync_default_room_from_globals()
    debug(f"[DEBUG] Kortteja yhteensÃ¤: {len(grid_data)}")


           


def process_card_click(index, resolved_sid, clicker):
    global revealed_cards, turn, matched_indices, player_order, player_points, current_click_sid
    sync_globals_from_default_room()
    clicker_name = (clicker or {}).get("username")
    current_player_name = player_order[turn] if player_order and 0 <= turn < len(player_order) else None
    if not clicker_name or clicker_name != current_player_name:
        debug(f"[DEBUG] Hylattiin klikkaus ei-aktiiviselta pelaajalta: {clicker_name} (vuoro: {current_player_name})")
        return
    if index in matched_indices or index in revealed_cards:
        return

    debug(f"[DEBUG] Kortti klikattu: index {index}, sana: {grid_data[index]['word']}")
    # Ensure both clicks of a pair come from the same client
    if len(revealed_cards) == 0:
        current_click_sid = resolved_sid
    elif len(revealed_cards) == 1 and current_click_sid != resolved_sid:
        debug("[DEBUG] Hylattiin toisen kortin klikkaus eri asiakkaalta samalle parille")
        return
    revealed_cards.append(index)
    sync_default_room_from_globals()
    socketio.emit("reveal_card", {
        "index": index,
        "card": grid_data[index]
    })

    if len(revealed_cards) == 2:
        idx1, idx2 = revealed_cards
        word1 = grid_data[idx1]["word"]
        word2 = grid_data[idx2]["word"]
        match_key1 = grid_data[idx1].get("pair_id", word1)
        match_key2 = grid_data[idx2].get("pair_id", word2)

        if match_key1 == match_key2:
            matched_indices.update(revealed_cards)
            debug(f"[DEBUG] Pari lÃ¶ytyi: {word1}")
            socketio.emit("pair_found", {"indices": revealed_cards, "word": word1})
            revealed_cards = []
            current_click_sid = None
            # Piste tÃ¤lle vuorossa olevalle pelaajalle
            current_player_name = player_order[turn] if 0 <= turn < len(player_order) else None
            if current_player_name is not None:
                player_points[current_player_name] = player_points.get(current_player_name, 0) + 1
                debug(f"[DEBUG] Piste {current_player_name} (+1). Pisteet nyt: {player_points}")
            sync_default_room_from_globals()

            # Jos kaikki parit lÃ¶ytyneet, pÃ¤Ã¤tÃ¤ peli kerran
            if len(matched_indices) == len(grid_data):
                print("[INFO] Kaikki parit lÃ¶ytyneet â€“ peli ohi!")
                # MÃ¤Ã¤ritÃ¤ voittaja tai tasapeli pisteiden perusteella
                if player_points:
                    max_pts = max(player_points.values()) if player_points else 0
                    winners = [n for n, p in player_points.items() if p == max_pts]
                    if len(winners) == 1:
                        winner_label = winners[0]
                    else:
                        winner_label = "Tasapeli"
                        print(f"[INFO] Tasapeli. Pisteet: {player_points}")
                    conclude_round(winner_label)
                else:
                    print("[ERROR] Ei voittajaa, player_points on tyhjÃ¤Ã¤.")

        else:
            debug(f"[DEBUG] Ei paria: {word1} vs {word2}")
            indices_to_hide = list(revealed_cards)  # Tee kopio!
            revealed_cards = []
            current_click_sid = None
            sync_default_room_from_globals()

            def hide_later():
                socketio.sleep(2)  # Odota 2 sekuntia
                socketio.emit("hide_cards", {"indices": indices_to_hide})
            
            socketio.start_background_task(hide_later)
            # Vuoro vaihtuu vasta epÃ¤onnistuneen parin jÃ¤lkeen
            if player_order:
                turn = (turn + 1) % len(player_order)
            sync_default_room_from_globals()
        
        # KÃ¤ytÃ¤ player_order vuoron nÃ¤yttÃ¤miseen
        next_turn_name = player_order[turn] if player_order else None
        debug(f"[DEBUG] Vuoro nyt: {next_turn_name}")
        socketio.emit("update_turn", {"turn": next_turn_name})
        schedule_bot_turn_if_needed()


@socketio.on("card_clicked")
def handle_card_click(data):
    index = data["index"]
    resolved_sid, clicker = resolve_player_for_event(data)
    process_card_click(index, resolved_sid, clicker)

@socketio.on("disconnect")
def on_disconnect():
    global players
    room = get_default_room()
    sid = request.sid
    if sid in players:
        username = players[sid]["username"]
        reconnect_token = players[sid].get("reconnect_token")
        players[sid]["connected"] = False
        print(f"[INFO] {username} poistui, odotetaan mahdollista reconnectia ({RECONNECT_GRACE_SECONDS} s)...")
        players[sid]["disconnected_at"] = time.monotonic()
        sync_default_room_from_globals()
        payload = build_lobby_payload()
        payload["username"] = username
        socketio.emit("player_joined", payload)
        def remove_later(sid_to_remove, username, expected_token):
            global turn, player_points
            eventlet.sleep(RECONNECT_GRACE_SECONDS)
            if sid_to_remove in players:
                current_token = players[sid_to_remove].get("reconnect_token")
                if current_token != expected_token:
                    print(f"[INFO] {username} reconnectasi uudella SID:llÃ¤, vanhaa istuntoa ei poisteta")
                    return
                print(f"[INFO] {username} poistetaan pelaajalistasta (ei reconnectia)")
                clear_reconnect_token_room(players[sid_to_remove].get("reconnect_token"))
                del players[sid_to_remove]
                room.players = players
                if not get_effective_human_player_items():
                    remove_bot_players()
                sync_default_room_from_globals()
                payload = build_lobby_payload()
                payload["username"] = username
                socketio.emit("player_joined", payload)
                if get_effective_player_count() < 2 and (theme_selection_active() or grid_data or 'pending_pair' in globals()):
                    print("[INFO] Pelaajia liian vÃ¤hÃ¤n keskenerÃ¤iseen erÃ¤Ã¤n â€“ keskeytetÃ¤Ã¤n nykyinen pelitila")
                    socketio.emit("game_aborted", {"reason": "Toinen pelaaja poistui. Peli keskeytetty."})
                    grid_data.clear()
                    revealed_cards.clear()
                    matched_indices.clear()
                    turn = 0
                    player_points.clear()
                    reset_pending_state()
                    return
                # Jos kaikki pelaajat poistuneet, nollaa pelitila
                if len(players) == 0:
                    print("[INFO] Kaikki pelaajat poistuneet â€“ nollataan pelitila")
                    grid_data.clear()
                    revealed_cards.clear()
                    matched_indices.clear()
                    turn = 0
                    player_points.clear()
                    reset_pending_state()
        socketio.start_background_task(remove_later, sid, username, reconnect_token)
    else:
        debug(f"[DEBUG] Tuntematon SID {sid} poistui pelistÃ¤")

@socketio.on("start_custom_game")
def handle_start_custom_game(data=None):
    global pending_words, pending_player, pending_pair, grid_data, player_order, current_game_mode, pending_theme, pending_search_theme, theme_candidates, theme_rejected_words, current_ui_language
    sync_globals_from_default_room()
    data = data or {}
    room = get_default_room()
    print(f"[INFO] Uusi erÃ¤ kÃ¤ynnistetÃ¤Ã¤n. Tila: mode={data.get('mode', 'manual')}, players={get_effective_player_count()}")
    # Jos peli on jo kÃ¤ynnissÃ¤, mutta pelaajien reconnect_tokenit ovat vaihtuneet, nollaa peli
    current_tokens = set(v['reconnect_token'] for v in get_effective_players_ordered())
    grid_tokens = getattr(handle_start_custom_game, 'last_tokens', set())
    if grid_data and current_tokens != grid_tokens:
        print("[INFO] Pelaajien reconnect-tokenit vaihtuneet â€“ nollataan pelitila")
        grid_data.clear()
        revealed_cards.clear()
        matched_indices.clear()
        global turn, player_points
        turn = 0
        player_points.clear()
        reset_pending_state()
    # Tallenna nykyiset reconnect_tokenit seuraavaa vertailua varten
    handle_start_custom_game.last_tokens = set(v['reconnect_token'] for v in get_effective_players_ordered())
    # Tallenna pelaajien jÃ¤rjestys kun peli alkaa
    player_order = [v["username"] for v in get_effective_players_ordered()]
    room.player_order = player_order
    if grid_data:  # Jos peli on jo kÃ¤ynnissÃ¤, Ã¤lÃ¤ aloita uutta
        print("[WARNING] Uuden erÃ¤n pyyntÃ¶ hylÃ¤tty: peli on jo kÃ¤ynnissÃ¤")
        return

    mode = str(data.get("mode", "manual")).strip().lower()
    theme = str(data.get("theme", "")).strip()
    ui_language = str(data.get("ui_language", "")).strip().lower()
    if mode not in {"manual", "theme", "spanish"}:
        mode = "manual"
    current_ui_language = ui_language if ui_language in {"fi", "en"} else "en"
    if mode in {"theme", "spanish"} and not theme:
        emit("game_setup_error", {"reason": "Teema puuttuu."}, broadcast=True)
        return

    print("[INFO] Aloitetaan sanojen keruu")
    pending_words = []
    pending_player = None
    pending_pair = 0
    grid_data.clear()
    current_game_mode = mode
    pending_theme = theme if mode in {"theme", "spanish"} else None
    pending_search_theme = translate_theme_to_english(theme, ui_language) if mode in {"theme", "spanish"} else None
    room.status = "setup"
    room.game_mode = current_game_mode
    room.ui_language = current_ui_language
    sync_default_room_from_globals()
    theme_candidates = []
    theme_rejected_words = set()
    if current_game_mode in {"theme", "spanish"}:
        starter_name = players.get(request.sid, {}).get("username")
        socketio.emit("theme_generation_started", {
            "theme": pending_theme,
            "pair": pending_pair + 1,
            "mode": current_game_mode,
            "starter_name": starter_name,
            "phase": "finding_words",
            "progress_count": pending_pair,
            "total_pairs": 8
        })
        socketio.sleep(0)
        prepare_theme_selection(starter_name)
        return
    ask_next_word()

def ask_next_word():
    global pending_pair, pending_player, player_order, player_points, matched_indices, revealed_cards, turn, current_game_mode
    sync_globals_from_default_room()
    debug(f"[DEBUG] ask_next_word kutsuttu! pending_pair: {pending_pair}, grid_data: {len(grid_data)}, mode: {current_game_mode}")
    # Jos kaikki 8 paria on annettu, lÃ¤hetÃ¤ ruudukko nÃ¤kyviin
    if pending_pair >= 8:
        print("[INFO] Kaikki sanat annettu, peli voidaan aloittaa")
        deactivate_theme_selection()
        globals().pop('pending_player', None)
        globals().pop('pending_pair', None)
        launch_grid_round()
        return

    if len(player_order) < 2:
        print("[WARNING] Pelaajia liian vÃ¤hÃ¤n, peli keskeytetÃ¤Ã¤n")
        socketio.emit("game_aborted", {"reason": "Toinen pelaaja poistui. Peli keskeytetty."})
        return
    # Bot-tiputuksessa pyydetään sanat aina ensimmäiseltä ihmispelaajalta.
    first_player = get_first_human_player_name() or (player_order[0] if player_order else None)
    pending_player = first_player
    sync_default_room_from_globals()
    if current_game_mode == "theme":
        if theme_selection_active():
            emit_theme_selection_state()
            return
        print(f"[INFO] Generoidaan teemasanat teemalle '{pending_theme}'")
        socketio.emit("theme_generation_started", {
            "theme": pending_theme,
            "pair": pending_pair + 1,
            "mode": "theme",
            "starter_name": first_player,
            "phase": "drawing_cards",
            "progress_count": pending_pair,
            "total_pairs": 8
        })
        socketio.sleep(0)
        global theme_generation_in_progress
        if theme_generation_in_progress:
            debug("[DEBUG] Teemagenerointi on jo kaynnissa, ei kaynnisteta toista taustatehtavaa")
            return
        theme_generation_in_progress = True
        socketio.start_background_task(generate_theme_pair)
        return
    if current_game_mode == "spanish":
        if theme_selection_active():
            emit_theme_selection_state()
            return
        print(f"[INFO] Generoidaan espanjan opiskelupeli teemalle '{pending_theme}'")
        socketio.emit("theme_generation_started", {
            "theme": pending_theme,
            "pair": pending_pair + 1,
            "mode": "spanish",
            "starter_name": first_player,
            "phase": "drawing_cards",
            "progress_count": pending_pair,
            "total_pairs": 8
        })
        socketio.sleep(0)
        global spanish_generation_in_progress
        if spanish_generation_in_progress:
            debug("[DEBUG] Espanjan generointi on jo kaynnissa, ei kaynnisteta toista taustatehtavaa")
            return
        spanish_generation_in_progress = True
        socketio.start_background_task(generate_spanish_learning_pairs, pending_theme)
        return
    print(f"[INFO] PyydetÃ¤Ã¤n sana pelaajalta {first_player}, pari {pending_pair+1}")
    socketio.emit("ask_for_word", {
        "player": first_player,
        "pair": pending_pair + 1
    })


@socketio.on("select_theme_word")
def handle_select_theme_word(data):
    sync_globals_from_default_room()
    if current_game_mode not in {"theme", "spanish"} or not theme_selection_active():
        emit("theme_selection_failed", {"reason": "selection_inactive"})
        return

    _, player_info = resolve_player_for_event(data)
    if not player_info:
        emit("theme_selection_failed", {"reason": "player_missing"})
        return
    sync_theme_selection_players()

    username = player_info["username"]
    word = normalize_candidate_word((data or {}).get("word"))
    replace_word = normalize_candidate_word((data or {}).get("replace_word"))
    if not word:
        emit("theme_selection_failed", {"reason": "invalid_word"})
        return

    counts = theme_selection_state.get("counts", {})
    ready = theme_selection_state.get("ready", {})
    selected_words = theme_selection_state.get("selected_words", [])
    rejected_words = theme_selection_state.get("rejected_words", [])
    candidates = theme_selection_state.get("candidates", [])
    swap_limit = int(theme_selection_state.get("swap_limit", 4))

    if counts.get(username, 0) >= swap_limit:
        emit("theme_selection_failed", {"reason": "quota_full"})
        return
    if not replace_word:
        emit("theme_selection_failed", {"reason": "replace_missing"})
        return
    if word not in candidates:
        emit("theme_selection_failed", {"reason": "unknown_word"})
        return
    selected_index = next((index for index, item in enumerate(selected_words) if item.get("word") == replace_word), -1)
    if selected_index < 0:
        emit("theme_selection_failed", {"reason": "replace_missing"})
        return
    if any(item.get("word") == word for item in selected_words) or word in rejected_words:
        emit("theme_selection_failed", {"reason": "word_unavailable"})
        return

    mode_label = "teemasanan" if current_game_mode == "theme" else "espanjapelin sanan"
    print(f"[INFO] {username} vaihtaa {mode_label}n '{replace_word}' -> '{word}' paikalle {selected_index + 1}")
    try:
        selection_ok = build_pair_entry_for_mode(word, selected_index)
    except PixabayConfigError as e:
        abort_round_due_to_pixabay_error(str(e))
        return
    if not selection_ok:
        print(f"[INFO] Sana '{word}' hylÃ¤ttiin tilassa '{current_game_mode}'")
        rejected_words.append(word)
        theme_rejected_words.add(word)
        socketio.emit("theme_selection_updated", build_theme_selection_payload(
            message=f"word_rejected:{word}"
        ))
        emit("theme_selection_failed", {"reason": "image_missing", "word": word})
        return

    previous_item = selected_words[selected_index]
    previous_word = previous_item.get("word")
    next_display_word = get_theme_display_word(word, selection_ok)
    next_display_key = normalize_display_label(next_display_word)
    for index, item in enumerate(selected_words):
        if index == selected_index:
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
        ready[player_name] = is_bot_player(player_name)
    ready[username] = True
    theme_selection_state["candidates"] = [candidate for candidate in candidates if candidate != word]
    if previous_word and previous_word not in rejected_words and previous_word not in theme_selection_state["candidates"]:
        theme_selection_state["candidates"].append(previous_word)
    candidate_labels = theme_selection_state.setdefault("candidate_labels", {})
    candidate_labels.pop(word, None)
    if previous_word and previous_word not in rejected_words:
        candidate_labels[previous_word] = previous_item.get("display_word") or get_theme_display_word(previous_word, previous_item.get("entry"))
    sync_default_room_from_globals()
    emit_theme_selection_state(message=f"word_swapped:{previous_word}:{word}")
    if player_order and all(ready.get(name, False) for name in player_order):
        print(f"[INFO] Kaikki pelaajat valmiina vaihdon jälkeen – aloitetaan {current_game_mode}-erä teemalla '{pending_theme}'")
        finalize_theme_selection()


@socketio.on("set_theme_ready")
def handle_set_theme_ready(data):
    sync_globals_from_default_room()
    if current_game_mode not in {"theme", "spanish"} or not theme_selection_active():
        emit("theme_selection_failed", {"reason": "selection_inactive"})
        return

    _, player_info = resolve_player_for_event(data)
    if not player_info:
        emit("theme_selection_failed", {"reason": "player_missing"})
        return
    sync_theme_selection_players()

    username = player_info["username"]
    ready = theme_selection_state.get("ready", {})
    ready[username] = bool((data or {}).get("ready", True))
    sync_default_room_from_globals()
    emit_theme_selection_state(message=f"ready:{username}" if ready[username] else f"unready:{username}")
    if player_order and all(ready.get(name, False) for name in player_order):
        print(f"[INFO] Kaikki pelaajat valmiina â€“ aloitetaan {current_game_mode}-erÃ¤ teemalla '{pending_theme}'")
        finalize_theme_selection()

@socketio.on("word_given")
def handle_word_given(data):
    global pending_pair, pending_player, grid_data
    sync_globals_from_default_room()
    if pending_pair >= 8:
        debug(f"[DEBUG] word_given hylÃ¤tty, kaikki parit jo annettu (pending_pair={pending_pair})")
        return
    sender_name = (players.get(request.sid) or {}).get("username")
    expected_player = pending_player or (player_order[0] if player_order else None)
    if expected_player and sender_name != expected_player:
        debug(f"[DEBUG] word_given hylatty vaaralta pelaajalta: sender={sender_name}, expected={expected_player}")
        return
    word = normalize_candidate_word(data["word"])
    if not word:
        print("[WARNING] KÃ¤yttÃ¤jÃ¤n sana ei kelpaa, pyydetÃ¤Ã¤n uusi sana")
        emit("word_failed", {
            "player": expected_player,
            "pair": pending_pair + 1
        }, broadcast=True)
        return
    pair_index = pending_pair
    print(f"[INFO] Vastaanotettu sana '{word}' parille {pair_index+1}")
    try:
        image_append_ok = append_word_images_to_grid(word, pair_index)
    except PixabayConfigError as e:
        abort_round_due_to_pixabay_error(str(e))
        return
    if image_append_ok:
        pending_pair += 1
        sync_default_room_from_globals()
        ask_next_word()
    else:
        print(f"[WARNING] Pixabay ei lÃ¶ytÃ¤nyt kuvia sanalle '{word}', pyydetÃ¤Ã¤n uusi sana")
        emit("word_failed", {
            "player": expected_player,
            "pair": pending_pair + 1
        }, broadcast=True)


@socketio.on("surrender_round")
def handle_surrender_round(data=None):
    global current_click_sid
    if not grid_data or get_effective_player_count() < 2:
        emit("round_surrender_failed", {"reason": "round_not_active"})
        return

    _, player_info = resolve_player_for_event(data)
    if not player_info:
        emit("round_surrender_failed", {"reason": "player_missing"})
        return

    surrendering_player = player_info["username"]
    opponents = [name for name in player_order if name != surrendering_player]
    if not opponents:
        emit("round_surrender_failed", {"reason": "opponent_missing"})
        return

    winner = opponents[0]
    print(f"[INFO] {surrendering_player} luovutti tÃ¤mÃ¤n kierroksen. Voittaja: {winner}")
    current_click_sid = None
    conclude_round(winner, surrendered_by=surrendering_player)

@socketio.on("ask_for_word")
def handle_client_request_ask_for_word(data):
    # Client pyytÃ¤Ã¤ toistamaan saman parin kyselyn (esim. word_failed perÃ¤ssÃ¤)
    target_player = data.get("player")
    pair = int(data.get("pair", 0))
    debug(f"[DEBUG] Client pyysi ask_for_word uudestaan: player={target_player}, pair={pair}")
    # Pieni viive, jotta mahdollinen alert/prompt ei tÃ¶rmÃ¤Ã¤ seuraavaan prompttiin
    socketio.sleep(0.3)
    try:
        emit("ask_for_word", {"player": target_player, "pair": pair}, broadcast=True)
    except Exception as e:
        print(f"[ERROR] ask_for_word uudelleenlÃ¤hetys epÃ¤onnistui: {e}")

def fetch_and_save_pixabay_images(word, pair_index, required_count=2):
    global used_pixabay_image_ids
    debug(f"[DEBUG] Haetaan Pixabaysta kuvia sanalla: {word}")
    pixabay_api_key = (os.getenv("PIXABAY_API_KEY") or "").strip()
    if not pixabay_api_key:
        raise PixabayConfigError("Pixabay API key is missing. Check PIXABAY_API_KEY.")
        print("[ERROR] PIXABAY_API_KEY puuttuu. LisÃ¤Ã¤ avain .env-tiedostoon.")
        return None
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
                raise PixabayConfigError("Pixabay rejected the request. Check PIXABAY_API_KEY in Render and .env.")
            response.raise_for_status()
        except requests.RequestException as e:
            print(f"[ERROR] Pixabay-pyyntÃ¶ epÃ¤onnistui: {e}")
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

    selected_hits = []
    skipped_duplicates = 0
    local_ids = set()

    for hit in hits:
        image_id = hit.get("id")
        if image_id is None:
            continue
        if image_id in used_pixabay_image_ids or image_id in local_ids:
            skipped_duplicates += 1
            continue
        selected_hits.append(hit)
        local_ids.add(image_id)
        if len(selected_hits) >= max(required_count + 2, required_count):
            break

    if skipped_duplicates:
        print(f"[INFO] Ohitettiin {skipped_duplicates} jo kaytettya Pixabay-kuvaa sanalle '{word}'")

    if len(selected_hits) >= required_count:
        image_refs = []
        for hit in selected_hits:
            img_url = hit.get("webformatURL") or hit.get("previewURL") or hit.get("largeImageURL")
            if not img_url:
                continue
            image_refs.append(img_url)
            used_pixabay_image_ids.add(hit["id"])
            if len(image_refs) >= required_count:
                break
        if len(image_refs) >= required_count:
            return image_refs
        print(f"[INFO] Sanalle '{word}' ei loytynyt tarpeeksi kaytettavia Pixabay-kuvia")
        return None
    else:
        print(f"[INFO] Sanalle '{word}' ei loytynyt tarpeeksi uusia Pixabay-kuvia taman eran sisalla")
        debug(f"[DEBUG] Ei tarpeeksi kuvia sanalle: {word}")
        return None
if __name__ == "__main__":
    players.clear()
    port = int(os.getenv("PORT", 5000))
    print(f"[INFO] Muistipeli kaynnistyy portissa {port} (host=0.0.0.0)")
    socketio.run(app, host="0.0.0.0", port=port, debug=False, use_reloader=False)



