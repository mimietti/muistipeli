# Aseta monkey_patch ennen muita importteja
import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template
from flask_socketio import SocketIO, emit
import random
import os

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
    global revealed_cards, turn, matched_indices, player_points, round_win

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

            # Lisää pisteet vuorossa olevalle pelaajalle
            current_player = players[turn]
            player_points[current_player] += 1

            revealed_cards = []
            if len(matched_indices) == len(grid_data):
                print("[DEBUG] Peli päättyi!")
                winner = max(player_points, key=player_points.get)
                if winner not in round_win:
                    round_win[winner] = 0
                round_win[winner] += 1
                socketio.emit("game_over", {
                    "winner": winner,
                    "points": player_points,
                    "round_wins": round_win
                })
        else:
            print(f"[DEBUG] Ei paria: {word1} vs {word2}")
            indices_to_hide = list(revealed_cards)
            revealed_cards = []

            def hide_later():
                socketio.sleep(2)
                socketio.emit("hide_cards", {"indices": indices_to_hide})

            socketio.start_background_task(hide_later)
            turn = (turn + 1) % len(players)

        socketio.emit("update_turn", {"turn": players[turn]})



@socketio.on("disconnect")
def on_disconnect():
    print("[DEBUG] Yhteys katkaistu")

if __name__ == "__main__":
    players.clear()
    print("[DEBUG] Sovellus käynnissä osoitteessa http://0.0.0.0:5000")
    socketio.run(app, host="0.0.0.0", port=5000)
