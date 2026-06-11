from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import timedelta
from functools import wraps

from authlib.integrations.flask_client import OAuth
from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename


BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "database.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")

ALLOWED_GENDERS = {"Male", "Female"}
MAX_RECORDS = 92


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    init_db()

    @app.before_request
    def _load_db():
        g.db = get_db()

    @app.teardown_request
    def _close_db(exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    def is_logged_in() -> bool:
        return bool(session.get("user_id"))

    def login_required(view_func):
        @wraps(view_func)
        def wrapped(*args, **kwargs):
            if not is_logged_in():
                flash("Login Required To Add Characters", "error")
                return redirect(url_for("login"))
            return view_func(*args, **kwargs)

        return wrapped

    oauth = OAuth(app)
    oauth.register(
        name="google",
        client_id=os.environ.get("GOOGLE_CLIENT_ID"),
        client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
        access_token_url="https://oauth2.googleapis.com/token",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        api_base_url="https://www.googleapis.com/oauth2/v1/",
        client_kwargs={"scope": "openid email profile"},
    )

    @app.route("/")
    def index():
        q = (request.args.get("q") or "").strip()
        gender = (request.args.get("gender") or "All").strip()

        logged_in = is_logged_in()
        user_id = session.get("user_id")

        filters = []
        params: list[object] = []

        if logged_in:
            filters.append("user_id = ?")
            params.append(user_id)
        else:
            filters.append("1=0")  # Public view: show no rows (and no data saving)

        if q:
            # Search by character_name OR anime_name (case-insensitive)
            filters.append("(LOWER(character_name) LIKE ? OR LOWER(anime_name) LIKE ?)")
            q_like = f"%{q.lower()}%"
            params.extend([q_like, q_like])

        if gender in ("Male", "Female"):
            filters.append("gender = ?")
            params.append(gender)

        where_clause = " AND ".join(filters) if filters else "1=1"

        cur = g.db.execute(
            f"""
            SELECT id, character_name, anime_name, gender, image, display_order
            FROM characters
            WHERE {where_clause}
            ORDER BY display_order ASC, id ASC
            """,
            params,
        )
        rows = cur.fetchall()

        return render_template(
            "index.html",
            logged_in=logged_in,
            records=rows,
            q=q,
            gender_filter=gender if gender else "All",
        )

    @app.route("/login")
    def login():
        return render_template("login.html")

    @app.route("/login/google")
    def google_login():
        if not os.environ.get("GOOGLE_CLIENT_ID") or not os.environ.get("GOOGLE_CLIENT_SECRET"):
            flash("Google OAuth is not configured on the server.", "error")
            return redirect(url_for("login"))

        redirect_uri = url_for("google_callback", _external=True)
        return oauth.google.authorize_redirect(redirect_uri)

    @app.route("/callback/google")
    def google_callback():
        token = oauth.google.authorize_access_token()
        _ = token  # unused but kept for clarity
        userinfo = oauth.google.get("https://openidconnect.googleapis.com/v1/userinfo")

        email = (userinfo.json().get("email") or "").strip().lower()
        sub = str(userinfo.json().get("sub") or "")

        if not email or not sub:
            flash("Failed to authenticate with Google.", "error")
            return redirect(url_for("login"))

        # Create user on first Google login (recommended)
        existing = g.db.execute(
            "SELECT id FROM users WHERE email = ? OR google_sub = ?",
            (email, sub),
        ).fetchone()

        if existing:
            user_id = existing["id"]
            g.db.execute(
                "UPDATE users SET email = ?, google_sub = ? WHERE id = ?",
                (email, sub, user_id),
            )
            g.db.commit()
        else:
            cur = g.db.execute(
                "INSERT INTO users (email, google_sub) VALUES (?, ?)",
                (email, sub),
            )
            user_id = cur.lastrowid
            g.db.commit()

        session.permanent = True
        session["user_id"] = user_id
        return redirect(url_for("index"))

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    def renumber_for_user(user_id: int):
        rows = g.db.execute(
            "SELECT id FROM characters WHERE user_id = ? ORDER BY display_order ASC, id ASC",
            (user_id,),
        ).fetchall()
        for idx, r in enumerate(rows, start=1):
            g.db.execute(
                "UPDATE characters SET display_order = ? WHERE id = ?",
                (idx, r["id"]),
            )
        g.db.commit()

    def get_record(user_id: int, record_id: int):
        return g.db.execute(
            """
            SELECT id, character_name, anime_name, gender, image, display_order
            FROM characters
            WHERE user_id = ? AND id = ?
            """,
            (user_id, record_id),
        ).fetchone()

    def safe_delete_image(filename: str | None):
        if not filename:
            return
        path = os.path.join(UPLOAD_DIR, filename)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    @app.route("/add", methods=["POST"])
    @login_required
    def add_character():
        user_id = session["user_id"]

        character_name = (request.form.get("character_name") or "").strip()
        anime_name = (request.form.get("anime_name") or "").strip()
        gender = (request.form.get("gender") or "").strip()

        if not character_name:
            flash("Character name is required.", "error")
            return redirect(url_for("index"))
        if not anime_name:
            flash("Anime name is required.", "error")
            return redirect(url_for("index"))
        if gender not in ALLOWED_GENDERS:
            flash("Gender is required.", "error")
            return redirect(url_for("index"))

        # limit
        count = g.db.execute(
            "SELECT COUNT(*) AS c FROM characters WHERE user_id = ?",
            (user_id,),
        ).fetchone()["c"]
        if count >= MAX_RECORDS:
            flash(f"Record limit reached (max {MAX_RECORDS}).", "error")
            return redirect(url_for("index"))

        # image required
        file = request.files.get("image")
        if not file or not file.filename:
            flash("Image is required.", "error")
            return redirect(url_for("index"))

        filename = secure_filename(file.filename)
        if not filename:
            flash("Invalid image filename.", "error")
            return redirect(url_for("index"))

        # Enforce unique character_name per user
        dup = g.db.execute(
            "SELECT id FROM characters WHERE user_id = ? AND character_name = ?",
            (user_id, character_name),
        ).fetchone()
        if dup:
            flash("Character name already exists.", "error")
            return redirect(url_for("index"))

        max_order = g.db.execute(
            "SELECT COALESCE(MAX(display_order), 0) AS m FROM characters WHERE user_id = ?",
            (user_id,),
        ).fetchone()["m"]
        display_order = int(max_order) + 1

        image_db_name = None
        try:
            cur = g.db.execute(
                """
                INSERT INTO characters (character_name, anime_name, gender, image, display_order, user_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (character_name, anime_name, gender, None, display_order, user_id),
            )
            char_id = cur.lastrowid

            image_ext = os.path.splitext(filename)[1].lower() or ".png"
            image_ext = image_ext if image_ext.startswith(".") else f".{image_ext}"
            image_db_name = f"user{user_id}_char{char_id}{image_ext}"

            file.save(os.path.join(UPLOAD_DIR, image_db_name))

            g.db.execute(
                "UPDATE characters SET image = ? WHERE id = ?",
                (image_db_name, char_id),
            )
            g.db.commit()
            flash("Character added successfully.", "success")
        except Exception:
            g.db.rollback()
            if image_db_name:
                safe_delete_image(image_db_name)
            flash("Failed to add character.", "error")

        return redirect(url_for("index"))

    @app.route("/delete/<int:record_id>", methods=["POST"])
    @login_required
    def delete_character(record_id: int):
        user_id = session["user_id"]
        row = get_record(user_id, record_id)
        if not row:
            flash("Record not found.", "error")
            return redirect(url_for("index"))

        image = row["image"]
        g.db.execute(
            "DELETE FROM characters WHERE user_id = ? AND id = ?",
            (user_id, record_id),
        )
        g.db.commit()

        safe_delete_image(image)
        renumber_for_user(user_id)
        flash("Character deleted.", "success")
        return redirect(url_for("index"))

    @app.route("/move/<action>/<int:record_id>", methods=["POST"])
    @login_required
    def move_character(action: str, record_id: int):
        user_id = session["user_id"]
        row = get_record(user_id, record_id)
        if not row:
            flash("Record not found.", "error")
            return redirect(url_for("index"))

        current_order = row["display_order"]

        if action == "up":
            other = g.db.execute(
                """
                SELECT id, display_order
                FROM characters
                WHERE user_id = ? AND display_order < ?
                ORDER BY display_order DESC
                LIMIT 1
                """,
                (user_id, current_order),
            ).fetchone()
        else:
            other = g.db.execute(
                """
                SELECT id, display_order
                FROM characters
                WHERE user_id = ? AND display_order > ?
                ORDER BY display_order ASC
                LIMIT 1
                """,
                (user_id, current_order),
            ).fetchone()

        if not other:
            flash("No more moves available.", "error")
            return redirect(url_for("index"))

        # swap display_order values
        g.db.execute(
            "UPDATE characters SET display_order = ? WHERE id = ? AND user_id = ?",
            (other["display_order"], record_id, user_id),
        )
        g.db.execute(
            "UPDATE characters SET display_order = ? WHERE id = ? AND user_id = ?",
            (current_order, other["id"], user_id),
        )
        g.db.commit()
        return redirect(url_for("index"))

    @app.route("/update/<int:record_id>", methods=["GET", "POST"])
    @login_required
    def update_character(record_id: int):
        user_id = session["user_id"]
        row = get_record(user_id, record_id)
        if not row:
            flash("Record not found.", "error")
            return redirect(url_for("index"))

        if request.method == "POST":
            new_name = (request.form.get("character_name") or "").strip()
            new_anime = (request.form.get("anime_name") or "").strip()
            new_gender = (request.form.get("gender") or "").strip()

            if not new_name:
                flash("Character name is required.", "error")
                return redirect(url_for("update_character", record_id=record_id))
            if not new_anime:
                flash("Anime name is required.", "error")
                return redirect(url_for("update_character", record_id=record_id))
            if new_gender not in ALLOWED_GENDERS:
                flash("Gender is required.", "error")
                return redirect(url_for("update_character", record_id=record_id))

            dup = g.db.execute(
                """
                SELECT id FROM characters
                WHERE user_id = ? AND character_name = ? AND id != ?
                """,
                (user_id, new_name, record_id),
            ).fetchone()
            if dup:
                flash("Character name already exists.", "error")
                return redirect(url_for("update_character", record_id=record_id))

            file = request.files.get("image")
            new_image_db_name = None
            old_image = row["image"]

            if file and file.filename:
                filename = secure_filename(file.filename)
                image_ext = os.path.splitext(filename)[1].lower() or ".png"
                image_ext = image_ext if image_ext.startswith(".") else f".{image_ext}"
                new_image_db_name = f"user{user_id}_char{record_id}{image_ext}"
                file.save(os.path.join(UPLOAD_DIR, new_image_db_name))

            g.db.execute(
                """
                UPDATE characters
                SET character_name = ?, anime_name = ?, gender = ?, image = COALESCE(?, image)
                WHERE user_id = ? AND id = ?
                """,
                (new_name, new_anime, new_gender, new_image_db_name, user_id, record_id),
            )
            g.db.commit()

            if new_image_db_name:
                safe_delete_image(old_image)

            flash("Character updated.", "success")
            return redirect(url_for("index"))

        return render_template("update.html", record=row)

    return app


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row

        # Updated users table for Google auth
        # (We keep legacy password_hash column if it already exists.)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT,
                google_sub TEXT UNIQUE
            )
            """
        )


        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS characters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                character_name TEXT UNIQUE NOT NULL,
                anime_name TEXT NOT NULL,
                gender TEXT NOT NULL,
                image TEXT,
                display_order INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )

        # Ensure columns exist even if DB was created earlier with a password_hash schema.
        # (SQLite doesn't support ALTER TABLE ADD UNIQUE constraints cleanly, but adding columns is fine.)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "google_sub" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN google_sub TEXT")

        # If the old users table had password_hash, ignore it. Don't create default users.
        conn.commit()


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)

