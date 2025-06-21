# Aseta monkey_patch ennen muita importteja
import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template
from flask_socketio import SocketIO, emit
import random
import os
import requests
from PIL import Image
from io import BytesIO
from dotenv import load_dotenv   # <-- TÄMÄ
load_dotenv()                   # <--

app = Flask(__name__)
socketio = SocketIO(app, async_mode='eventlet')

players = []
max_players = 2
grid_data = []
revealed_cards = []
matched_indices = set()
turn = 0
player_points = {}
round_win={}


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
    if username not in players and len(players) < max_players:
        players.append(username)
        print(f"[DEBUG] {username} liittyi peliin.")
        emit("player_joined", {"username": username, "players": players}, broadcast=True)

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
    print("[DEBUG] request_grid vastaanotettu – lähetetään init_grid")
    print(f"[DEBUG] Pelaajat: {players}")
    print(f"[DEBUG] Grid sisältää {len(grid_data)} korttia")

    if len(players) < 2:
        print("[WARNING] Ei tarpeeksi pelaajia ruudukon alustamiseen.")
        return

    emit("init_grid", {
        "cards": grid_data,
        "turn": players[turn],
        "players": players
    })

@socketio.on("ready_for_game")
def handle_ready_for_game():
    print("[DEBUG] Client ilmoitti olevansa valmis peliin")
    emit("start_game", broadcast=True)

def generate_grid():
    global grid_data, revealed_cards, matched_indices, turn, player_points
    print("[DEBUG] Peli käynnistyy – luodaan ruudukko")
    images = []
    for filename in sorted(os.listdir("static/images")):
        if filename.endswith(".jpg"):
            word = filename.split("_")[0]
            path = f"/static/images/{filename}"
            images.append({"image": path, "word": word})

    # 👉 Ryhmittele sanojen mukaan
    word_dict = {}
    for item in images:
        word = item["word"]
        word_dict.setdefault(word, []).append(item)

    # 👉 Valitse 8 satunnaista sanaa, joilla on 2 kuvaa
    valid_pairs = [v for v in word_dict.values() if len(v) >= 2]
    selected = random.sample(valid_pairs, 8)
    selected_images = [img for pair in selected for img in pair[:2]]

    random.shuffle(selected_images)
    grid_data = selected_images
    revealed_cards = []
    matched_indices = set()
    turn = 0
    player_points = {player: 0 for player in players}  # Alusta pisteet
    print(f"[DEBUG] Kortteja yhteensä: {len(grid_data)}")


           


@socketio.on("card_clicked")
def handle_card_click(data):
    global revealed_cards, turn, matched_indices

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
            if len(matched_indices) == len(grid_data):
                print("[DEBUG] Peli päättyi!")
                winner = max(player_points, key=player_points.get)
                round_win[winner] += 1
                socketio.emit("game_over", {
                    "points": player_points,
                    "round_win": round_win
            })

                if len(matched_indices) == len(grid_data):
                    print("[DEBUG] Kaikki parit löytyneet – peli ohi!")

                    # Alustetaan player_points, jos se on tyhjä
                    if not player_points:
                        for player in players:
                            player_points[player] = 0

                    # Päivitetään pelaajien pisteet
                    for idx in matched_indices:
                        word = grid_data[idx]["word"]
                        for player in players:
                            if word in player:  # Päivitä tämä logiikka tarpeen mukaan
                                player_points[player] += 1

                    # Tarkistetaan voittaja
                    if player_points:
                        winner = max(player_points, key=player_points.get)
                        print(f"[DEBUG] Voittaja: {winner}")

                        # Lähetetään game_over-tapahtuma
                        socketio.emit("game_over", {
                            "winner": winner,
                            "points": player_points,
                            "round_win": round_win
                        })
                    else:
                        print("[ERROR] Ei voittajaa, player_points on tyhjä.")

        else:
            print(f"[DEBUG] Ei paria: {word1} vs {word2}")
            indices_to_hide = list(revealed_cards)  # Tee kopio!
            revealed_cards = []

            def hide_later():
                socketio.sleep(2)  # Odota 2 sekuntia
                socketio.emit("hide_cards", {"indices": indices_to_hide})
            
            socketio.start_background_task(hide_later)
            turn = (turn + 1) % len(players)
        
        socketio.emit("update_turn", {"turn": players[turn]})



@socketio.on("disconnect")
def on_disconnect():
    print("[DEBUG] Yhteys katkaistu")

@socketio.on("start_custom_game")
def handle_start_custom_game():
    global pending_words, pending_player, pending_pair, grid_data
    print("[DEBUG] start_custom_game vastaanotettu, aloitetaan sanojen kysely")
    pending_words = []
    pending_player = 0
    pending_pair = 0
    grid_data.clear()
    ask_next_word()

def ask_next_word():
    global pending_player, pending_pair
    print(f"[DEBUG] ask_next_word: pair={pending_pair}, player={players[pending_player]}")
    if pending_pair < 8:
        emit("ask_for_word", {
            "player": players[pending_player],
            "pair": pending_pair + 1
        }, broadcast=True)
        print(f"[DEBUG] Lähetettiin ask_for_word pelaajalle {players[pending_player]}, pari {pending_pair+1}")
    else:
        print("[DEBUG] Kaikki sanat annettu, lähetetään init_grid")
        random.shuffle(grid_data)
        emit("init_grid", {
            "cards": grid_data,
            "turn": players[0],
            "players": players
        }, broadcast=True)

@socketio.on("word_given")
def handle_word_given(data):
    global pending_player, pending_pair, grid_data
    word = data["word"]
    pair_index = pending_pair
    print(f"[DEBUG] word_given vastaanotettu: {word} (pari {pair_index+1})")
    result = fetch_and_save_pixabay_images(word, pair_index)
    if result:
        print(f"[DEBUG] Pixabaysta löytyi kuvat sanalle '{word}': {result}")
        for i, path in enumerate(result):
            grid_data.append({"image": "/" + path, "word": word})
        pending_pair += 1
        pending_player = (pending_player + 1) % 2
        ask_next_word()
    else:
        print(f"[DEBUG] Pixabay EI löytänyt kuvia sanalle '{word}', pyydetään uusi sana")
        emit("word_failed", {
            "player": players[pending_player],
            "pair": pending_pair + 1
        }, broadcast=True)

def fetch_and_save_pixabay_images(word, pair_index):
    print(f"[DEBUG] Haetaan Pixabaysta kuvia sanalla: {word}")
    pixabay_api_key = os.getenv("PIXABAY_API_KEY")
    url = "https://pixabay.com/api/"
    params = {
        "key": pixabay_api_key,
        "q": word,
        "image_type": "photo",
        "orientation": "horizontal",
        "per_page": 10,
        "safesearch": "true"
    }
    response = requests.get(url, params=params)
    print(f"[DEBUG] Pixabay HTTP status: {response.status_code}")
    print(f"[DEBUG] Pixabay response text: {response.text[:300]}")  # Näytä max 300 merkkiä

    try:
        data = response.json()
    except Exception as e:
        print(f"[ERROR] Pixabay JSON decode error: {e}")
        return None

    if "hits" in data and len(data["hits"]) >= 2:
        paths = []
        for i in range(2):
            img_url = data["hits"][i]["webformatURL"]
            img_data = requests.get(img_url).content
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
    print("[DEBUG] Sovellus käynnissä osoitteessa http://0.0.0.0:5000")
    socketio.run(app, host="0.0.0.0", port=5000)
