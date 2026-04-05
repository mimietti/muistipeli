import warnings

warnings.filterwarnings("ignore", message=".*Eventlet is deprecated.*")

import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
import random
import os
import re
import requests
from PIL import Image
from io import BytesIO
from dotenv import load_dotenv   # <-- TÄMÄ
from collections import defaultdict
load_dotenv()                   # <--

app = Flask(__name__)
# Salli yhteydet myös muista koneista/osoitteista (kehitystä varten)
socketio = SocketIO(
    app,
    async_mode='eventlet',
    cors_allowed_origins="*",
    logger=False,
    engineio_logger=False,
)

VERBOSE_DEBUG = False


def debug(message):
    if VERBOSE_DEBUG:
        print(message)

players = {}  # { sid: {"username": ..., "reconnect_token": ...} }
max_players = 2
grid_data = []
revealed_cards = []
matched_indices = set()
turn = 0
player_points = {}
round_win = defaultdict(int)
player_order = []  # Pelaajien järjestys
current_game_mode = "manual"
pending_theme = None
theme_candidates = []
theme_rejected_words = set()
used_pixabay_image_ids = set()

SPANISH_TRANSLATION_API_URL = "https://api.mymemory.translated.net/get"
ABSTRACT_THEME_WORDS = {
    "ability", "advice", "anger", "belief", "concept", "courage", "emotion", "faith",
    "freedom", "friendship", "future", "happiness", "hope", "idea", "justice",
    "knowledge", "logic", "love", "peace", "power", "quality", "spirit", "strategy",
    "strength", "success", "theory", "thought", "truth", "value", "vision", "wisdom"
}


def reset_pending_state():
    global current_game_mode, pending_theme, theme_candidates, theme_rejected_words, used_pixabay_image_ids
    globals().pop('pending_pair', None)
    globals().pop('pending_words', None)
    globals().pop('pending_player', None)
    current_game_mode = "manual"
    pending_theme = None
    theme_candidates = []
    theme_rejected_words = set()
    used_pixabay_image_ids = set()


def spanish_setup_still_active(theme):
    active_pair = globals().get('pending_pair')
    if active_pair is None:
        return False
    if current_game_mode != "spanish":
        return False
    if pending_theme != theme:
        return False
    if len(players) < 2:
        return False
    return True


def normalize_candidate_word(word):
    cleaned = str(word or "").strip().lower()
    if not re.fullmatch(r"[a-z]{3,15}", cleaned):
        return None
    return cleaned


def normalize_spanish_word(word):
    cleaned = str(word or "").strip().lower()
    cleaned = re.sub(r"^[^a-záéíóúüñ]+|[^a-záéíóúüñ]+$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(el|la|los|las|un|una|unos|unas)\s+", "", cleaned, flags=re.IGNORECASE)
    if not re.fullmatch(r"[a-záéíóúüñ]{2,18}", cleaned, flags=re.IGNORECASE):
        return None
    return cleaned


def is_concrete_theme_word(word):
    return word not in ABSTRACT_THEME_WORDS


def fetch_theme_words(theme, max_results=80, require_noun=False):
    theme = str(theme or "").strip()
    if not theme:
        return []

    print(f"[INFO] Haetaan Datamusesta teemasanat teemalle '{theme}'")
    all_words = []
    seen = set()
    query_variants = [
        {"ml": theme, "max": max_results, "md": "p"},
        {"topics": theme, "max": max_results, "md": "p"},
        {"rel_trg": theme, "max": max_results, "md": "p"},
    ]

    for params in query_variants:
        try:
            response = requests.get("https://api.datamuse.com/words", params=params, timeout=10)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as e:
            debug(f"[DEBUG] Datamuse-pyyntö epäonnistui ({params}): {e}")
            continue
        except ValueError as e:
            debug(f"[DEBUG] Datamuse palautti virheellistä JSONia ({params}): {e}")
            continue

        for item in payload:
            word = normalize_candidate_word(item.get("word"))
            if not word or word in seen:
                continue
            tags = item.get("tags") or []
            if require_noun and "n" not in tags:
                continue
            seen.add(word)
            all_words.append(word)

    random.shuffle(all_words)
    preview = ", ".join(all_words[:12]) if all_words else "(ei sanoja)"
    print(f"[INFO] Datamuse ehdotti teemalle '{theme}' {len(all_words)} sanaa: {preview}")
    return all_words


