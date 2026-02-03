#!/usr/bin/env python3
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
import base64
from flask import Flask, render_template, request, jsonify
import os
import subprocess
from datetime import datetime
# ...whatever imports you already have...

# --- small-image fixer for ffmpeg (paste after imports) ---
from PIL import Image

def ensure_min_image_size(path, min_w=720, min_h=1280, bg_color=(255,255,255)):
    """
    If the image at `path` is smaller than min_w/min_h, create a new image
    min_w x min_h with bg_color and center the original image on it.
    Overwrites the original file with an RGB PNG.
    """
    try:
        with Image.open(path) as im:
            w, h = im.size
            if w >= min_w and h >= min_h:
                return path

            if im.mode != "RGBA":
                im = im.convert("RGBA")

            bg = Image.new("RGBA", (min_w, min_h), bg_color + (255,))
            x = (min_w - w) // 2
            y = (min_h - h) // 2
            bg.paste(im, (x, y), im)

            bg_rgb = bg.convert("RGB")
            bg_rgb.save(path, format="PNG")

    except Exception as e:
        print("ensure_min_image_size failed:", e)

    return path
# --- end helper ---

# --- paste this near the top of app.py (with other imports) ---
from io import BytesIO
from PIL import Image, ImageOps

def save_image_safely(file_storage, out_path, target_size=(720, 1280), fill_color=(255, 255, 255)):
    """
    Save an uploaded image (werkzeug FileStorage or file-like) to out_path.
    - If the image is tiny or invalid, create a target_size canvas and center the image on it.
    - Always write a PNG/RGB file that ffmpeg can consume.
    """
    # read bytes (works for FileStorage and file-like objects)
    try:
        data = file_storage.read()
    except Exception:
        # fallback: attempt direct save if read() fails
        try:
            with open(out_path, "wb") as f:
                file_storage.save(f)
        except Exception:
            raise
        return

    # Try open with Pillow
    try:
        img = Image.open(BytesIO(data))
        img = img.convert("RGBA")
    except Exception:
        # if Pillow can't open, write raw bytes to disk
        with open(out_path, "wb") as f:
            f.write(data)
        return

    w, h = img.size

    # If image is very small (1x1 or tiny), place it on a target canvas
    if w < 50 or h < 50 or (w == 1 and h == 1):
        canvas = Image.new("RGBA", (target_size[0], target_size[1]), fill_color + (255,))
        thumb = ImageOps.contain(img, target_size)  # keep aspect ratio
        paste_x = (target_size[0] - thumb.width) // 2
        paste_y = (target_size[1] - thumb.height) // 2
        canvas.paste(thumb, (paste_x, paste_y), thumb)
        canvas.convert("RGB").save(out_path, format="PNG")
    else:
        # For normal images, just ensure RGB PNG (so ffmpeg won't choke)
        img.convert("RGB").save(out_path, format="PNG")

    # try to reset file_storage pointer for callers that expect it (best effort)
    try:
        file_storage.seek(0)
    except Exception:
        pass
# --- end helper ---

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
app.secret_key = "change_this_for_production"  # change for real deployment
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB

# Keep session alive
app.permanent_session_lifetime = timedelta(days=30)

# ---------- ACRCloud config (fill these if needed) ----------
ACR_HOST = "https://identify-eu-west-1.acrcloud.com/v1/identify"  # example endpoint
ACCESS_KEY = "YOUR_ACR_KEY"
ACCESS_SECRET = "YOUR_ACR_SECRET"

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

    # users (added bio column so edit_profile can save bio)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        age INTEGER NOT NULL,
        kidsta_id TEXT UNIQUE,
        display_name TEXT,
        avatar_filename TEXT,
        bio TEXT,
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

    # notifications
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

    # reports
    cur.execute("""
    CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reporter_id INTEGER NOT NULL,
        reported_id INTEGER NOT NULL,
        reason TEXT,
        created_at TEXT NOT NULL
    )
    """)

    # blocks
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

def get_current_user():
    """
    Returns sqlite3.Row for logged-in user or None
    """
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

