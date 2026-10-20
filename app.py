import os
from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
from pathlib import Path
from functools import wraps

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("HOUSE_OF_JACOB_DB_PATH", str(BASE_DIR / "house_of_jacob.db")))

app = Flask(__name__)
app.secret_key = os.environ.get("HOUSE_OF_JACOB_SECRET") or ("local-learning-secret-change-before-deploy" if not os.environ.get("RENDER") else None)
if not app.secret_key:
    raise RuntimeError("Set HOUSE_OF_JACOB_SECRET in the hosting environment before deploying.")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")))


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                display_name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                bio TEXT NOT NULL DEFAULT '',
                interests TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                content TEXT NOT NULL CHECK(length(content) <= 2000),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS direct_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                recipient_id INTEGER NOT NULL,
                body TEXT NOT NULL CHECK(length(body) <= 4000),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (recipient_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_dm_pair ON direct_messages(sender_id, recipient_id, created_at)")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please sign in first.")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/")
def index():
    user = None
    if "user_id" in session:
        with get_db() as db:
            user = db.execute(
                "SELECT id, display_name, email, bio, interests FROM users WHERE id = ?",
                (session["user_id"],)
            ).fetchone()
    return render_template("index.html", user=user)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("display_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not name or not email or len(password) < 8:
            flash("Enter a display name, a valid email, and a password of at least 8 characters.")
            return render_template("register.html")
        try:
            with get_db() as db:
                db.execute(
                    "INSERT INTO users (display_name, email, password_hash) VALUES (?, ?, ?)",
                    (name, email, generate_password_hash(password))
                )
            flash("Account created. You can now sign in.")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("That email is already registered. Try signing in.")
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        with get_db() as db:
            user = db.execute(
                "SELECT id, password_hash FROM users WHERE email = ?", (email,)
            ).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            flash("You are signed in.")
            return redirect(url_for("dashboard"))
        flash("Email or password was not correct.")
    return render_template("login.html")


@app.route("/dashboard")
@login_required
def dashboard():
    with get_db() as db:
        user = db.execute(
            "SELECT id, display_name, email, bio, interests, created_at FROM users WHERE id = ?",
            (session["user_id"],)
        ).fetchone()
        posts = db.execute(
            """
            SELECT posts.id, posts.content, posts.created_at, users.display_name
            FROM posts
            JOIN users ON users.id = posts.user_id
            ORDER BY posts.created_at DESC, posts.id DESC
            """
        ).fetchall()

    if user is None:
        session.clear()
        flash("Please sign in again.")
        return redirect(url_for("login"))
    return render_template("dashboard.html", user=user, posts=posts)


@app.route("/create-post", methods=["POST"])
@login_required
def create_post():
    content = request.form.get("content", "").strip()
    if not content:
        flash("Your post cannot be empty.")
        return redirect(url_for("dashboard"))
    if len(content) > 2000:
        flash("Your post must be 2000 characters or fewer.")
        return redirect(url_for("dashboard"))
    with get_db() as db:
        db.execute(
            "INSERT INTO posts (user_id, content) VALUES (?, ?)",
            (session["user_id"], content),
        )
    flash("Your post was published.")
    return redirect(url_for("dashboard"))


@app.route("/profile", methods=["POST"])
@login_required
def update_profile():
    display_name = request.form.get("display_name", "").strip()
    bio = request.form.get("bio", "").strip()[:240]
    interests = request.form.get("interests", "").strip()[:240]
    if not display_name:
        flash("Display name cannot be empty.")
        return redirect(url_for("dashboard"))
    with get_db() as db:
        db.execute(
            "UPDATE users SET display_name = ?, bio = ?, interests = ? WHERE id = ?",
            (display_name, bio, interests, session["user_id"])
        )
    flash("Your profile has been updated.")
    return redirect(url_for("dashboard"))




@app.route("/messages")
@login_required
def messages():
    with get_db() as db:
        people = db.execute(
            """
            SELECT u.id, u.display_name, u.bio,
                   (SELECT body FROM direct_messages
                    WHERE (sender_id = u.id AND recipient_id = ?)
                       OR (sender_id = ? AND recipient_id = u.id)
                    ORDER BY created_at DESC, id DESC LIMIT 1) AS last_message,
                   (SELECT created_at FROM direct_messages
                    WHERE (sender_id = u.id AND recipient_id = ?)
                       OR (sender_id = ? AND recipient_id = u.id)
                    ORDER BY created_at DESC, id DESC LIMIT 1) AS last_message_at
            FROM users u WHERE u.id != ?
            ORDER BY last_message_at DESC, u.display_name COLLATE NOCASE
            """,
            (session["user_id"], session["user_id"], session["user_id"], session["user_id"], session["user_id"])
        ).fetchall()
        selected_id = request.args.get("to", type=int)
        selected = None
        conversation = []
        if selected_id:
            selected = db.execute(
                "SELECT id, display_name, bio FROM users WHERE id = ? AND id != ?",
                (selected_id, session["user_id"])
            ).fetchone()
            if selected:
                conversation = db.execute(
                    """
                    SELECT dm.id, dm.sender_id, dm.recipient_id, dm.body, dm.created_at,
                           sender.display_name AS sender_name
                    FROM direct_messages dm
                    JOIN users sender ON sender.id = dm.sender_id
                    WHERE (dm.sender_id = ? AND dm.recipient_id = ?)
                       OR (dm.sender_id = ? AND dm.recipient_id = ?)
                    ORDER BY dm.created_at ASC, dm.id ASC
                    LIMIT 300
                    """,
                    (session["user_id"], selected_id, selected_id, session["user_id"])
                ).fetchall()
    return render_template("messages.html", people=people, selected=selected, conversation=conversation)


@app.route("/messages/send", methods=["POST"])
@login_required
def send_message():
    recipient_id = request.form.get("recipient_id", type=int)
    body = request.form.get("body", "").strip()
    if not recipient_id or not body:
        flash("Choose a person and enter a message.")
        return redirect(url_for("messages"))
    if len(body) > 4000:
        flash("Messages must be 4000 characters or fewer.")
        return redirect(url_for("messages", to=recipient_id))
    with get_db() as db:
        recipient = db.execute("SELECT id FROM users WHERE id = ?", (recipient_id,)).fetchone()
        if not recipient or recipient_id == session["user_id"]:
            flash("That member is not available.")
            return redirect(url_for("messages"))
        db.execute(
            "INSERT INTO direct_messages (sender_id, recipient_id, body) VALUES (?, ?, ?)",
            (session["user_id"], recipient_id, body)
        )
    return redirect(url_for("messages", to=recipient_id))


@app.route("/calls")
@login_required
def calls():
    # Jitsi creates a browser-based meeting room. Share its link with the member
    # you want to call; this does not provide native ringing or incoming-call alerts.
    import secrets
    room = "HouseOfJacob-" + secrets.token_urlsafe(18)
    return render_template("calls.html", room=room, call_url="https://meet.jit.si/" + room)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have signed out.")
    return redirect(url_for("index"))


init_db()

if __name__ == "__main__":
    app.run(debug=False)
