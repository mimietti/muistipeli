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
words = ["cat", "dog", "fish", "car", "tree", "sun", "apple", "book"]
grid_data = []
revealed_cards = []
matched_indices = set()
turn = 0  # 0 tai 1

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

@socketio.on("start_game")
def start_game():
    global grid_data, revealed_cards, matched_indices, turn
    print("[DEBUG] Peli käynnistyy...")
    images = []
    for filename in sorted(os.listdir("static/images")):
        if filename.endswith(".jpg"):
            word = filename.split("_")[0]
            path = f"/static/images/{filename}"
            images.append({"image": path, "word": word})
    random.shuffle(images)
    grid_data = images
    revealed_cards = []
    matched_indices = set()
    turn = 0
    emit("init_grid", {"cards": grid_data, "turn": players[turn], "players": players}, broadcast=True)

@socketio.on("start_game_clicked")
def handle_start_game():
    if len(players) == max_players:
        print("[DEBUG] Molemmat pelaajat liittyneet, aloitetaan peli")
        emit("start_game", broadcast=True)
    else:
        print("[DEBUG] Pelaajia ei ole tarpeeksi pelin aloittamiseen")

@socketio.on("card_clicked")
def handle_card_click(data):
    global revealed_cards, turn, matched_indices
    index = data["index"]
    if index in matched_indices or index in revealed_cards:
        return

    revealed_cards.append(index)
    emit("reveal_card", {"index": index, "image": grid_data[index]["image"]}, broadcast=True)

    if len(revealed_cards) == 2:
        idx1, idx2 = revealed_cards
        word1 = grid_data[idx1]["word"]
        word2 = grid_data[idx2]["word"]
        if word1 == word2:
            matched_indices.update(revealed_cards)
            emit("pair_found", {"indices": revealed_cards, "word": word1}, broadcast=True)
        else:
            def hide_cards():
                socketio.sleep(1.5)
                emit("hide_cards", {"indices": revealed_cards}, broadcast=True)
            socketio.start_background_task(hide_cards)
            turn = (turn + 1) % 2
        revealed_cards = []

    emit("update_turn", {"turn": players[turn]}, broadcast=True)

@socketio.on("disconnect")
def on_disconnect():
    print("[DEBUG] Yhteys katkaistu")

if __name__ == "__main__":
    print("[DEBUG] Sovellus käynnissä osoitteessa http://0.0.0.0:5000")
    socketio.run(app, host="0.0.0.0", port=5000)