@app.route("/make_slideshow", methods=["POST"])
def make_slideshow():
    init_db()
    user = get_current_user()
    song = (request.form.get("song") or "").strip()
    # Ensure song_path exists from the very beginning to avoid NameError
    song_path = None

    if not user:
        return jsonify({"ok": False, "error": "login needed"}), 401

    def safe_name(n):
        return "".join(c if (c.isalnum() or c in "._-") else "_" for c in n)

    tmp_items = []       # paths to parts that will be concatenated
    tmp_to_cleanup = []  # files to remove in finally

    try:
        # re-read/confirm incoming params
        song = (request.form.get("song") or "").strip()

        # 1) Read incoming photos (data-URL) and uploaded files
        photos = []
        try:
            count = int(request.form.get("count", 0))
        except Exception:
            count = 0

        for i in range(count):
            d = request.form.get(f"photo{i}")
            if d and isinstance(d, str) and d.startswith("data:"):
                photos.append(d)

        uploaded_files = request.files.getlist("media") if "media" in request.files else []

        if not photos and not uploaded_files:
            return jsonify({"ok": False, "error": "no valid photos or media provided"}), 400

        os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

        # Convert data-URL photos -> short mp4s (3s)
        import base64, subprocess, shutil
        for idx, data_url in enumerate(photos):
            try:
                header, b64 = data_url.split(",", 1)
                img_bytes = base64.b64decode(b64)
            except Exception:
                continue
            fname = f"photo_{idx}_{int(datetime.utcnow().timestamp())}.jpg"
            img_path = os.path.join(app.config["UPLOAD_FOLDER"], fname)
            with open(img_path, "wb") as f:
                f.write(img_bytes)

            out_tmp = os.path.join(app.config["UPLOAD_FOLDER"], f"tmp_img_{idx}_{int(datetime.utcnow().timestamp())}.mp4")
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1",
                "-i", img_path,
                "-c:v", "libx264",
                "-t", "3",
                "-pix_fmt", "yuv420p",
                "-vf", "scale=720:1280,setsar=1",
                out_tmp
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(out_tmp):
                tmp_items.append(out_tmp)
                tmp_to_cleanup.extend([img_path, out_tmp])
            else:
                tmp_to_cleanup.append(img_path)

        # 3) Process uploaded files (images -> short mp4; videos -> re-encode full length)
        for fidx, fobj in enumerate(uploaded_files):
            if not fobj or not getattr(fobj, "filename", None):
                continue
            orig = secure_filename(fobj.filename)
            stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
            saved = os.path.join(app.config["UPLOAD_FOLDER"], f"{stamp}_{safe_name(orig)}")
            fobj.save(saved)

            ext = orig.rsplit(".", 1)[-1].lower() if "." in orig else ""

            if ext in ("png", "jpg", "jpeg", "gif", "webp", "bmp"):
                out_tmp = os.path.join(app.config["UPLOAD_FOLDER"], f"tmp_upimg_{fidx}_{stamp}.mp4")
                cmd = [
                    "ffmpeg", "-y",
                    "-loop", "1",
                    "-i", saved,
                    "-c:v", "libx264",
                    "-t", "3",
                    "-pix_fmt", "yuv420p",
                    "-vf", "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2",
                    out_tmp
                ]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if os.path.exists(out_tmp):
                    tmp_items.append(out_tmp)
                    tmp_to_cleanup.extend([saved, out_tmp])
                else:
                    tmp_items.append(saved)
                    tmp_to_cleanup.append(saved)
            else:
                out_tmp = os.path.join(app.config["UPLOAD_FOLDER"], f"tmp_upvid_{fidx}_{stamp}.mp4")
                cmd = [
                    "ffmpeg", "-y",
                    "-i", saved,
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "23",
                    "-pix_fmt", "yuv420p",
                    "-vf", "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2",
                    "-c:a", "aac",
                    "-b:a", "128k",
                    out_tmp
                ]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if os.path.exists(out_tmp):
                    tmp_items.append(out_tmp)
                    tmp_to_cleanup.extend([saved, out_tmp])
                else:
                    tmp_items.append(saved)
                    tmp_to_cleanup.append(saved)

        if len(tmp_items) == 0:
            return jsonify({"ok": False, "error": "no valid media after processing"}), 400

        # 4) Write concat list file for ffmpeg concat demuxer
        list_file = os.path.join(app.config["UPLOAD_FOLDER"], f"concat_{int(datetime.utcnow().timestamp())}.txt")
        with open(list_file, "w", encoding="utf-8") as lf:
            for p in tmp_items:
                lf.write(f"file '{p}'\n")
        tmp_to_cleanup.append(list_file)

        out_name = f"slideshow_{int(datetime.utcnow().timestamp())}.mp4"
        out_path = os.path.join(app.config["UPLOAD_FOLDER"], out_name)

        # ----- Resolve song_path early if a song name provided -----
        if song:
            # try exact, safe-name, and common extensions
            audio_dir = os.path.join(BASE_DIR, "static", "audio_library")
            candidate = os.path.join(audio_dir, song)
            if os.path.exists(candidate):
                song_path = candidate
            else:
                song_safe = safe_name(song)
                alt = os.path.join(audio_dir, song_safe)
                if os.path.exists(alt):
                    song_path = alt
                else:
                    # try common extensions appended to safe name
                    for ext in ("mp3", "m4a", "wav", "aac", "ogg"):
                        candidate_ext = os.path.join(audio_dir, f"{song_safe}.{ext}")
                        if os.path.exists(candidate_ext):
                            song_path = candidate_ext
                            break
            # if still None, we'll just ignore replacing audio later (no crash)

        # 5) Re-encode final file using concat (keeps full lengths)
        if song_path:
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0", "-i", list_file,
                "-i", song_path,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-pix_fmt", "yuv420p",
                "-shortest",
                out_path
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0", "-i", list_file,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-pix_fmt", "yuv420p",
                out_path
            ]

        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        if proc.returncode != 0 or not os.path.exists(out_path):
            # include stderr to help debug
            return jsonify({"ok": False, "error": "ffmpeg failed to create final video", "details": proc.stderr[:1000]}), 500

        # 6) If we encoded without the song earlier, but want to replace audio now, do a safe remux (only if song_path exists)
        if song and song_path and os.path.exists(out_path):
            merged = os.path.join(app.config["UPLOAD_FOLDER"], f"merged_{int(datetime.utcnow().timestamp())}.mp4")
            cmdm = [
                "ffmpeg", "-y",
                "-i", out_path,
                "-i", song_path,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-c:a", "aac",
                "-b:a", "192k",
                "-shortest",
                merged
            ]
            procm = subprocess.run(cmdm, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if procm.returncode == 0 and os.path.exists(merged):
                try:
                    os.replace(merged, out_path)
                except Exception:
                    try:
                        shutil.copyfile(merged, out_path)
                        os.remove(merged)
                    except Exception:
                        pass

        # 7) Insert DB row for the created file
        caption = "Photo/Video Slideshow"
        if song:
            caption += f" · Song: {song}"
        created_at = datetime.utcnow().isoformat()
        db = get_db()
        try:
            db.execute(
                "INSERT INTO posts (user_id, caption, media_filename, created_at, visibility) VALUES (?, ?, ?, ?, ?)",
                (user["id"], caption, out_name, created_at, "public")
            )
        except Exception:
            db.execute(
                "INSERT INTO posts (user_id, caption, media_filename, created_at) VALUES (?, ?, ?, ?)",
                (user["id"], caption, out_name, created_at)
            )
        db.commit()

        return jsonify({"ok": True, "video": f"/uploads/{out_name}", "file": out_name})

    except Exception as e:
        print("MAKE_SLIDESHOW ERROR:", str(e))
        return jsonify({"ok": False, "error": "server processing error: " + str(e)}), 500

    finally:
        # cleanup temporary files
        try:
            for p in tmp_to_cleanup:
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except:
                        pass
        except:
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
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))
    return render_template("reels.html", user=user)

