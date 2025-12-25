import os
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, g, send_from_directory, session, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import subprocess
import requests
import uuid
import shutil

# ---------- Configuration ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "kidsta.db")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
# images + video + audio + pdf
ALLOWED_EXT = {
    "png", "jpg", "jpeg", "gif", "webp",   # images
    "mp4", "mov", "webm",                  # videos
    "mp3", "wav", "m4a", "ogg",           # audio
    "pdf"                                  # documents
}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
# ensure audio library folder exists (optional)
os.makedirs(os.path.join(BASE_DIR, "static", "audio_library"), exist_ok=True)

app = Flask(__name__)
app.secret_key = "change_this_for_proDUCTION"  # change for real deployment
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB

# Keep session alive
app.permanent_session_lifetime = timedelta(days=30)

# ---------- ACRCloud config (fill these!) ----------
ACR_HOST = "https://identify-eu-west-1.acrcloud.com/v1/identify"  # example endpoint
ACCESS_KEY = "YOUR_ACR_KEY"      # <-- replace with your ACRCloud ACCESS_KEY
ACCESS_SECRET = "YOUR_ACR_SECRET"  # <-- replace with your ACRCloud ACCESS_SECRET

# ---------- DB helper ----------
def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
    cur = db.cursor()

    # users
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        age INTEGER NOT NULL,
        kidsta_id TEXT UNIQUE,
        display_name TEXT,
        avatar_filename TEXT,
        created_at TEXT NOT NULL
    )
    """)

    # friends
    cur.execute("""
    CREATE TABLE IF NOT EXISTS friends (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        friend_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL
    )
    """)

    # posts
    cur.execute("""
    CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        caption TEXT,
        media_filename TEXT,
        created_at TEXT NOT NULL,
        visibility TEXT NOT NULL DEFAULT 'public'
    )
    """)

    # post_media: store multiple files per post
    cur.execute("""
    CREATE TABLE IF NOT EXISTS post_media (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER NOT NULL,
        filename TEXT NOT NULL,
        media_type TEXT NOT NULL,
        ord INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """)

    # notifications (so create_notification won't fail on fresh DB)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        type TEXT NOT NULL,
        from_user_id INTEGER,
        post_id INTEGER,
        is_read INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """)

    # likes
    cur.execute("""
    CREATE TABLE IF NOT EXISTS likes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        post_id INTEGER NOT NULL,
        value INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    # comments
    cur.execute("""
    CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        post_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    # reports (kisne kisko report kiya)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reporter_id INTEGER NOT NULL,
        reported_id INTEGER NOT NULL,
        reason TEXT,
        created_at TEXT NOT NULL
    )
    """)

    # blocks (kisne kisko block kiya)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS blocks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        blocker_id INTEGER NOT NULL,
        blocked_id INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    db.commit()


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()

# ---------- Small helpers ----------
def allowed_file(filename):
    if not filename:
        return False
    ext = filename.rsplit(".", 1)[-1].lower()
    return ext in ALLOWED_EXT

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()

def is_blocked(viewer_id, target_id):
    """Check if two users are blocked from each other."""
    if not viewer_id or not target_id:
        return False

    db = get_db()
    row = db.execute("""
        SELECT 1 FROM blocks
        WHERE (blocker_id = ? AND blocked_id = ?)
           OR (blocker_id = ? AND blocked_id = ?)
        LIMIT 1
    """, (viewer_id, target_id, target_id, viewer_id)).fetchone()

    return row is not None


def are_friends(user_id, other_id):
    """Check if two users are friends (accepted)."""
    db = get_db()
    row = db.execute("""
        SELECT 1 FROM friends
        WHERE user_id = ? AND friend_id = ? AND status = 'accepted'
        LIMIT 1
    """, (user_id, other_id)).fetchone()
    if row:
        return True

    row2 = db.execute("""
        SELECT 1 FROM friends
        WHERE user_id = ? AND friend_id = ? AND status = 'accepted'
        LIMIT 1
    """, (other_id, user_id)).fetchone()
    return row2 is not None

def create_notification(user_id, notif_type, from_user_id=None, post_id=None):
    """Create a notification row."""
    if not user_id:
        return
    db = get_db()
    created_at = datetime.utcnow().isoformat()
    try:
        db.execute(
            "INSERT INTO notifications (user_id, type, from_user_id, post_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, notif_type, from_user_id, post_id, created_at)
        )
        db.commit()
    except Exception:
        # If notifications table doesn't exist or insert fails, ignore gracefully
        pass

# ---------- Copyright-check helper (unchanged) ----------
def check_copyright(video_path, snippet_start_seconds=5, snippet_duration=10):
    tmp_id = str(uuid.uuid4())
    tmp_audio = os.path.join(BASE_DIR, f"tmp_audio_{tmp_id}.wav")
    try:
        cmd = [
            "ffmpeg", "-y", "-i", video_path,
            "-ss", f"00:00:{int(snippet_start_seconds):02d}",
            "-t", str(int(snippet_duration)),
            "-ar", "16000", "-ac", "1",
            tmp_audio
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

        if not os.path.exists(tmp_audio):
            return False, {}

        with open(tmp_audio, "rb") as f:
            audio_data = f.read()

        data = {"access_key": ACCESS_KEY}
        files = {"sample": ("audio.wav", audio_data, "audio/wav")}
        try:
            r = requests.post(ACR_HOST, data=data, files=files, timeout=10)
            result = r.json()
        except Exception:
            result = {}

        meta = {}
        if "metadata" in result and "music" in result["metadata"] and len(result["metadata"]["music"]) > 0:
            music = result["metadata"]["music"][0]
            title = music.get("title")
            artists = music.get("artists", [])
            artist_name = artists[0].get("name") if artists else None
            meta = {"title": title, "artist": artist_name, "raw": music}
            return True, meta

        return False, {}
    finally:
        try:
            if os.path.exists(tmp_audio):
                os.remove(tmp_audio)
        except Exception:
            pass

# ---------- Routes ----------
@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("home"))
    return redirect(url_for("login"))

# ---------- LOGIN (create or login) ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    init_db()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        age_raw = request.form.get("age", "").strip()

        if not username or not password or not age_raw:
            flash("Please fill username, password and age.", "danger")
            return redirect(url_for("login"))

        try:
            age = int(age_raw)
        except:
            flash("Enter a valid age.", "danger")
            return redirect(url_for("login"))

        if age < 7 or age > 18:
            flash("Only ages 7 to 18 allowed.", "danger")
            return redirect(url_for("login"))

        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

        if user:
            if not check_password_hash(user["password"], password):
                flash("Wrong password.", "danger")
                return redirect(url_for("login"))

            session["user_id"] = user["id"]
            session["display_name"] = user["display_name"]
            session["avatar_filename"] = user["avatar_filename"]
            session["username"] = user["username"]
            session.permanent = True

            if user["kidsta_id"] and user["display_name"]:
                return redirect(url_for("home"))
            return redirect(url_for("profile_setup"))
        else:
            pw_hash = generate_password_hash(password)
            created_at = datetime.utcnow().isoformat()
            cur = db.cursor()
            cur.execute("INSERT INTO users (username, password, age, created_at) VALUES (?, ?, ?, ?)",
                        (username, pw_hash, age, created_at))
            db.commit()
            new_id = cur.lastrowid
            session["user_id"] = new_id
            session["display_name"] = None
            session["avatar_filename"] = None
            session["username"] = username
            session.permanent = True
            flash("Account created! Please complete profile.", "success")
            return redirect(url_for("profile_setup"))

    return render_template("login.html", title="Login")

@app.route("/audio-library")
def audio_library():
    folder = os.path.join(app.config["UPLOAD_FOLDER"], "..", "audio_library")
    folder = os.path.abspath(folder)
    try:
        files = [f for f in os.listdir(folder) if f.lower().endswith((".mp3", ".wav", ".ogg", ".m4a"))]
    except:
        files = []
    return render_template("audio_library.html", files=files)

@app.route("/audio/<path:filename>")
def uploaded_audio(filename):
    folder = os.path.join("static", "audio_library")
    return send_from_directory(folder, filename)


# ---------- LOGOUT ----------
@app.route("/logout")
def logout():
    session.pop("user_id", None)
    session.pop("display_name", None)
    session.pop("avatar_filename", None)
    session.pop("username", None)
    flash("Logged out.", "info")
    return redirect(url_for("login"))

@app.route("/reels")
def reels():
    init_db()
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    return render_template("reels.html", user=user)


# ---------- PROFILE SETUP ----------
@app.route("/profile_setup", methods=["GET", "POST"])
def profile_setup():
    init_db()
    user = current_user()
    if not user:
        flash("Please login first.", "danger")
        return redirect(url_for("login"))

    if user["kidsta_id"] and user["display_name"]:
        return redirect(url_for("home"))

    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()
        kidsta_id = request.form.get("kidsta_id", "").strip()
        file = request.files.get("avatar")

        if not display_name or not kidsta_id:
            flash("Please fill all fields.", "danger")
            return redirect(url_for("profile_setup"))

        db = get_db()
        existing = db.execute("SELECT id FROM users WHERE kidsta_id = ?", (kidsta_id,)).fetchone()
        if existing:
            flash("Kidsta ID already taken.", "danger")
            return redirect(url_for("profile_setup"))

        avatar_fname = None
        if file and file.filename:
            if not allowed_file(file.filename):
                flash("File type not allowed.", "danger")
                return redirect(url_for("profile_setup"))
            fname = secure_filename(file.filename)
            stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
            avatar_fname = f"{stamp}_{fname}"
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], avatar_fname))

        db.execute("UPDATE users SET display_name = ?, kidsta_id = ?, avatar_filename = ? WHERE id = ?",
                   (display_name, kidsta_id, avatar_fname, user["id"]))
        db.commit()

        session["display_name"] = display_name
        session["avatar_filename"] = avatar_fname
        session["username"] = user["username"]
        session.permanent = True

        flash("Profile saved.", "success")
        return redirect(url_for("home"))

    return render_template("profile_setup.html", user=user)

# ---------- PROFILE page ----------
@app.route("/profile")
def profile():
    init_db()
    user = current_user()
    if not user:
        flash("Please login first.", "danger")
        return redirect(url_for("login"))

    db = get_db()
    posts = db.execute("SELECT * FROM posts WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)).fetchall()
    pending = db.execute("SELECT COUNT(*) AS c FROM friends WHERE friend_id = ? AND status = 'pending'", (user["id"],)).fetchone()["c"]
    return render_template("profile.html", user=user, posts=posts, pending_requests=pending)

# ---------- UPLOAD (new post) ----------
@app.route("/upload", methods=["GET", "POST"])
def upload_post():
    init_db()
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    if request.method == "POST":
        # title and caption from form
        title = request.form.get("title", "").strip()
        caption = request.form.get("caption", "").strip()

        # visibility is fixed to friends only (no public)
        visibility = "friends"

        # handle multiple files
        files = request.files.getlist("media")
        saved_files = []

        for f in files:
            if not f or not f.filename:
                continue
            if not allowed_file(f.filename):
                flash(f"File type not allowed: {f.filename}", "danger")
                return redirect(url_for("upload_post"))

            fname = secure_filename(f.filename)
            stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
            final_fname = f"{stamp}_{fname}"
            final_path = os.path.join(app.config["UPLOAD_FOLDER"], final_fname)

            try:
                f.save(final_path)
            except Exception:
                try:
                    f.stream.seek(0)
                    f.save(final_path)
                except Exception as e:
                    flash("Failed to save file: " + fname, "danger")
                    return redirect(url_for("upload_post"))

            ext = fname.rsplit(".", 1)[-1].lower()
            if ext in {"png","jpg","jpeg","gif","webp"}:
                mtype = "image"
            elif ext in {"mp4","mov","webm"}:
                mtype = "video"
            elif ext in {"mp3","wav","m4a","ogg"}:
                mtype = "audio"
            elif ext == "pdf":
                mtype = "pdf"
            else:
                mtype = "other"

            saved_files.append((final_fname, mtype))

        created_at = datetime.utcnow().isoformat()
        db = get_db()
        display_caption = title if title else ""
        if caption:
            if display_caption:
                display_caption = f"{display_caption} · {caption}"
            else:
                display_caption = caption

        cur = db.cursor()
        cur.execute(
            "INSERT INTO posts (user_id, caption, media_filename, created_at, visibility) VALUES (?, ?, ?, ?, ?)",
            (user["id"], display_caption, saved_files[0][0] if saved_files else None, created_at, visibility)
        )
        post_id = cur.lastrowid

        ord_idx = 0
        for fname, mtype in saved_files:
            ord_idx += 1
            db.execute(
                "INSERT INTO post_media (post_id, filename, media_type, ord, created_at) VALUES (?, ?, ?, ?, ?)",
                (post_id, fname, mtype, ord_idx, created_at)
            )

        db.commit()
        flash("Post uploaded to your friends!", "success")
        return redirect(url_for("home"))

    return render_template("upload.html", user=user)

# ---------- upload_reel (if you use a frontend that posts single file + song) ----------
@app.route("/upload_reel", methods=["POST"])
def upload_reel():
    init_db()
    user = current_user()
    if not user:
        return jsonify({"ok": False, "message": "Not logged in"}), 401

    file = request.files.get("video")
    song = request.form.get("song", "").strip()  # optional song filename from static/audio_library

    if not file or not file.filename:
        return jsonify({"ok": False, "message": "No video received"}), 400

    def safe_name(n):
        return "".join(c if (c.isalnum() or c in "._-") else "_" for c in n)

    orig_fname = secure_filename(file.filename)
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    tmp_name = f"tmp_reel_{stamp}_{safe_name(orig_fname)}"
    tmp_path = os.path.join(app.config["UPLOAD_FOLDER"], tmp_name)

    try:
        file.save(tmp_path)
    except Exception as e:
        print("UPLOAD_REEL: save error:", e)
        return jsonify({"ok": False, "message": "Save failed"}), 500

    final_name = f"reel_{stamp}_{safe_name(orig_fname)}"
    final_path = os.path.join(app.config["UPLOAD_FOLDER"], final_name)

    if song:
        song_safe = safe_name(song)
        song_path = os.path.join(BASE_DIR, "static", "audio_library", song_safe)
        if not os.path.exists(song_path):
            alt = os.path.join(BASE_DIR, "static", "audio_library", song)
            if os.path.exists(alt):
                song_path = alt
            else:
                song_path = None

        if song_path:
            merged_tmp = os.path.join(app.config["UPLOAD_FOLDER"], f"merged_{stamp}_{safe_name(orig_fname)}.mp4")
            cmd = [
                "ffmpeg", "-y",
                "-i", tmp_path,
                "-i", song_path,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-shortest",
                "-t", "60",
                merged_tmp
            ]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if proc.returncode != 0 or not os.path.exists(merged_tmp):
                # fallback full re-encode
                merged_tmp = os.path.join(app.config["UPLOAD_FOLDER"], f"merged2_{stamp}_{safe_name(orig_fname)}.mp4")
                cmd2 = [
                    "ffmpeg", "-y",
                    "-i", tmp_path,
                    "-i", song_path,
                    "-map", "0:v:0",
                    "-map", "1:a:0",
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "23",
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-shortest",
                    "-t", "60",
                    merged_tmp
                ]
                proc2 = subprocess.run(cmd2, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            if os.path.exists(merged_tmp):
                try:
                    os.replace(merged_tmp, final_path)
                    try:
                        os.remove(tmp_path)
                    except:
                        pass
                except Exception as e:
                    print("UPLOAD_REEL: move merged to final failed:", e)
                    os.replace(tmp_path, final_path)
            else:
                os.replace(tmp_path, final_path)
        else:
            os.replace(tmp_path, final_path)
    else:
        os.replace(tmp_path, final_path)

    caption = "Reel"
    if song:
        caption += f" · Song: {song}"
    created_at = datetime.utcnow().isoformat()
    db = get_db()
    try:
        db.execute(
            "INSERT INTO posts (user_id, caption, media_filename, created_at, visibility) VALUES (?, ?, ?, ?, ?)",
            (user["id"], caption, final_name, created_at, "public")
        )
    except Exception:
        db.execute(
            "INSERT INTO posts (user_id, caption, media_filename, created_at) VALUES (?, ?, ?, ?)",
            (user["id"], caption, final_name, created_at)
        )
    db.commit()
    return jsonify({"ok": True, "redirect": url_for("home")})

# ---------- DELETE post (owner only) ----------
@app.route("/delete_post/<int:post_id>", methods=["POST"])
def delete_post(post_id):
    init_db()
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    db = get_db()
    post = db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if not post:
        flash("Post not found.", "danger")
        return redirect(url_for("home"))
    if post["user_id"] != user["id"]:
        flash("Not allowed.", "danger")
        return redirect(url_for("home"))

    # remove main file
    if post["media_filename"]:
        try:
            os.remove(os.path.join(app.config["UPLOAD_FOLDER"], post["media_filename"]))
        except Exception:
            pass

    # remove all post_media files and entries
    media_rows = db.execute("SELECT filename FROM post_media WHERE post_id = ?", (post_id,)).fetchall()
    for m in media_rows:
        try:
            os.remove(os.path.join(app.config["UPLOAD_FOLDER"], m["filename"]))
        except Exception:
            pass
    db.execute("DELETE FROM post_media WHERE post_id = ?", (post_id,))

    db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    db.execute("DELETE FROM likes WHERE post_id = ?", (post_id,))
    db.execute("DELETE FROM comments WHERE post_id = ?", (post_id,))
    db.commit()
    flash("Post deleted.", "info")
    return redirect(url_for("profile"))

# ---------- EDIT post (owner only) ----------
@app.route("/edit_post/<int:post_id>", methods=["GET", "POST"])
def edit_post(post_id):
    init_db()
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    db = get_db()
    post = db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if not post:
        flash("Post not found.", "danger")
        return redirect(url_for("profile"))
    if post["user_id"] != user["id"]:
        flash("Not allowed.", "danger")
        return redirect(url_for("profile"))

    if request.method == "POST":
        caption = request.form.get("caption", "").strip()
        file = request.files.get("media")
        media_fname = post["media_filename"]

        if file and file.filename:
            if not allowed_file(file.filename):
                flash("File type not allowed.", "danger")
                return redirect(url_for("edit_post", post_id=post_id))
            try:
                if post["media_filename"]:
                    old = os.path.join(app.config["UPLOAD_FOLDER"], post["media_filename"])
                    if os.path.exists(old):
                        os.remove(old)
            except Exception:
                pass
            fname = secure_filename(file.filename)
            stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
            media_fname = f"{stamp}_{fname}"
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], media_fname))

        db.execute("UPDATE posts SET caption = ?, media_filename = ? WHERE id = ?", (caption, media_fname, post_id))
        db.commit()
        flash("Post updated.", "success")
        return redirect(url_for("profile"))

    return render_template("edit_post.html", post=post)

# ---------- LIKE / DISLIKE toggle ----------
@app.route("/like/<int:post_id>", methods=["POST"])
def like_post(post_id):
    init_db()
    user = current_user()
    if not user:
        flash("Please login first.", "danger")
        return redirect(url_for("login"))

    try:
        value = int(request.form.get("value", "1"))
        if value not in (1, -1):
            value = 1
    except:
        value = 1

    db = get_db()

    post = db.execute("SELECT user_id FROM posts WHERE id = ?", (post_id,)).fetchone()
    post_owner_id = post["user_id"] if post else None

    existing = db.execute("SELECT * FROM likes WHERE user_id = ? AND post_id = ?", (user["id"], post_id)).fetchone()

    if not existing:
        db.execute("INSERT INTO likes (user_id, post_id, value, created_at) VALUES (?, ?, ?, ?)",
                   (user["id"], post_id, value, datetime.utcnow().isoformat()))
        if value == 1 and post_owner_id and post_owner_id != user["id"]:
            create_notification(post_owner_id, "like", from_user_id=user["id"], post_id=post_id)
    else:
        if existing["value"] == value:
            db.execute("DELETE FROM likes WHERE id = ?", (existing["id"],))
        else:
            db.execute("UPDATE likes SET value = ?, created_at = ? WHERE id = ?",
                       (value, datetime.utcnow().isoformat(), existing["id"]))
            if value == 1 and post_owner_id and post_owner_id != user["id"]:
                create_notification(post_owner_id, "like", from_user_id=user["id"], post_id=post_id)

    db.commit()
    return redirect(request.referrer or url_for("home"))

# ---------- COMMENTS page + add comment ----------
@app.route("/post/<int:post_id>/comments", methods=["GET", "POST"])
def post_comments(post_id):
    init_db()
    user = current_user()
    db = get_db()
    post = db.execute("SELECT p.*, u.display_name FROM posts p JOIN users u ON p.user_id = u.id WHERE p.id = ?", (post_id,)).fetchone()
    if not post:
        flash("Post not found.", "danger")
        return redirect(url_for("home"))

    if request.method == "POST":
        if not user:
            flash("Please login to comment.", "danger")
            return redirect(url_for("login"))
        text = request.form.get("text", "").strip()
        if not text:
            flash("Please write a comment.", "danger")
            return redirect(url_for("post_comments", post_id=post_id))

        db.execute("INSERT INTO comments (user_id, post_id, text, created_at) VALUES (?, ?, ?, ?)",
                   (user["id"], post_id, text, datetime.utcnow().isoformat()))
        db.commit()

        if post["user_id"] != user["id"]:
            create_notification(post["user_id"], "comment", from_user_id=user["id"], post_id=post_id)

        flash("Comment added.", "success")
        return redirect(url_for("post_comments", post_id=post_id))

    comments = db.execute("SELECT c.*, u.display_name FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC", (post_id,)).fetchall()
    likes_count = db.execute("SELECT COUNT(*) AS c FROM likes WHERE post_id = ? AND value = 1", (post_id,)).fetchone()["c"]
    dislikes_count = db.execute("SELECT COUNT(*) AS c FROM likes WHERE post_id = ? AND value = -1", (post_id,)).fetchone()["c"]
    return render_template("comments.html", post=post, comments=comments, likes=likes_count, dislikes=dislikes_count, user=user)

# ---------- HOME feed ----------
@app.route("/home")
def home():
    init_db()
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    db = get_db()
    posts = db.execute("""
        SELECT p.*, u.display_name, u.kidsta_id, u.id as author_id
        FROM posts p
        JOIN users u ON p.user_id = u.id
        ORDER BY p.created_at DESC
    """).fetchall()

    posts_with_meta = []
    for p in posts:
        author_id = p["author_id"]

        # block check
        if is_blocked(user["id"], author_id):
            continue

        vis = p["visibility"] if "visibility" in p.keys() else "public"
        if vis == "friends":
            if author_id != user["id"] and not are_friends(user["id"], author_id):
                continue

        pid = p["id"]
        likes_count = db.execute("SELECT COUNT(*) AS c FROM likes WHERE post_id = ? AND value = 1", (pid,)).fetchone()["c"]
        dislikes_count = db.execute("SELECT COUNT(*) AS c FROM likes WHERE post_id = ? AND value = -1", (pid,)).fetchone()["c"]
        comments_count = db.execute("SELECT COUNT(*) AS c FROM comments WHERE post_id = ?", (pid,)).fetchone()["c"]
        own = db.execute("SELECT value FROM likes WHERE post_id = ? AND user_id = ?", (pid, user["id"])).fetchone()
        own_value = own["value"] if own else 0

        # fetch media list
        media_rows = db.execute("SELECT filename, media_type FROM post_media WHERE post_id = ? ORDER BY ord ASC", (pid,)).fetchall()
        media_list = []
        for m in media_rows:
            media_list.append({"filename": m["filename"], "media_type": m["media_type"]})

        # fallback: if no post_media rows but posts.media_filename exists, use that single file
        if not media_list and p["media_filename"]:
            fname = p["media_filename"]
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            if ext in {"png","jpg","jpeg","gif","webp"}:
                mtype = "image"
            elif ext in {"mp4","mov","webm"}:
                mtype = "video"
            elif ext in {"mp3","wav","m4a","ogg"}:
                mtype = "audio"
            elif ext == "pdf":
                mtype = "pdf"
            else:
                mtype = "other"
            media_list.append({"filename": fname, "media_type": mtype})

        posts_with_meta.append({
            "post": p,
            "likes": likes_count,
            "dislikes": dislikes_count,
            "comments": comments_count,
            "own": own_value,
            "media": media_list
        })

    return render_template("home.html", posts=posts_with_meta, user_id=user["id"])

# ---------- REPORT USER ----------
@app.route("/report_user/<int:reported_id>", methods=["POST"])
def report_user(reported_id):
    init_db()
    user = current_user()
    if not user:
        flash("Please login first.", "danger")
        return redirect(url_for("login"))

    reason = request.form.get("reason", "").strip()
    if not reason:
        reason = "No reason given"

    db = get_db()
    created_at = datetime.utcnow().isoformat()
    db.execute(
        "INSERT INTO reports (reporter_id, reported_id, reason, created_at) VALUES (?, ?, ?, ?)",
        (user["id"], reported_id, reason, created_at)
    )
    db.commit()
    flash("Thanks for reporting. Our team will review this.", "info")
    return redirect(request.referrer or url_for("home"))

# ---------- BLOCK / UNBLOCK / FRIENDS endpoints left unchanged ----------
# (kept from your file - delete/unblock/send_friend/respond_friend etc.)

@app.route("/send_friend/<int:from_id>/<int:to_id>", methods=["POST"])
def send_friend(from_id, to_id):
    init_db()
    db = get_db()
    existing = db.execute("SELECT * FROM friends WHERE user_id = ? AND friend_id = ?", (from_id, to_id)).fetchone()
    if existing:
        flash("Friend request already exists.", "info")
        return redirect(request.referrer or url_for("search"))
    created_at = datetime.utcnow().isoformat()
    db.execute("INSERT INTO friends (user_id, friend_id, status, created_at) VALUES (?, ?, 'pending', ?)", (from_id, to_id, created_at))
    db.commit()
    create_notification(to_id, "friend_request", from_user_id=from_id)
    flash("Friend request sent.", "success")
    return redirect(request.referrer or url_for("search"))

@app.route("/friend_requests")
def friend_requests():
    init_db()
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    db = get_db()
    reqs = db.execute("SELECT f.id, f.user_id as from_id, u.display_name, u.kidsta_id FROM friends f JOIN users u ON f.user_id = u.id WHERE f.friend_id = ? AND f.status = 'pending'", (user["id"],)).fetchall()
    return render_template("friend_requests.html", reqs=reqs, user_id=user["id"])

@app.route("/respond_friend/<int:request_id>/<action>", methods=["POST"])
def respond_friend(request_id, action):
    init_db()
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    db = get_db()
    rec = db.execute("SELECT * FROM friends WHERE id = ?", (request_id,)).fetchone()
    if not rec:
        flash("Request not found.", "danger")
        return redirect(url_for("friend_requests"))
    if action == "accept":
        db.execute("UPDATE friends SET status = 'accepted' WHERE id = ?", (request_id,))
        created_at = datetime.utcnow().isoformat()
        db.execute("INSERT INTO friends (user_id, friend_id, status, created_at) VALUES (?, ?, 'accepted', ?)",
                   (rec["friend_id"], rec["user_id"], created_at))
        db.commit()
        create_notification(rec["user_id"], "friend_accept", from_user_id=user["id"])
        flash("Friend request accepted.", "success")
    else:
        db.execute("UPDATE friends SET status = 'denied' WHERE id = ?", (request_id,))
        db.commit()
        flash("Friend request denied.", "info")
    return redirect(url_for("friend_requests"))

@app.route("/search")
def search():
    init_db()
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    q = request.args.get("q", "").strip()
    results = []
    db = get_db()
    if q:
        results = db.execute("SELECT id, display_name, kidsta_id, avatar_filename FROM users WHERE kidsta_id LIKE ? LIMIT 10", (f"%{q}%",)).fetchall()

    friend_rows = db.execute("SELECT friend_id FROM friends WHERE user_id = ? AND status = 'accepted'", (user["id"],)).fetchall()
    pending_rows = db.execute("SELECT friend_id FROM friends WHERE user_id = ? AND status = 'pending'", (user["id"],)).fetchall()
    friend_ids = [r["friend_id"] for r in friend_rows]
    pending_ids = [r["friend_id"] for r in pending_rows]

    filtered_results = []
    for r in results:
        if is_blocked(user["id"], r["id"]):
            continue
        filtered_results.append(r)

    return render_template("search.html", results=filtered_results, user_id=user["id"], q=q, friend_ids=friend_ids, pending_ids=pending_ids)

# Add this near other routes in app.py
import base64

@app.route("/make_slideshow", methods=["GET", "POST"])
def make_slideshow():
    init_db()
    user = current_user()
    if not user:
        return jsonify({"ok": False, "error": "login needed"}), 401

    try:
        count = int(request.form.get("count", 0))
    except:
        return jsonify({"ok": False, "error": "no photos provided"}), 400

    if count == 0:
        return jsonify({"ok": False, "error": "no photos provided"}), 400

    # save photos from data URLs (photo0, photo1, ...)
    photo_paths = []
    for i in range(count):
        data_url = request.form.get(f"photo{i}")
        if not data_url:
            continue
        header, encoded = data_url.split(",", 1)
        img_bytes = base64.b64decode(encoded)
        fname = f"slide_{i}_{int(datetime.utcnow().timestamp())}.jpg"
        fullpath = os.path.join(app.config["UPLOAD_FOLDER"], fname)
        with open(fullpath, "wb") as f:
            f.write(img_bytes)
        photo_paths.append(fullpath)

    if not photo_paths:
        return jsonify({"ok": False, "error": "no valid photos"}), 400

    # make ffmpeg list file
    list_file = os.path.join(app.config["UPLOAD_FOLDER"], f"slides_{int(datetime.utcnow().timestamp())}.txt")
    with open(list_file, "w") as f:
        for p in photo_paths:
            f.write(f"file '{p}'\n")
            f.write("duration 3\n")
        f.write(f"file '{photo_paths[-1]}'\n")  # last frame repeat

    # optional song
    song = request.form.get("song", "").strip()
    song_path = None
    if song:
        candidate = os.path.join(BASE_DIR, "static", "audio_library", song)
        if os.path.exists(candidate):
            song_path = candidate

    out_name = f"slideshow_{int(datetime.utcnow().timestamp())}.mp4"
    out_path = os.path.join(app.config["UPLOAD_FOLDER"], out_name)

    # ffmpeg command
    if song_path:
        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
            "-i", song_path, "-shortest", "-vf", "scale=720:1280", out_path
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
            "-vf", "scale=720:1280", out_path
        ]

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        print("make_slideshow ffmpeg rc:", proc.returncode)
    except Exception as e:
        print("make_slideshow ffmpeg fail:", e)
        return jsonify({"ok": False, "error": "ffmpeg failed"}), 500

    # store post in DB
    caption = request.form.get("title") or "Photo Slideshow"
    created_at = datetime.utcnow().isoformat()
    db = get_db()
    try:
        db.execute("INSERT INTO posts (user_id, caption, media_filename, created_at, visibility) VALUES (?, ?, ?, ?, ?)",
                   (user["id"], caption, out_name, created_at, "friends"))
    except Exception:
        db.execute("INSERT INTO posts (user_id, caption, media_filename, created_at) VALUES (?, ?, ?, ?)",
                   (user["id"], caption, out_name, created_at))
    db.commit()

    return jsonify({"ok": True, "redirect": url_for("home")})


# Returns list of audio filenames in static/audio_library (JSON)
@app.route("/api/audio_files")
def api_audio_files():
    try:
        folder = os.path.join(BASE_DIR, "static", "audio_library")
        files = []
        # only list normal audio extensions
        for f in os.listdir(folder):
            if f.lower().endswith((".mp3", ".wav", ".m4a", ".ogg")):
                files.append(f)
        files.sort()
    except Exception as e:
        # if folder missing or error, return empty list
        print("api_audio_files error:", e)
        files = []
    return jsonify(files)


@app.route("/notifications")
def notifications():
    init_db()
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    db = get_db()
    rows = db.execute("""
        SELECT n.*, u.display_name AS from_name
        FROM notifications n
        LEFT JOIN users u ON n.from_user_id = u.id
        WHERE n.user_id = ?
        ORDER BY n.created_at DESC
        LIMIT 50
    """, (user["id"],)).fetchall()

    try:
        db.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ?", (user["id"],))
        db.commit()
    except Exception:
        pass

    return render_template("notifications.html", notifications=rows)

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

@app.route("/about")
def about():
    return render_template("about.html")

# ---------- Run ----------
if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(debug=True, port=5001)
