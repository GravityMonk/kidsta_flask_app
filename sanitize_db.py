#!/usr/bin/env python3
import sqlite3
import re
import os

DB = "kidsta.db"

def clean_name(name):
    if not name:
        return name
    name = name.replace("“", "").replace("”", "").replace("‘", "").replace("’", "")
    name = name.replace('"', "").replace("'", "").replace("🌙", "")
    name = name.strip()
    name = re.sub(r"[^A-Za-z0-9\s\-\_\.\(\)]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.replace(" ", "_")
    return name

def main():
    if not os.path.exists(DB):
        print("DB not found:", DB)
        return
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    rows = cur.execute("SELECT id, caption FROM posts WHERE caption IS NOT NULL").fetchall()
    updated = 0
    for pid, caption in rows:
        if not caption:
            continue
        song = None
        if "SongFile:" in caption:
            song = caption.split("SongFile:")[-1].strip()
        if song:
            cleaned = clean_name(song)
            new_caption = f"SongFile:{cleaned}"
            if new_caption != caption:
                print(f"Updating post {pid}:")
                print("  old caption:", caption)
                print("  new caption:", new_caption)
                cur.execute("UPDATE posts SET caption = ? WHERE id = ?", (new_caption, pid))
                updated += 1

    conn.commit()
    conn.close()
    print("Done. Rows updated:", updated)

if __name__ == "__main__":
    main()