# ---------- PROFILE SETUP ----------
@app.route("/profile_setup", methods=["GET", "POST"])
def profile_setup():
    init_db()
    user = get_current_user()
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
    user = get_current_user()
    if not user:
        flash("Please login first.", "danger")
        return redirect(url_for("login"))

    db = get_db()
    posts = db.execute("SELECT * FROM posts WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)).fetchall()
    pending = db.execute("SELECT COUNT(*) AS c FROM friends WHERE friend_id = ? AND status = 'pending'", (user["id"],)).fetchone()["c"]
    return render_template("profile.html", user=user, posts=posts, pending_requests=pending)

# --- Safe helper functions for counts (add if not present) ---
def get_like_count(post_id):
    try:
        cur = get_db().cursor()
        cur.execute("SELECT COUNT(*) AS c FROM likes WHERE post_id = ? AND value = 1", (post_id,))
        row = cur.fetchone()
        if row and ("c" in row.keys()):
            return row["c"]
        cur.execute("SELECT COUNT(*) AS c FROM likes WHERE post_id = ?", (post_id,))
        row = cur.fetchone()
        return row["c"] if row else 0
    except Exception:
        return 0

def get_dislike_count(post_id):
    try:
        cur = get_db().cursor()
        cur.execute("SELECT COUNT(*) AS c FROM likes WHERE post_id = ? AND value = -1", (post_id,))
        row = cur.fetchone()
        if row and ("c" in row.keys()):
            return row["c"]
        cur.execute("SELECT COUNT(*) AS c FROM dislikes WHERE post_id = ?", (post_id,))
        row = cur.fetchone()
        return row["c"] if row else 0
    except Exception:
        return 0

def get_comment_count(post_id):
    try:
        cur = get_db().cursor()
        cur.execute("SELECT COUNT(*) AS c FROM comments WHERE post_id = ?", (post_id,))
        row = cur.fetchone()
        return row["c"] if row else 0
    except Exception:
        return 0

# ---------- UPLOAD (new post) ----------
@app.route("/upload", methods=["GET", "POST"])
def upload_post():
    init_db()
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        caption = request.form.get("caption", "").strip()
        visibility = "friends"
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

# ---------- upload_reel ----------
@app.route("/upload_reel", methods=["POST"])
def upload_reel():
    init_db()
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "message": "Not logged in"}), 401

    file = request.files.get("video")
    song = request.form.get("song", "").strip()

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
    user = get_current_user()
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
    user = get_current_user()
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
    user = get_current_user()
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
    user = get_current_user()
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
    # 1. Check login
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    # 2. Get logged-in user info
    cur = get_db().cursor()
    cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    user = cur.fetchone()
    if not user:
        return redirect("/login")

    # 3. Fetch all posts (with user info + avatar)
    cur.execute("""
        SELECT 
            posts.id AS post_id,
            posts.caption,
            posts.user_id,
            posts.media_filename AS legacy_media,
            posts.created_at,
            users.display_name,
            users.kidsta_id,
            users.avatar_filename AS avatar_filename
        FROM posts
        JOIN users ON posts.user_id = users.id
        ORDER BY posts.id DESC
    """)
    rows = cur.fetchall()

    posts_with_meta = []

    for r in rows:
        # load media rows preferring post_media table (new schema)
        media_rows = []
        try:
            cur.execute("""
                SELECT filename, media_type
                FROM post_media
                WHERE post_id = ?
                ORDER BY ord ASC
            """, (r["post_id"],))
            media_rows = cur.fetchall() or []
        except Exception:
            media_rows = []

        # If no post_media rows but there is a legacy media filename, add it as single media
        if (not media_rows) and r["legacy_media"]:
            fname = r["legacy_media"]
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            if ext in {"png", "jpg", "jpeg", "gif", "webp"}:
                mtype = "image"
            elif ext in {"mp4", "mov", "webm"}:
                mtype = "video"
            elif ext in {"mp3", "wav", "m4a", "ogg"}:
                mtype = "audio"
            elif ext == "pdf":
                mtype = "pdf"
            else:
                mtype = "other"
            # create a minimal row-like dict compatible with Jinja usage (filename, media_type)
            media_rows = [{"filename": fname, "media_type": mtype}]

        # Build post object and add avatar filename so templates can show avatar
        post_obj = {
            "id": r["post_id"],
            "caption": r["caption"],
            "created_at": r["created_at"],
            "display_name": r["display_name"],
            "kidsta_id": r["kidsta_id"],
            "avatar": r["avatar_filename"]  # may be None
        }

        posts_with_meta.append({
            "post": post_obj,
            "media": media_rows,
            "likes": get_like_count(r["post_id"]),
            "dislikes": get_dislike_count(r["post_id"]),
            "comments": get_comment_count(r["post_id"])
        })

    # 4. SEARCH FILTER (if q provided)
    q = (request.args.get("q") or "").strip().lower()
    if q:
        filtered_posts = []
        for item in posts_with_meta:
            p = item["post"]
            cap = (p.get("caption") or "").lower() if isinstance(p, dict) else (p["caption"] or "").lower()
            name = (p.get("display_name") or "").lower() if isinstance(p, dict) else (p["display_name"] or "").lower()
            kid = (p.get("kidsta_id") or "").lower() if isinstance(p, dict) else (p["kidsta_id"] or "").lower()
            if q in cap or q in name or q in kid:
                filtered_posts.append(item)
        posts_with_meta = filtered_posts

    # 5. Render template
    return render_template(
        "home.html",
        posts=posts_with_meta,
        user_id=user_id
    )


