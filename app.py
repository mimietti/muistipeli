from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
import random
import os
import requests
from PIL import Image
from io import BytesIO
from dotenv import load_dotenv   # <-- TÄMÄ
from collections import defaultdict
load_dotenv()                   # <--
import uuid

app = Flask(__name__)
# Salli yhteydet myös muista koneista/osoitteista (kehitystä varten)
socketio = SocketIO(app, async_mode='threading', cors_allowed_origins="*")

players = {}  # { sid: {"username": ..., "reconnect_token": ...} }
max_players = 2
grid_data = []
revealed_cards = []
matched_indices = set()
turn = 0
player_points = {}
round_win = defaultdict(int)
player_order = []  # Pelaajien järjestys

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/waiting")
def waiting():
    return render_template("waiting_room.html")

@app.route("/game")
def game():
    return render_template("game.html")

@socketio.on("join")
def on_join(data):
    global players
    username = data["username"]
    sid = request.sid
    reconnect_token = data.get("reconnect_token")
    if not reconnect_token:
        print(f"[DEBUG] HYLÄTTY join ilman reconnect_tokenia: {username}, SID: {sid}")
        return
    # Poista vanha sid, jos reconnect_token täsmää
    for k, v in list(players.items()):
        if v["username"] == username and v.get("reconnect_token") == reconnect_token:
            del players[k]
    if len(players) < max_players:
        players[sid] = {"username": username, "reconnect_token": reconnect_token}
        print(f"[DEBUG] {username} liittyi peliin. SID: {sid}, reconnect_token: {reconnect_token}")
    # Lähetä pelaajalista säilyttäen liittymisjärjestys ja yksilöivä reconnect_token
    players_ordered = list(players.values())  # insertion order säilyy
    usernames = [v["username"] for v in players_ordered]
    infos = [{"username": v["username"], "reconnect_token": v.get("reconnect_token")}
             for v in players_ordered]
    last_token = infos[-1]["reconnect_token"] if infos else None
    socketio.emit("player_joined", {
        "username": username,
        "players": usernames,
        "players_info": infos,
        "last_joined_token": last_token
    })

@socketio.on("start_game_clicked")
def handle_start_game():
    if len(players) == max_players:
        print("[DEBUG] Molemmat pelaajat liittyneet, aloitetaan peli")
        # Luo pelidata jo tässä
        generate_grid()
        emit("start_game", broadcast=True)
    else:
        print("[DEBUG] Pelaajia ei ole tarpeeksi pelin aloittamiseen")


@socketio.on("request_grid")
def handle_grid_request():
    global player_order, turn
    print("[DEBUG] request_grid vastaanotettu – tarkistetaan pelitila")
    print(f"[DEBUG] Pelaajat: {players}")
    print(f"[DEBUG] Grid sisältää {len(grid_data)} korttia")

    if len(players) < 2:
        print("[WARNING] Ei tarpeeksi pelaajia ruudukon palauttamiseen.")
        emit("no_grid", {"reason": "Pelaajia liian vähän"})
        return

    # Jos ruudukko ei ole valmis, ilmoita siitä ja tarvittaessa toista käynnissä oleva sanapyyntö
    if not grid_data or len(grid_data) < 16:
        print("[DEBUG] Ruudukko ei ole valmis – ei lähetetä init_grid")
        emit("no_grid", {"reason": "Grid ei valmis"})
        # Jos custom-pelin sanojen kysely on käynnissä, toista viimeisin pyyntö
        if 'pending_pair' in globals():
            try:
                if len(player_order) < 1:
                    # Fallback järjestys
                    player_order[:] = [v["username"] for v in players.values()]
                first_player = player_order[0] if player_order else None
                if first_player is not None and pending_pair < 8:
                    print(f"[DEBUG] request_grid: toistetaan ask_for_word pelaajalle {first_player}, pari {pending_pair+1}")
                    emit("ask_for_word", {"player": first_player, "pair": pending_pair + 1}, broadcast=True)
            except Exception as e:
                print(f"[DEBUG] request_grid: ask_for_word toisto epäonnistui: {e}")
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
    print("[DEBUG] Client ilmoitti olevansa valmis peliin")
    emit("start_game", broadcast=True)

def generate_grid():
    global grid_data, revealed_cards, matched_indices, turn, player_points, player_order
    print("[DEBUG] Peli käynnistyy – luodaan ruudukko")
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
    print(f"[DEBUG] Kortteja yhteensä: {len(grid_data)}")


           


