from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
from pathlib import Path
from functools import wraps

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("HOUSE_OF_JACOB_DB_PATH", str(BASE_DIR / "house_of_jacob.db")))

app = Flask(__name__)
# For local learning only. Before deployment, set a long random secret in an environment variable.
import os
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
    if user is None:
        session.clear()
        flash("Please sign in again.")
        return redirect(url_for("login"))
    return render_template("dashboard.html", user=user)


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


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have signed out.")
    return redirect(url_for("index"))


init_db()

if __name__ == "__main__":
    # Local development only. Production uses Gunicorn via the Procfile.
    app.run(debug=False)