# ---------- REPORT USER ----------
@app.route("/report_user/<int:reported_id>", methods=["POST"])
def report_user(reported_id):
    init_db()
    user = get_current_user()
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

# ---------- FRIEND / BLOCK endpoints ----------
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
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))
    db = get_db()
    reqs = db.execute("SELECT f.id, f.user_id as from_id, u.display_name, u.kidsta_id FROM friends f JOIN users u ON f.user_id = u.id WHERE f.friend_id = ? AND f.status = 'pending'", (user["id"],)).fetchall()
    return render_template("friend_requests.html", reqs=reqs, user_id=user["id"])

@app.route("/respond_friend/<int:request_id>/<action>", methods=["POST"])
def respond_friend(request_id, action):
    init_db()
    user = get_current_user()
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
    user = get_current_user()
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

#@app.route("/make_slideshow", methods=["POST"], endpoint="make_slideshow_secondary")
def make_slideshow():
    init_db()
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "login needed"}), 401

    # Ensure song_path exists from the very beginning to avoid NameError
    song_path = None

    # read selected song name
    song = (request.form.get("song") or "").strip()

    def safe_name(n):
        return "".join(c if (c.isalnum() or c in "._-") else "_" for c in n)

    tmp_items = []       # paths to parts that will be concatenated
    tmp_to_cleanup = []  # files to remove in finally

    try:
        # 1) Read incoming photos (data-URL) and uploaded files
        photos = []
        try:
            count = int(request.form.get("count", 0))
        except Exception:
            count = 0

        for i in range(count):
            d = request.form.get(f"photo{i}")
            if d and isinstance(d, str) and d.startswith("data:"):
                photos.append(d)

        uploaded_files = request.files.getlist("media") if "media" in request.files else []

        if not photos and not uploaded_files:
            return jsonify({"ok": False, "error": "no valid photos or media provided"}), 400

        os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

        # 2) Convert data-URL photos -> short mp4s (3s)
        for idx, data_url in enumerate(photos):
            try:
                header, b64 = data_url.split(",", 1)
                img_bytes = base64.b64decode(b64)
            except Exception:
                continue
            fname = f"photo_{idx}_{int(datetime.utcnow().timestamp())}.jpg"
            img_path = os.path.join(app.config["UPLOAD_FOLDER"], fname)
            with open(img_path, "wb") as f:
                f.write(img_bytes)

            out_tmp = os.path.join(app.config["UPLOAD_FOLDER"], f"tmp_img_{idx}_{int(datetime.utcnow().timestamp())}.mp4")
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1",
                "-i", img_path,
                "-c:v", "libx264",
                "-t", "3",
                "-pix_fmt", "yuv420p",
                "-vf", "scale=720:1280,setsar=1",
                out_tmp
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(out_tmp):
                tmp_items.append(out_tmp)
                tmp_to_cleanup.extend([img_path, out_tmp])
            else:
                tmp_to_cleanup.append(img_path)

        # 3) Process uploaded files (images -> short mp4; videos -> re-encode full length)
        for fidx, fobj in enumerate(uploaded_files):
            if not fobj or not getattr(fobj, "filename", None):
                continue
            orig = secure_filename(fobj.filename)
            stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
            saved = os.path.join(app.config["UPLOAD_FOLDER"], f"{stamp}_{safe_name(orig)}")
            fobj.save(saved)

            ext = orig.rsplit(".", 1)[-1].lower() if "." in orig else ""

            if ext in ("png", "jpg", "jpeg", "gif", "webp", "bmp"):
                out_tmp = os.path.join(app.config["UPLOAD_FOLDER"], f"tmp_upimg_{fidx}_{stamp}.mp4")
                cmd = [
                    "ffmpeg", "-y",
                    "-loop", "1",
                    "-i", saved,
                    "-c:v", "libx264",
                    "-t", "3",
                    "-pix_fmt", "yuv420p",
                    "-vf", "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2",
                    out_tmp
                ]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if os.path.exists(out_tmp):
                    tmp_items.append(out_tmp)
                    tmp_to_cleanup.extend([saved, out_tmp])
                else:
                    tmp_items.append(saved)
                    tmp_to_cleanup.append(saved)
            else:
                # VIDEO -> re-encode to consistent mp4 (KEEP full length)
                out_tmp = os.path.join(app.config["UPLOAD_FOLDER"], f"tmp_upvid_{fidx}_{stamp}.mp4")
                cmd = [
                    "ffmpeg", "-y",
                    "-i", saved,
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "23",
                    "-pix_fmt", "yuv420p",
                    "-vf", "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2",
                    "-c:a", "aac",
                    "-b:a", "128k",
                    out_tmp
                ]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if os.path.exists(out_tmp):
                    tmp_items.append(out_tmp)
                    tmp_to_cleanup.extend([saved, out_tmp])
                else:
                    tmp_items.append(saved)
                    tmp_to_cleanup.append(saved)

        if len(tmp_items) == 0:
            return jsonify({"ok": False, "error": "no valid media after processing"}), 400

        # 4) Write concat list file for ffmpeg concat demuxer
        list_file = os.path.join(app.config["UPLOAD_FOLDER"], f"concat_{int(datetime.utcnow().timestamp())}.txt")
        with open(list_file, "w", encoding="utf-8") as lf:
            for p in tmp_items:
                lf.write(f"file '{p}'\n")
        tmp_to_cleanup.append(list_file)

        out_name = f"slideshow_{int(datetime.utcnow().timestamp())}.mp4"
        out_path = os.path.join(app.config["UPLOAD_FOLDER"], out_name)

        # 5) Re-encode final file using concat (keeps full lengths)
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", list_file,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-pix_fmt", "yuv420p",
            out_path
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        if proc.returncode != 0 or not os.path.exists(out_path):
            return jsonify({"ok": False, "error": "ffmpeg failed to create final video", "details": proc.stderr[:1000]}), 500

        # 6) If a song was selected, replace the video's audio with the chosen song
        if song:
            # try exact name then safe-name fallback then common extensions
            candidate = os.path.join(BASE_DIR, "static", "audio_library", song)
            if os.path.exists(candidate):
                song_path = candidate
            else:
                alt = os.path.join(BASE_DIR, "static", "audio_library", safe_name(song))
                if os.path.exists(alt):
                    song_path = alt
                else:
                    for ext in ("mp3", "m4a", "aac", "wav", "ogg"):
                        candidate_ext = os.path.join(BASE_DIR, "static", "audio_library", f"{song}.{ext}")
                        if os.path.exists(candidate_ext):
                            song_path = candidate_ext
                            break

        # if we found the song file, re-mux the song as the video's audio
        if song_path and os.path.exists(out_path):
            merged = os.path.join(app.config["UPLOAD_FOLDER"], f"merged_{int(datetime.utcnow().timestamp())}.mp4")
            cmdm = [
                "ffmpeg", "-y",
                "-i", out_path,        # the video we created
                "-i", song_path,       # the selected song file
                "-map", "0:v:0",       # take video stream from first input
                "-map", "1:a:0",       # take audio stream from second input (the song)
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-c:a", "aac",
                "-b:a", "192k",
                "-shortest",
                merged
            ]
            procm = subprocess.run(cmdm, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if procm.returncode == 0 and os.path.exists(merged):
                try:
                    os.replace(merged, out_path)
                except Exception:
                    try:
                        shutil.copyfile(merged, out_path)
                        os.remove(merged)
                    except Exception:
                        pass

        # 7) Insert DB row for the created file
        caption = "Photo/Video Slideshow"
        if song:
            caption += f" · Song: {song}"
        created_at = datetime.utcnow().isoformat()
        db = get_db()
        try:
            db.execute(
                "INSERT INTO posts (user_id, caption, media_filename, created_at, visibility) VALUES (?, ?, ?, ?, ?)",
                (user["id"], caption, out_name, created_at, "public")
            )
        except Exception:
            db.execute(
                "INSERT INTO posts (user_id, caption, media_filename, created_at) VALUES (?, ?, ?, ?)",
                (user["id"], caption, out_name, created_at)
            )
        db.commit()

        return jsonify({"ok": True, "video": f"/uploads/{out_name}", "file": out_name})

    except Exception as e:
        print("MAKE_SLIDESHOW ERROR:", str(e))
        return jsonify({"ok": False, "error": "server processing error: " + str(e)}), 500

    finally:
        # cleanup temporary files
        try:
            for p in tmp_to_cleanup:
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except:
                        pass
        except:
            pass


@app.route("/api/audio_files")
def api_audio_files():
    try:
        folder = os.path.join(BASE_DIR, "static", "audio_library")
        files = []
        for f in os.listdir(folder):
            if f.lower().endswith((".mp3", ".wav", ".m4a", ".ogg")):
                files.append(f)
        files.sort()
    except Exception as e:
        print("api_audio_files error:", e)
        files = []
    return jsonify(files)

@app.route("/notifications")
def notifications():
    init_db()
    user = get_current_user()
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

# ---------- EDIT PROFILE (fixed) ----------
@app.route("/edit_profile", methods=["GET", "POST"])
def edit_profile():
    init_db()
    user = get_current_user()
    if not user:
        flash("Please login first.", "danger")
        return redirect(url_for("login"))

    # POST = save changes
    if request.method == "POST":
        display_name = (request.form.get("display_name") or "").strip()
        kidsta_id = (request.form.get("kidsta_id") or "").strip()
        file = request.files.get("avatar_file")

        # current avatar fallback (sqlite3.Row => index access)
        avatar_fname = user["avatar_filename"] if "avatar_filename" in user.keys() else None

        # If user uploaded new avatar, save it
        if file and file.filename:
            if not allowed_file(file.filename):
                flash("File type not allowed.", "danger")
                return redirect(url_for("edit_profile"))
            fname = secure_filename(file.filename)
            stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
            avatar_fname = f"{stamp}_{fname}"
            try:
                file.save(os.path.join(app.config["UPLOAD_FOLDER"], avatar_fname))
            except Exception as e:
                flash("Failed to save avatar.", "danger")
                return redirect(url_for("edit_profile"))

            # Optionally remove old file
            try:
                old = user["avatar_filename"] if "avatar_filename" in user.keys() else None
                if old:
                    old_path = os.path.join(app.config["UPLOAD_FOLDER"], old)
                    if os.path.exists(old_path):
                        os.remove(old_path)
            except Exception:
                pass

        # Use new values if provided, otherwise keep old values from DB row
        new_display = display_name if display_name else (user["display_name"] if "display_name" in user.keys() else None)
        new_kidsta = kidsta_id if kidsta_id else (user["kidsta_id"] if "kidsta_id" in user.keys() else None)

        db = get_db()
        try:
            db.execute(
                "UPDATE users SET display_name = ?, kidsta_id = ?, avatar_filename = ? WHERE id = ?",
                (new_display, new_kidsta, avatar_fname, user["id"])
            )
            db.commit()
        except Exception as e:
            flash("Failed to update profile. Maybe KIDSTA ID already taken.", "danger")
            return redirect(url_for("edit_profile"))

        # update session values so profile page shows new values immediately
        session["display_name"] = new_display
        session["avatar_filename"] = avatar_fname
        flash("Profile updated.", "success")
        return redirect(url_for("profile"))

    # GET = show edit form
    return render_template("edit_profile.html", user=user)

@app.route("/edit_profile", methods=["POST"])
def save_profile():
    init_db()
    user = get_current_user()
    if not user:
        return redirect("/login")

    display_name = request.form.get("display_name", "").strip()
    bio = request.form.get("bio", "").strip()
    file = request.files.get("avatar")

    avatar_fname = user["avatar_filename"]
    if file and file.filename:
        if allowed_file(file.filename):
            fname = secure_filename(file.filename)
            stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
            avatar_fname = f"{stamp}_{fname}"
            try:
                file.save(os.path.join(app.config["UPLOAD_FOLDER"], avatar_fname))
            except Exception:
                pass
        else:
            flash("Avatar file type not allowed.", "danger")
            return redirect(url_for("edit_profile"))

    db = get_db()
    db.execute("UPDATE users SET display_name = ?, bio = ?, avatar_filename = ? WHERE id = ?",
               (display_name or user["display_name"], bio or user.get("bio"), avatar_fname, user["id"]))
    db.commit()

    # Update session values too
    session["display_name"] = display_name or session.get("display_name")
    session["avatar_filename"] = avatar_fname or session.get("avatar_filename")

    flash("Profile updated.", "success")
    return redirect("/profile")

# ---------- Run ----------
if __name__ == "__main__":
    with app.app_context():
        init_db()
    import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