@socketio.on("card_clicked")
def handle_card_click(data):
    global revealed_cards, turn, matched_indices, player_order, player_points

    index = data["index"]
    if index in matched_indices or index in revealed_cards:
        return

    print(f"[DEBUG] Kortti klikattu: index {index}, sana: {grid_data[index]['word']}")
    revealed_cards.append(index)
    socketio.emit("reveal_card", {
        "index": index,
        "image": grid_data[index]["image"]
    })

    if len(revealed_cards) == 2:
        idx1, idx2 = revealed_cards
        word1 = grid_data[idx1]["word"]
        word2 = grid_data[idx2]["word"]

        if word1 == word2:
            matched_indices.update(revealed_cards)
            print(f"[DEBUG] Pari löytyi: {word1}")
            socketio.emit("pair_found", {"indices": revealed_cards, "word": word1})
            revealed_cards = []
            # Piste tälle vuorossa olevalle pelaajalle
            current_player_name = player_order[turn] if 0 <= turn < len(player_order) else None
            if current_player_name is not None:
                player_points[current_player_name] = player_points.get(current_player_name, 0) + 1
                print(f"[DEBUG] Piste {current_player_name} (+1). Pisteet nyt: {player_points}")

            # Jos kaikki parit löytyneet, päätä peli kerran
            if len(matched_indices) == len(grid_data):
                print("[DEBUG] Kaikki parit löytyneet – peli ohi!")
                # Määritä voittaja tai tasapeli pisteiden perusteella
                if player_points:
                    max_pts = max(player_points.values()) if player_points else 0
                    winners = [n for n, p in player_points.items() if p == max_pts]
                    if len(winners) == 1:
                        winner = winners[0]
                        round_win[winner] += 1
                        winner_label = winner
                        print(f"[DEBUG] Voittaja: {winner}. Erävoitot: {dict(round_win)}")
                    else:
                        winner_label = "Tasapeli"
                        print(f"[DEBUG] Tasapeli. Pisteet: {player_points}")

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
                # Poista mahdolliset kesken jääneet custom-sanakyselyn tilat
                globals().pop('pending_pair', None)
                globals().pop('pending_words', None)
                globals().pop('pending_player', None)

        else:
            print(f"[DEBUG] Ei paria: {word1} vs {word2}")
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
        print(f"[DEBUG] Vuoro nyt: {next_turn_name}")
        socketio.emit("update_turn", {"turn": next_turn_name})

@socketio.on("disconnect")
def on_disconnect():
    global players
    sid = request.sid
    if sid in players:
        username = players[sid]["username"]
        print(f"[DEBUG] {username} (SID: {sid}) disconnect havaittu, odotetaan mahdollista reconnectia...")
        def remove_later(sid_to_remove, username):
            socketio.sleep(8)  # Odota 8 sekuntia reconnectia
            if sid_to_remove in players:
                print(f"[DEBUG] {username} (SID: {sid_to_remove}) poistetaan pelaajalistasta (ei reconnectia)")
                del players[sid_to_remove]
                socketio.emit("player_joined", {"username": username, "players": [v["username"] for v in players.values()]})
                # Jos kaikki pelaajat poistuneet, nollaa pelitila
                if len(players) == 0:
                    print("[DEBUG] Kaikki pelaajat poistuneet – nollataan pelitila")
                    grid_data.clear()
                    revealed_cards.clear()
                    matched_indices.clear()
                    global turn, player_points
                    turn = 0
                    player_points.clear()
                    globals().pop('pending_pair', None)
                    globals().pop('pending_words', None)
                    globals().pop('pending_player', None)
        socketio.start_background_task(remove_later, sid, username)
    else:
        print(f"[DEBUG] Tuntematon SID {sid} poistui pelistä")

@socketio.on("start_custom_game")
def handle_start_custom_game():
    global pending_words, pending_player, pending_pair, grid_data, player_order
    print(f"[DEBUG] start_custom_game kutsuttu! grid_data: {len(grid_data)}, pending_pair: {globals().get('pending_pair', 'ei asetettu')}, players: {players}")
    # Jos peli on jo käynnissä, mutta pelaajien reconnect_tokenit ovat vaihtuneet, nollaa peli
    current_tokens = set(v['reconnect_token'] for v in players.values())
    grid_tokens = getattr(handle_start_custom_game, 'last_tokens', set())
    if grid_data and current_tokens != grid_tokens:
        print("[DEBUG] Pelaajien reconnect_tokenit vaihtuneet – nollataan pelitila!")
        grid_data.clear()
        revealed_cards.clear()
        matched_indices.clear()
        global turn, player_points
        turn = 0
        player_points.clear()
        globals().pop('pending_pair', None)
        globals().pop('pending_words', None)
        globals().pop('pending_player', None)
    # Tallenna nykyiset reconnect_tokenit seuraavaa vertailua varten
    handle_start_custom_game.last_tokens = set(v['reconnect_token'] for v in players.values())
    # Tallenna pelaajien järjestys kun peli alkaa
    player_order = [v["username"] for v in players.values()]
    if grid_data:  # Jos peli on jo käynnissä, älä aloita uutta
        print("[DEBUG] start_custom_game hylätty: peli on jo käynnissä")
        return
    print("[DEBUG] start_custom_game vastaanotettu, aloitetaan sanojen kysely")
    pending_words = []
    pending_player = 0
    pending_pair = 0
    grid_data.clear()
    ask_next_word()