def translate_word(word, source_lang, target_lang):
    normalized_word = normalize_candidate_word(word)
    if not normalized_word:
        return None

    try:
        response = requests.get(
            SPANISH_TRANSLATION_API_URL,
            params={"q": normalized_word, "langpair": f"{source_lang}|{target_lang}"},
            timeout=10
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[WARNING] Kaannos epäonnistui sanalle '{normalized_word}' ({source_lang}->{target_lang}): {e}")
        return None

    translated_text = (
        (payload.get("responseData") or {}).get("translatedText")
        or ""
    )
    translated_text = re.sub(r"\s*\(.*?\)\s*", " ", translated_text).strip()
    if any(separator in translated_text for separator in [",", ";", "/", "|"]):
        return None

    normalized_translation = normalize_spanish_word(translated_text)
    if not normalized_translation:
        return None
    if normalize_candidate_word(normalized_translation) == normalized_word:
        return None
    return normalized_translation


def translate_word_to_spanish(word):
    return translate_word(word, "en", "es")


def translate_word_to_finnish(word):
    return translate_word(word, "en", "fi")


def append_word_images_to_grid(word, pair_index):
    global grid_data
    result = fetch_and_save_pixabay_images(word, pair_index)
    if not result:
        print(f"[INFO] Pixabay-haku epaonnistui sanalle '{word}'")
        return False
    print(f"[INFO] Pixabaysta loytyi kuvat sanalle '{word}': {result}")
    for path in result:
        grid_data.append({"image": "/" + path, "word": word})
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


def generate_theme_pair():
    global pending_pair, pending_theme, theme_candidates, theme_rejected_words
    pair_index = pending_pair
    existing_words = {item["word"] for item in grid_data}
    attempts = 0

    while attempts < 40:
        if not theme_candidates:
            theme_candidates = fetch_theme_words(pending_theme, max_results=100)
            if not theme_candidates:
                break

        word = theme_candidates.pop(0)
        if word in existing_words or word in theme_rejected_words:
            attempts += 1
            continue

        print(f"[INFO] Kokeillaan teemasanaksi '{word}' parille {pair_index+1}")
        if append_word_images_to_grid(word, pair_index):
            socketio.emit("theme_word_accepted", {
                "theme": pending_theme,
                "word": word,
                "pair": pair_index + 1,
                "total_pairs": 8
            })
            pending_pair += 1
            ask_next_word()
            return

        print(f"[INFO] Pixabay ei loytanyt teemasanalle '{word}' sopivia kuvia, haetaan korvaaja")
        theme_rejected_words.add(word)
        attempts += 1

    message = f"Teemasta '{pending_theme}' ei löytynyt tarpeeksi käyttökelpoisia sanoja. Kokeile toista teemaa."
    print(f"[WARNING] {message}")
    grid_data.clear()
    reset_pending_state()
    socketio.emit("game_setup_error", {"reason": message})


def generate_spanish_learning_pairs(theme, target_pairs=8):
    global pending_pair, pending_theme, theme_candidates, theme_rejected_words
    existing_words = {item.get("word") for item in grid_data if item.get("word")}
    attempts = 0
    max_attempts = 160

    while spanish_setup_still_active(theme) and pending_pair < target_pairs and attempts < max_attempts:
        if not theme_candidates:
            theme_candidates = fetch_theme_words(theme, max_results=160, require_noun=True)
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

        finnish_word = translate_word_to_finnish(english_word) or english_word

        if not spanish_setup_still_active(theme):
            print("[INFO] Espanjapelin generointi keskeytettiin, koska pelitila muuttui")
            return

        print(f"[INFO] Kokeillaan espanjapariksi '{english_word}' -> '{spanish_word}' parille {pending_pair + 1}")
        image_paths = fetch_and_save_pixabay_images(english_word, pending_pair, required_count=1)
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
        pending_pair += 1

    if not spanish_setup_still_active(theme):
        print("[INFO] Espanjapelin generointi lopetettiin siististi keskeytyneen pelin vuoksi")
        return

    if pending_pair >= target_pairs:
        ask_next_word()
        return

    message = f"Teemasta '{pending_theme}' ei löytynyt tarpeeksi käyttökelpoisia sanoja. Kokeile toista teemaa."
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


def build_lobby_payload():
    players_ordered = list(players.values())
    usernames = [v["username"] for v in players_ordered]
    infos = [{"username": v["username"], "reconnect_token": v.get("reconnect_token")}
             for v in players_ordered]
    last_token = infos[-1]["reconnect_token"] if infos else None
    return {
        "players": usernames,
        "players_info": infos,
        "last_joined_token": last_token
    }

@socketio.on("join")
def on_join(data):
    global players
    username = data["username"]
    sid = request.sid
    reconnect_token = data.get("reconnect_token")
    if not reconnect_token:
        print(f"[WARNING] HYLÄTTY join ilman reconnect_tokenia: {username}, SID: {sid}")
        return
    # Sama selain voi vaihtaa sivua tai nimeä; reconnect_token yksilöi istunnon.
    for k, v in list(players.items()):
        if v.get("reconnect_token") == reconnect_token:
            del players[k]
    if len(players) < max_players:
        players[sid] = {"username": username, "reconnect_token": reconnect_token}
        print(f"[INFO] {username} liittyi peliin.")
    payload = build_lobby_payload()
    payload["username"] = username
    socketio.emit("player_joined", payload)


@socketio.on("request_lobby_state")
def handle_request_lobby_state():
    emit("lobby_state", build_lobby_payload())

@socketio.on("start_game_clicked")
def handle_start_game():
    if len(players) == max_players:
        print("[INFO] Molemmat pelaajat liittyneet, aloitetaan peli")
        # Luo pelidata jo tässä
        generate_grid()
        emit("start_game", broadcast=True)
    else:
        print("[WARNING] Pelaajia ei ole tarpeeksi pelin aloittamiseen")


@socketio.on("request_grid")
def handle_grid_request():
    global player_order, turn
    debug("[DEBUG] request_grid vastaanotettu – tarkistetaan pelitila")
    debug(f"[DEBUG] Pelaajat: {players}")
    debug(f"[DEBUG] Grid sisältää {len(grid_data)} korttia")

    if len(players) < 2:
        print("[WARNING] Ei tarpeeksi pelaajia ruudukon palauttamiseen.")
        emit("no_grid", {"reason": "Pelaajia liian vähän"})
        return

    # Jos ruudukko ei ole valmis, ilmoita siitä ja tarvittaessa toista käynnissä oleva sanapyyntö
    if not grid_data or len(grid_data) < 16:
        debug("[DEBUG] Ruudukko ei ole valmis – ei lähetetä init_grid")
        emit("no_grid", {"reason": "Grid ei valmis"})
        # Jos custom-pelin sanojen kysely on käynnissä, toista viimeisin pyyntö
        if 'pending_pair' in globals():
            try:
                if current_game_mode in {"theme", "spanish"}:
                    emit(
                        "theme_generation_started",
                        {"theme": pending_theme, "pair": pending_pair + 1, "mode": current_game_mode},
                        broadcast=True
                    )
                    return
                if len(player_order) < 1:
                    # Fallback järjestys
                    player_order[:] = [v["username"] for v in players.values()]
                first_player = player_order[0] if player_order else None
                if first_player is not None and pending_pair < 8:
                    debug(f"[DEBUG] request_grid: toistetaan ask_for_word pelaajalle {first_player}, pari {pending_pair+1}")
                    emit("ask_for_word", {"player": first_player, "pair": pending_pair + 1}, broadcast=True)
            except Exception as e:
                debug(f"[DEBUG] request_grid: ask_for_word toisto epäonnistui: {e}")
        return

    # Muodosta samassa muodossa kuin custom-pelissä
    if not player_order:
        # Fallback: käytä liittymisjärjestystä players-sanakirjasta
        player_order = [v["username"] for v in players.values()]
    current_turn_name = player_order[turn] if player_order and 0 <= turn < len(player_order) else (player_order[0] if player_order else None)
    emit("init_grid", {
        "cards": grid_data,
        "turn": current_turn_name,
        "players": player_order
    })

@socketio.on("ready_for_game")
def handle_ready_for_game():
    debug("[DEBUG] Client ilmoitti olevansa valmis peliin")
    emit("start_game", broadcast=True)

def generate_grid():
    global grid_data, revealed_cards, matched_indices, turn, player_points, player_order
    print("[INFO] Peli käynnistyy – luodaan ruudukko")
    images = []
    for filename in sorted(os.listdir("static/images")):
        if filename.endswith(".jpg") or filename.endswith(".png"):
            word = filename.split("_")[0]
            path = f"/static/images/{filename}"
            images.append({"image": path, "word": word})

    # 👉 Ryhmittele sanojen mukaan
    word_dict = {}
    for item in images:
        word = item["word"]
        word_dict.setdefault(word, []).append(item)

    # 👉 Valitse 8 satunnaista sanaa, joilla on vähintään 2 kuvaa
    valid_pairs = [v for v in word_dict.values() if len(v) >= 2]
    selected = random.sample(valid_pairs, 8)
    selected_images = []
    for pair in selected:
        # Ota vain 2 kuvaa per sana
        selected_images.extend(pair[:2])
    random.shuffle(selected_images)
    grid_data = selected_images[:16]  # Varmista että kortteja on tasan 16
    revealed_cards = []
    matched_indices = set()
    # Aseta vuorot ja pisteet käyttäjien nimien mukaan
    player_order = [v["username"] for v in players.values()]
    turn = 0
    player_points = {name: 0 for name in player_order}
    debug(f"[DEBUG] Kortteja yhteensä: {len(grid_data)}")


           


@socketio.on("card_clicked")
def handle_card_click(data):
    global revealed_cards, turn, matched_indices, player_order, player_points

    index = data["index"]
    if index in matched_indices or index in revealed_cards:
        return

    debug(f"[DEBUG] Kortti klikattu: index {index}, sana: {grid_data[index]['word']}")
    revealed_cards.append(index)
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
            debug(f"[DEBUG] Pari löytyi: {word1}")
            socketio.emit("pair_found", {"indices": revealed_cards, "word": word1})
            revealed_cards = []
            # Piste tälle vuorossa olevalle pelaajalle
            current_player_name = player_order[turn] if 0 <= turn < len(player_order) else None
            if current_player_name is not None:
                player_points[current_player_name] = player_points.get(current_player_name, 0) + 1
                debug(f"[DEBUG] Piste {current_player_name} (+1). Pisteet nyt: {player_points}")

            # Jos kaikki parit löytyneet, päätä peli kerran
            if len(matched_indices) == len(grid_data):
                print("[INFO] Kaikki parit löytyneet – peli ohi!")
                # Määritä voittaja tai tasapeli pisteiden perusteella
                if player_points:
                    max_pts = max(player_points.values()) if player_points else 0
                    winners = [n for n, p in player_points.items() if p == max_pts]
                    if len(winners) == 1:
                        winner = winners[0]
                        round_win[winner] += 1
                        winner_label = winner
                        print(f"[INFO] Voittaja: {winner}. Erävoitot: {dict(round_win)}")
                    else:
                        winner_label = "Tasapeli"
                        print(f"[INFO] Tasapeli. Pisteet: {player_points}")

                    socketio.emit("game_over", {
                        "winner": winner_label,
                        "points": player_points,
                        "round_win": dict(round_win)
                    })
                else:
                    print("[ERROR] Ei voittajaa, player_points on tyhjää.")

                # Pyyhi pelitila, jotta uusi erä voi alkaa heti
                try:
                    grid_data.clear()
                    revealed_cards.clear()
                    matched_indices.clear()
                except Exception:
                    pass
                turn = 0
                player_points = {}
                reset_pending_state()

        else:
            debug(f"[DEBUG] Ei paria: {word1} vs {word2}")
            indices_to_hide = list(revealed_cards)  # Tee kopio!
            revealed_cards = []

            def hide_later():
                socketio.sleep(2)  # Odota 2 sekuntia
                socketio.emit("hide_cards", {"indices": indices_to_hide})
            
            socketio.start_background_task(hide_later)
            # Vuoro vaihtuu vasta epäonnistuneen parin jälkeen
            if player_order:
                turn = (turn + 1) % len(player_order)
        
        # Käytä player_order vuoron näyttämiseen
        next_turn_name = player_order[turn] if player_order else None
        debug(f"[DEBUG] Vuoro nyt: {next_turn_name}")
        socketio.emit("update_turn", {"turn": next_turn_name})

@socketio.on("disconnect")
def on_disconnect():
    global players
    sid = request.sid
    if sid in players:
        username = players[sid]["username"]
        reconnect_token = players[sid].get("reconnect_token")
        print(f"[INFO] {username} poistui, odotetaan mahdollista reconnectia...")
        def remove_later(sid_to_remove, username, expected_token):
            eventlet.sleep(8)  # Odota 8 sekuntia reconnectia
            if sid_to_remove in players:
                current_token = players[sid_to_remove].get("reconnect_token")
                if current_token != expected_token:
                    print(f"[INFO] {username} reconnectasi uudella SID:llä, vanhaa istuntoa ei poisteta")
                    return
                print(f"[INFO] {username} poistetaan pelaajalistasta (ei reconnectia)")
                del players[sid_to_remove]
                payload = build_lobby_payload()
                payload["username"] = username
                socketio.emit("player_joined", payload)
                # Jos kaikki pelaajat poistuneet, nollaa pelitila
                if len(players) == 0:
                    print("[INFO] Kaikki pelaajat poistuneet – nollataan pelitila")
                    grid_data.clear()
                    revealed_cards.clear()
                    matched_indices.clear()
                    global turn, player_points
                    turn = 0
                    player_points.clear()
                    reset_pending_state()
        socketio.start_background_task(remove_later, sid, username, reconnect_token)
    else:
        debug(f"[DEBUG] Tuntematon SID {sid} poistui pelistä")

@socketio.on("start_custom_game")
def handle_start_custom_game(data=None):
    global pending_words, pending_player, pending_pair, grid_data, player_order, current_game_mode, pending_theme, theme_candidates, theme_rejected_words
    data = data or {}
    print(f"[INFO] Uusi erä käynnistetään. Tila: mode={data.get('mode', 'manual')}, players={len(players)}")
    # Jos peli on jo käynnissä, mutta pelaajien reconnect_tokenit ovat vaihtuneet, nollaa peli
    current_tokens = set(v['reconnect_token'] for v in players.values())
    grid_tokens = getattr(handle_start_custom_game, 'last_tokens', set())
    if grid_data and current_tokens != grid_tokens:
        print("[INFO] Pelaajien reconnect-tokenit vaihtuneet – nollataan pelitila")
        grid_data.clear()
        revealed_cards.clear()
        matched_indices.clear()
        global turn, player_points
        turn = 0
        player_points.clear()
        reset_pending_state()
    # Tallenna nykyiset reconnect_tokenit seuraavaa vertailua varten
    handle_start_custom_game.last_tokens = set(v['reconnect_token'] for v in players.values())
    # Tallenna pelaajien järjestys kun peli alkaa
    player_order = [v["username"] for v in players.values()]
    if grid_data:  # Jos peli on jo käynnissä, älä aloita uutta
        print("[WARNING] Uuden erän pyyntö hylätty: peli on jo käynnissä")
        return

    mode = str(data.get("mode", "manual")).strip().lower()
    theme = str(data.get("theme", "")).strip()
    if mode not in {"manual", "theme", "spanish"}:
        mode = "manual"
    if mode in {"theme", "spanish"} and not theme:
        emit("game_setup_error", {"reason": "Teema puuttuu."}, broadcast=True)
        return

    print("[INFO] Aloitetaan sanojen keruu")
    pending_words = []
    pending_player = 0
    pending_pair = 0
    grid_data.clear()
    current_game_mode = mode
    pending_theme = theme if mode in {"theme", "spanish"} else None
    theme_candidates = []
    theme_rejected_words = set()
    if current_game_mode in {"theme", "spanish"}:
        socketio.emit("theme_generation_started", {"theme": pending_theme, "mode": current_game_mode})
    ask_next_word()

def ask_next_word():
    global pending_pair, player_order, player_points, matched_indices, revealed_cards, turn, current_game_mode
    debug(f"[DEBUG] ask_next_word kutsuttu! pending_pair: {pending_pair}, grid_data: {len(grid_data)}, mode: {current_game_mode}")
    # Jos kaikki 8 paria on annettu, lähetä ruudukko näkyviin
    if pending_pair >= 8:
        print("[INFO] Kaikki sanat annettu, peli voidaan aloittaa")
        # Alusta pelitila ennen ruudukon lähetystä
        matched_indices = set()
        revealed_cards = []
        turn = 0
        player_points = {name: 0 for name in player_order}
        random.shuffle(grid_data)
        socketio.emit("init_grid", {
            "cards": grid_data,
            "turn": player_order[0] if player_order else None,
            "players": player_order
        })
        return

    if len(player_order) < 2:
        print("[WARNING] Pelaajia liian vähän, peli keskeytetään")
        socketio.emit("game_aborted", {"reason": "Toinen pelaaja poistui. Peli keskeytetty."})
        return
    # Kysy aina ekalta pelaajalta (player_order[0])
    first_player = player_order[0]
    if current_game_mode == "theme":
        print(f"[INFO] Generoidaan teemasanat teemalle '{pending_theme}'")
        socketio.emit("theme_generation_started", {"theme": pending_theme, "pair": pending_pair + 1, "mode": "theme"})
        socketio.start_background_task(generate_theme_pair)
        return
    if current_game_mode == "spanish":
        print(f"[INFO] Generoidaan espanjan opiskelupeli teemalle '{pending_theme}'")
        socketio.emit("theme_generation_started", {"theme": pending_theme, "pair": pending_pair + 1, "mode": "spanish"})
        socketio.start_background_task(generate_spanish_learning_pairs, pending_theme)
        return

    print(f"[INFO] Pyydetään sana pelaajalta {first_player}, pari {pending_pair+1}")
    socketio.emit("ask_for_word", {
        "player": first_player,
        "pair": pending_pair + 1
    })

@socketio.on("word_given")
def handle_word_given(data):
    global pending_pair, grid_data
    if pending_pair >= 8:
        debug(f"[DEBUG] word_given hylätty, kaikki parit jo annettu (pending_pair={pending_pair})")
        return
    word = normalize_candidate_word(data["word"])
    if not word:
        print("[WARNING] Käyttäjän sana ei kelpaa, pyydetään uusi sana")
        player_names = [v["username"] for v in players.values()]
        first_player = player_names[0] if player_names else None
        emit("word_failed", {
            "player": first_player,
            "pair": pending_pair + 1
        }, broadcast=True)
        return
    pair_index = pending_pair
    print(f"[INFO] Vastaanotettu sana '{word}' parille {pair_index+1}")
    if append_word_images_to_grid(word, pair_index):
        pending_pair += 1
        ask_next_word()
    else:
        print(f"[WARNING] Pixabay ei löytänyt kuvia sanalle '{word}', pyydetään uusi sana")
        player_names = [v["username"] for v in players.values()]
        first_player = player_names[0]
        emit("word_failed", {
            "player": first_player,
            "pair": pending_pair + 1
        }, broadcast=True)

@socketio.on("ask_for_word")
def handle_client_request_ask_for_word(data):
    # Client pyytää toistamaan saman parin kyselyn (esim. word_failed perässä)
    target_player = data.get("player")
    pair = int(data.get("pair", 0))
    debug(f"[DEBUG] Client pyysi ask_for_word uudestaan: player={target_player}, pair={pair}")
    # Pieni viive, jotta mahdollinen alert/prompt ei törmää seuraavaan prompttiin
    socketio.sleep(0.3)
    try:
        emit("ask_for_word", {"player": target_player, "pair": pair}, broadcast=True)
    except Exception as e:
        print(f"[ERROR] ask_for_word uudelleenlähetys epäonnistui: {e}")

def fetch_and_save_pixabay_images(word, pair_index, required_count=2):
    global used_pixabay_image_ids
    debug(f"[DEBUG] Haetaan Pixabaysta kuvia sanalla: {word}")
    pixabay_api_key = (os.getenv("PIXABAY_API_KEY") or "").strip()
    if not pixabay_api_key:
        print("[ERROR] PIXABAY_API_KEY puuttuu. Lisää avain .env-tiedostoon.")
        return None
    url = "https://pixabay.com/api/"
    params = {
        "key": pixabay_api_key,
        "q": word,
        "image_type": "photo",
        "orientation": "horizontal",
        "per_page": 10,
        "safesearch": "true"
    }
    try:
        response = requests.get(url, params=params, timeout=15)
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
        if len(selected_hits) == required_count:
            break

    if skipped_duplicates:
        print(f"[INFO] Ohitettiin {skipped_duplicates} jo kaytettya Pixabay-kuvaa sanalle '{word}'")

    if len(selected_hits) >= required_count:
        os.makedirs("static/images", exist_ok=True)
        paths = []
        for i, hit in enumerate(selected_hits[:required_count]):
            img_url = hit["webformatURL"]
            try:
                img_response = requests.get(img_url, timeout=15)
                img_response.raise_for_status()
            except requests.RequestException as e:
                print(f"[ERROR] Kuvan lataus epäonnistui ({img_url}): {e}")
                return None
            img_data = img_response.content
            img = Image.open(BytesIO(img_data)).convert("RGB")
            img = img.resize((512, 512))
            filename = f"{word}{i+1}_{pair_index}.png"
            save_path = os.path.join("static/images", filename)
            img.save(save_path)
            debug(f"[DEBUG] Tallennettu kuva: {save_path}")
            paths.append(save_path)
            used_pixabay_image_ids.add(hit["id"])
        return paths
    else:
        print(f"[INFO] Sanalle '{word}' ei loytynyt tarpeeksi uusia Pixabay-kuvia taman eran sisalla")
        debug(f"[DEBUG] Ei tarpeeksi kuvia sanalle: {word}")
        return None
if __name__ == "__main__":
    players.clear()
    print("[INFO] Muistipeli kaynnissa osoitteessa http://127.0.0.1:5000")
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, use_reloader=False)