def ask_next_word():
    global pending_pair, player_order, player_points, matched_indices, revealed_cards, turn
    print(f"[DEBUG] ask_next_word kutsuttu! pending_pair: {pending_pair}, grid_data: {len(grid_data)}")
    # Jos kaikki 8 paria on annettu, lähetä ruudukko näkyviin
    if pending_pair >= 8:
        print("[DEBUG] Kaikki sanat annettu, lähetetään init_grid")
        # Alusta pelitila ennen ruudukon lähetystä
        matched_indices = set()
        revealed_cards = []
        turn = 0
        player_points = {name: 0 for name in player_order}
        random.shuffle(grid_data)
        emit("init_grid", {
            "cards": grid_data,
            "turn": player_order[0] if player_order else None,
            "players": player_order
        }, broadcast=True)
        return

    if len(player_order) < 2:
        print("[DEBUG] Pelaajia liian vähän, peli keskeytetään")
        emit("game_aborted", {"reason": "Toinen pelaaja poistui. Peli keskeytetty."}, broadcast=True)
        return
    # Kysy aina ekalta pelaajalta (player_order[0])
    first_player = player_order[0]
    print(f"[DEBUG] ask_next_word: pair={pending_pair}, player={first_player}")
    print(f"[DEBUG] Lähetetään ask_for_word pelaajalle {first_player}, pari {pending_pair+1}")
    emit("ask_for_word", {
        "player": first_player,
        "pair": pending_pair + 1
    }, broadcast=True)

@socketio.on("word_given")
def handle_word_given(data):
    global pending_pair, grid_data
    if pending_pair >= 8:
        print(f"[DEBUG] word_given hylätty, kaikki parit jo annettu (pending_pair={pending_pair})")
        return
    word = data["word"]
    pair_index = pending_pair
    print(f"[DEBUG] word_given vastaanotettu: {word} (pari {pair_index+1})")
    result = fetch_and_save_pixabay_images(word, pair_index)
    if result:
        print(f"[DEBUG] Pixabaysta löytyi kuvat sanalle '{word}': {result}")
        for i, path in enumerate(result):
            grid_data.append({"image": "/" + path, "word": word})
        pending_pair += 1
        ask_next_word()
    else:
        print(f"[DEBUG] Pixabay EI löytänyt kuvia sanalle '{word}', pyydetään uusi sana")
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
    print(f"[DEBUG] Client pyysi ask_for_word uudestaan: player={target_player}, pair={pair}")
    # Pieni viive, jotta mahdollinen alert/prompt ei törmää seuraavaan prompttiin
    socketio.sleep(0.3)
    try:
        emit("ask_for_word", {"player": target_player, "pair": pair}, broadcast=True)
    except Exception as e:
        print(f"[DEBUG] ask_for_word uudelleenlähetys epäonnistui: {e}")

def fetch_and_save_pixabay_images(word, pair_index):
    print(f"[DEBUG] Haetaan Pixabaysta kuvia sanalla: {word}")
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
    except requests.RequestException as e:
        print(f"[ERROR] Pixabay-pyyntö epäonnistui: {e}")
        return None

    print(f"[DEBUG] Pixabay HTTP status: {response.status_code}")
    print(f"[DEBUG] Pixabay response text: {response.text[:300]}")  # Näytä max 300 merkkiä
    if response.status_code != 200:
        print("[ERROR] Pixabay palautti virheellisen HTTP-statuksen.")
        return None

    try:
        data = response.json()
    except Exception as e:
        print(f"[ERROR] Pixabay JSON decode error: {e}")
        return None

    if data.get("error"):
        print(f"[ERROR] Pixabay API error: {data.get('error')} | message: {data.get('message')}")
        return None

    if "hits" in data and len(data["hits"]) >= 2:
        os.makedirs("static/images", exist_ok=True)
        paths = []
        for i in range(2):
            img_url = data["hits"][i]["webformatURL"]
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
            print(f"[DEBUG] Tallennettu kuva: {save_path}")
            paths.append(save_path)
        return paths
    else:
        print(f"[DEBUG] Ei tarpeeksi kuvia sanalle: {word}")
        return None
if __name__ == "__main__":
    players.clear()
    print("[DEBUG] Sovellus käynnissä osoitteessa http://0.0.0.0:5000 (LAN) – käytä palvelimen IP:tä toiselta koneelta.")
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)
