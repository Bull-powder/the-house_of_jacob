import os
from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import uuid
import mimetypes

try:
    from supabase import create_client
except ImportError:
    create_client = None

try:
    import cloudinary
    import cloudinary.uploader
except ImportError:
    cloudinary = None

# PostgreSQL is opt-in so adding DATABASE_URL to Render cannot silently switch
# the live site to an empty database. Keep SQLite as the default until data is
# deliberately migrated and HOUSE_OF_JACOB_STORAGE=postgres is set.
STORAGE_BACKEND = os.environ.get("HOUSE_OF_JACOB_STORAGE", "sqlite").strip().lower()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if STORAGE_BACKEND not in {"sqlite", "postgres"}:
    raise RuntimeError("HOUSE_OF_JACOB_STORAGE must be either 'sqlite' or 'postgres'.")
if STORAGE_BACKEND == "postgres":
    if not DATABASE_URL:
        raise RuntimeError("Set DATABASE_URL before enabling PostgreSQL storage.")
    import psycopg
    from psycopg.rows import dict_row
from werkzeug.utils import secure_filename
from pathlib import Path
from functools import wraps

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("HOUSE_OF_JACOB_DB_PATH", str(BASE_DIR / "house_of_jacob.db")))

app = Flask(__name__)
app.secret_key = os.environ.get("HOUSE_OF_JACOB_SECRET") or ("local-learning-secret-change-before-deploy" if not os.environ.get("RENDER") else None)
if not app.secret_key:
    raise RuntimeError("Set HOUSE_OF_JACOB_SECRET in the hosting environment before deploying.")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")), MAX_CONTENT_LENGTH=50 * 1024 * 1024)
UPLOAD_DIR = Path(os.environ.get("HOUSE_OF_JACOB_UPLOAD_DIR", str(BASE_DIR / "static" / "uploads")))
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
SUPABASE_MEDIA_BUCKET = os.environ.get("SUPABASE_MEDIA_BUCKET", "community-media").strip()
CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "").strip()
CLOUDINARY_API_KEY = os.environ.get("CLOUDINARY_API_KEY", "").strip()
CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "").strip()
_cloudinary_enabled = bool(CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET)
_supabase = None

if _cloudinary_enabled:
    if cloudinary is None:
        raise RuntimeError("Install the cloudinary package to enable Cloudinary media storage.")
    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY,
        api_secret=CLOUDINARY_API_SECRET,
        secure=True,
    )
elif STORAGE_BACKEND == "postgres" and not (SUPABASE_URL and SUPABASE_SECRET_KEY):
    raise RuntimeError("PostgreSQL mode requires Cloudinary credentials or SUPABASE_URL and SUPABASE_SECRET_KEY for persistent media.")

if not _cloudinary_enabled and SUPABASE_URL and SUPABASE_SECRET_KEY:
    if create_client is None:
        raise RuntimeError("Install the supabase package to enable persistent media storage.")
    _supabase = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
ALLOWED_VIDEO_EXTENSIONS = {"mp4", "webm", "mov"}


def save_upload(upload, folder, filename, content_type):
    """Store media in Cloudinary first, then Supabase, or locally for development."""
    if _cloudinary_enabled:
        resource_type = "video" if content_type.startswith(("video/", "audio/")) else "image"
        result = cloudinary.uploader.upload(
            upload.stream,
            resource_type=resource_type,
            folder=folder,
            public_id=Path(filename).stem,
            unique_filename=True,
            overwrite=False,
        )
        url = result.get("secure_url") or result.get("url")
        if not url:
            raise RuntimeError("Cloudinary upload returned no media URL.")
        return url
    if _supabase:
        object_path = f"{folder}/{filename}"
        _supabase.storage.from_(SUPABASE_MEDIA_BUCKET).upload(
            object_path,
            upload.read(),
            file_options={"content-type": content_type, "upsert": "false"},
        )
        return _supabase.storage.from_(SUPABASE_MEDIA_BUCKET).get_public_url(object_path)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    upload.save(UPLOAD_DIR / filename)
    return filename


@app.template_global()
def media_url(value):
    """Render stored public media URLs or legacy/local upload filenames."""
    if not value:
        return ""
    if value.startswith(("https://", "http://")):
        return value
    return url_for("static", filename="uploads/" + value)


class PostgresDB:
    """Small compatibility layer for the existing parameterized SQL queries."""
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type:
                self.connection.rollback()
            else:
                self.connection.commit()
        finally:
            self.connection.close()
        return False

    def execute(self, query, params=()):
        # The app uses qmark placeholders for SQLite. Its current queries do
        # not contain literal question marks, so this conversion is safe here.
        query = query.replace("?", "%s")
        query = query.replace("COLLATE NOCASE", "")
        return self.connection.execute(query, params)


def get_db():
    if STORAGE_BACKEND == "postgres":
        return PostgresDB(psycopg.connect(DATABASE_URL, row_factory=dict_row))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    if STORAGE_BACKEND == "postgres":
        # This creates a schema only; it does NOT migrate existing SQLite data.
        # Keep this backend disabled until a verified data migration is complete.
        schema = [
            """CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY, display_name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
                bio TEXT NOT NULL DEFAULT '', interests TEXT NOT NULL DEFAULT '',
                profile_image TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
            """CREATE TABLE IF NOT EXISTS posts (
                id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL,
                content TEXT NOT NULL DEFAULT '' CHECK(length(content) <= 2000),
                media_path TEXT NOT NULL DEFAULT '', media_type TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE)""",
            """CREATE TABLE IF NOT EXISTS direct_messages (
                id BIGSERIAL PRIMARY KEY, sender_id BIGINT NOT NULL,
                recipient_id BIGINT NOT NULL,
                body TEXT NOT NULL CHECK(length(body) <= 4000),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (recipient_id) REFERENCES users(id) ON DELETE CASCADE)""",
            """CREATE TABLE IF NOT EXISTS comments (
                id BIGSERIAL PRIMARY KEY, post_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                content TEXT NOT NULL CHECK(length(content) <= 1000),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE)""",
            """CREATE TABLE IF NOT EXISTS reactions (
                id BIGSERIAL PRIMARY KEY, post_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL, reaction TEXT NOT NULL DEFAULT 'like',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(post_id, user_id),
                FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE)""",
            "CREATE INDEX IF NOT EXISTS idx_dm_pair ON direct_messages(sender_id, recipient_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_reactions_post ON reactions(post_id)"
        ]
        with get_db() as db:
            for statement in schema:
                db.execute(statement)
            # Add optional media fields safely to existing conversations.
            db.execute("ALTER TABLE direct_messages ADD COLUMN IF NOT EXISTS media_path TEXT NOT NULL DEFAULT ''")
            db.execute("ALTER TABLE direct_messages ADD COLUMN IF NOT EXISTS media_type TEXT NOT NULL DEFAULT ''")
            db.execute("ALTER TABLE direct_messages ADD COLUMN IF NOT EXISTS reply_to_id BIGINT REFERENCES direct_messages(id) ON DELETE SET NULL")
            db.execute("""CREATE TABLE IF NOT EXISTS message_reactions (
                message_id BIGINT NOT NULL REFERENCES direct_messages(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                reaction TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (message_id, user_id))""")
        return

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
                profile_image TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                content TEXT NOT NULL DEFAULT '' CHECK(length(content) <= 2000),
                media_path TEXT NOT NULL DEFAULT '',
                media_type TEXT NOT NULL DEFAULT '',
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
        dm_columns = {row["name"] for row in db.execute("PRAGMA table_info(direct_messages)")}
        if "media_path" not in dm_columns: db.execute("ALTER TABLE direct_messages ADD COLUMN media_path TEXT NOT NULL DEFAULT ''")
        if "media_type" not in dm_columns: db.execute("ALTER TABLE direct_messages ADD COLUMN media_type TEXT NOT NULL DEFAULT ''")
        if "reply_to_id" not in dm_columns: db.execute("ALTER TABLE direct_messages ADD COLUMN reply_to_id INTEGER REFERENCES direct_messages(id) ON DELETE SET NULL")
        db.execute("""CREATE TABLE IF NOT EXISTS message_reactions (
            message_id INTEGER NOT NULL REFERENCES direct_messages(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            reaction TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (message_id, user_id))""")
        user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
        if "profile_image" not in user_columns: db.execute("ALTER TABLE users ADD COLUMN profile_image TEXT NOT NULL DEFAULT ''")
        post_columns = {row["name"] for row in db.execute("PRAGMA table_info(posts)")}
        if "media_path" not in post_columns: db.execute("ALTER TABLE posts ADD COLUMN media_path TEXT NOT NULL DEFAULT ''")
        if "media_type" not in post_columns: db.execute("ALTER TABLE posts ADD COLUMN media_type TEXT NOT NULL DEFAULT ''")
        db.execute("""CREATE TABLE IF NOT EXISTS comments (id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER NOT NULL, user_id INTEGER NOT NULL, content TEXT NOT NULL CHECK(length(content) <= 1000), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(post_id) REFERENCES posts(id) ON DELETE CASCADE, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)""")
        db.execute("""CREATE TABLE IF NOT EXISTS reactions (id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER NOT NULL, user_id INTEGER NOT NULL, reaction TEXT NOT NULL DEFAULT 'like', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(post_id,user_id), FOREIGN KEY(post_id) REFERENCES posts(id) ON DELETE CASCADE, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_dm_pair ON direct_messages(sender_id, recipient_id, created_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id, created_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_reactions_post ON reactions(post_id)")


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
        except Exception as exc:
            is_duplicate = isinstance(exc, sqlite3.IntegrityError)
            if STORAGE_BACKEND == "postgres":
                is_duplicate = is_duplicate or isinstance(exc, psycopg.errors.UniqueViolation)
            if is_duplicate:
                flash("That email is already registered. Try signing in.")
            else:
                app.logger.exception("Account registration failed")
                flash("We could not create your account right now. Please try again.")
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
    query = request.args.get("q", "").strip()[:100]
    with get_db() as db:
        user = db.execute("SELECT id, display_name, email, bio, interests, profile_image, created_at FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        following_ids = {row["followed_id"] for row in db.execute("SELECT followed_id FROM follows WHERE follower_id=?", (session["user_id"],)).fetchall()}
        params = [session["user_id"]]
        where = ""
        if query:
            where = "WHERE posts.content LIKE ? OR users.display_name LIKE ?"
            params.extend(["%" + query + "%", "%" + query + "%"])
        rows = db.execute("""
            SELECT posts.id, posts.user_id, posts.content, posts.media_path, posts.media_type, posts.music_path, posts.music_title, posts.created_at, users.display_name, users.profile_image,
            (SELECT COUNT(*) FROM comments c WHERE c.post_id=posts.id) AS comment_count,
            (SELECT COUNT(*) FROM reactions r WHERE r.post_id=posts.id) AS reaction_count,
            (SELECT reaction FROM reactions r WHERE r.post_id=posts.id AND r.user_id=?) AS my_reaction
            FROM posts JOIN users ON users.id=posts.user_id """ + where + """
            ORDER BY posts.created_at DESC, posts.id DESC LIMIT 100
        """, params).fetchall()
        people = []
        if query:
            like = "%" + query + "%"
            people = db.execute("SELECT id,display_name,bio,profile_image FROM users WHERE id != ? AND (display_name LIKE ? OR bio LIKE ? OR interests LIKE ?) ORDER BY display_name COLLATE NOCASE LIMIT 20", (session["user_id"],like,like,like)).fetchall()
        posts = []
        for row in rows:
            item = dict(row)
            item["comments"] = [dict(c) for c in db.execute("SELECT comments.content,comments.created_at,users.display_name,users.profile_image FROM comments JOIN users ON users.id=comments.user_id WHERE comments.post_id=? ORDER BY comments.created_at,comments.id LIMIT 50", (item["id"],)).fetchall()]
            posts.append(item)
    if user is None:
        session.clear(); flash("Please sign in again."); return redirect(url_for("login"))
    return render_template("dashboard.html", user=user, posts=posts, query=query, people=people, following_ids=following_ids)


@app.route("/create-post", methods=["POST"])
@login_required
def create_post():
    content = request.form.get("content", "").strip()
    upload = request.files.get("media")
    music_upload = request.files.get("music_file")
    music_url = request.form.get("music_url", "").strip()
    music_title = request.form.get("music_title", "").strip()[:120]
    media_path, media_type, music_path = "", "", ""
    if not content and (not upload or not upload.filename) and (not music_upload or not music_upload.filename) and not music_url:
        flash("Add a caption, photo/video, or music to your post."); return redirect(url_for("dashboard"))
    if len(content) > 2000:
        flash("Your post must be 2000 characters or fewer."); return redirect(url_for("dashboard"))
    if music_url:
        from urllib.parse import urlparse
        parsed_music = urlparse(music_url)
        if parsed_music.scheme != "https" or parsed_music.hostname not in {"audio-ssl.itunes.apple.com", "audio.itunes.apple.com"}:
            flash("Choose a valid online music preview from the House of Jacob music search."); return redirect(url_for("dashboard"))
        music_path = music_url
        music_title = music_title or "Online song preview"
    elif music_upload and music_upload.filename:
        safe_music = secure_filename(music_upload.filename)
        music_ext = safe_music.rsplit(".", 1)[-1].lower() if "." in safe_music else ""
        if music_ext not in {"mp3", "m4a", "wav", "ogg", "aac", "webm"}:
            flash("Music must be MP3, M4A, WAV, OGG, AAC, or WEBM."); return redirect(url_for("dashboard"))
        try:
            music_path = save_upload(music_upload, "music", uuid.uuid4().hex + "." + music_ext,
                                     mimetypes.guess_type(safe_music)[0] or "audio/mpeg")
            music_title = music_title or Path(safe_music).stem
        except Exception:
            app.logger.exception("Post music upload failed")
            flash("Music could not be uploaded. Please try a smaller audio file."); return redirect(url_for("dashboard"))
    if upload and upload.filename:
        safe = secure_filename(upload.filename)
        ext = safe.rsplit(".", 1)[-1].lower() if "." in safe else ""
        if ext in ALLOWED_IMAGE_EXTENSIONS: media_type = "image"
        elif ext in ALLOWED_VIDEO_EXTENSIONS: media_type = "video"
        else:
            flash("Use PNG, JPG, GIF, WEBP images or MP4, WEBM, MOV videos."); return redirect(url_for("dashboard"))
        media_name = uuid.uuid4().hex + "." + ext
        content_type = mimetypes.guess_type(safe)[0] or ("video/mp4" if media_type == "video" else "application/octet-stream")
        try:
            media_path = save_upload(upload, "posts", media_name, content_type)
        except Exception:
            app.logger.exception("Post media upload failed")
            flash("The media upload failed. Please try again.")
            return redirect(url_for("dashboard"))
    with get_db() as db:
        db.execute("INSERT INTO posts (user_id,content,media_path,media_type,music_path,music_title) VALUES (?,?,?,?,?,?)",
                   (session["user_id"],content,media_path,media_type,music_path,music_title))
    flash("Your post was published with its media and soundtrack."); return redirect(url_for("dashboard"))


@app.route("/profile", methods=["POST"])
@login_required
def update_profile():
    display_name = request.form.get("display_name", "").strip()
    bio = request.form.get("bio", "").strip()[:240]
    interests = request.form.get("interests", "").strip()[:240]
    upload = request.files.get("profile_image")
    profile_image = None
    if upload and upload.filename:
        safe = secure_filename(upload.filename)
        ext = safe.rsplit(".",1)[-1].lower() if "." in safe else ""
        if ext not in ALLOWED_IMAGE_EXTENSIONS:
            flash("Profile photos must be PNG, JPG, GIF, or WEBP."); return redirect(url_for("dashboard"))
        profile_image = uuid.uuid4().hex + "." + ext
        content_type = mimetypes.guess_type(safe)[0] or "application/octet-stream"
        try:
            profile_image = save_upload(upload, "profiles", profile_image, content_type)
        except Exception:
            app.logger.exception("Profile image upload failed")
            flash("The profile photo upload failed. Please try again.")
            return redirect(url_for("dashboard"))
    if not display_name:
        flash("Display name cannot be empty.")
        return redirect(url_for("dashboard"))
    with get_db() as db:
        if profile_image:
            db.execute("UPDATE users SET display_name=?,bio=?,interests=?,profile_image=? WHERE id=?", (display_name,bio,interests,profile_image,session["user_id"]))
        else:
            db.execute("UPDATE users SET display_name=?,bio=?,interests=? WHERE id=?", (display_name,bio,interests,session["user_id"]))
    flash("Your profile has been updated.")
    return redirect(url_for("dashboard"))




@app.route("/posts/<int:post_id>/delete", methods=["POST"])
@login_required
def delete_post(post_id):
    """Allow members to delete only their own posts."""
    with get_db() as db:
        post = db.execute(
            "SELECT id FROM posts WHERE id=? AND user_id=?",
            (post_id, session["user_id"])
        ).fetchone()
        if not post:
            flash("Post not found, or you do not have permission to delete it.")
            return redirect(url_for("dashboard"))
        # Explicitly remove dependent records for compatibility with existing DBs.
        db.execute("DELETE FROM comments WHERE post_id=?", (post_id,))
        db.execute("DELETE FROM reactions WHERE post_id=?", (post_id,))
        db.execute("DELETE FROM posts WHERE id=? AND user_id=?", (post_id, session["user_id"]))
    flash("Your post was deleted.")
    return redirect(url_for("dashboard"))


@app.route("/posts/<int:post_id>/comment", methods=["POST"])
@login_required
def add_comment(post_id):
    content = request.form.get("content", "").strip()
    if not content or len(content)>1000: flash("Replies must contain 1–1000 characters."); return redirect(url_for("dashboard"))
    with get_db() as db:
        if not db.execute("SELECT id FROM posts WHERE id=?", (post_id,)).fetchone(): flash("Post not found."); return redirect(url_for("dashboard"))
        db.execute("INSERT INTO comments(post_id,user_id,content) VALUES(?,?,?)", (post_id,session["user_id"],content))
    return redirect(url_for("dashboard")+"#post-"+str(post_id))


@app.route("/posts/<int:post_id>/react", methods=["POST"])
@login_required
def react_to_post(post_id):
    reaction=request.form.get("reaction","like")
    if reaction not in {"like","love","support","laugh"}: reaction="like"
    with get_db() as db:
        if not db.execute("SELECT id FROM posts WHERE id=?", (post_id,)).fetchone(): flash("Post not found."); return redirect(url_for("dashboard"))
        current=db.execute("SELECT reaction FROM reactions WHERE post_id=? AND user_id=?", (post_id,session["user_id"])).fetchone()
        if current and current["reaction"]==reaction:
            db.execute("DELETE FROM reactions WHERE post_id=? AND user_id=?", (post_id,session["user_id"]))
        else:
            db.execute("INSERT INTO reactions(post_id,user_id,reaction) VALUES(?,?,?) ON CONFLICT(post_id,user_id) DO UPDATE SET reaction=excluded.reaction", (post_id,session["user_id"],reaction))
    return redirect(url_for("dashboard")+"#post-"+str(post_id))


@app.route("/messages")
@login_required
def messages():
    with get_db() as db:
        people = db.execute(
            """
            SELECT u.id, u.display_name, u.bio, u.profile_image,
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
                "SELECT id, display_name, bio, profile_image FROM users WHERE id = ? AND id != ?",
                (selected_id, session["user_id"])
            ).fetchone()
            if selected:
                conversation = db.execute(
                    """
                    SELECT dm.id, dm.sender_id, dm.recipient_id, dm.body, dm.media_path, dm.media_type, dm.reply_to_id, dm.created_at,
                           sender.display_name AS sender_name, parent.body AS reply_to_body,
                           (SELECT reaction FROM message_reactions mr WHERE mr.message_id=dm.id AND mr.user_id=?) AS my_reaction,
                           (SELECT COUNT(*) FROM message_reactions mr WHERE mr.message_id=dm.id AND mr.reaction='❤️') AS heart_count,
                           (SELECT COUNT(*) FROM message_reactions mr WHERE mr.message_id=dm.id AND mr.reaction='😂') AS laugh_count,
                           (SELECT COUNT(*) FROM message_reactions mr WHERE mr.message_id=dm.id AND mr.reaction='👍') AS like_count
                    FROM direct_messages dm
                    JOIN users sender ON sender.id = dm.sender_id
                    LEFT JOIN direct_messages parent ON parent.id=dm.reply_to_id
                    WHERE (dm.sender_id = ? AND dm.recipient_id = ?)
                       OR (dm.sender_id = ? AND dm.recipient_id = ?)
                    ORDER BY dm.created_at ASC, dm.id ASC
                    LIMIT 300
                    """,
                    (session["user_id"], session["user_id"], selected_id, selected_id, session["user_id"])
                ).fetchall()
    return render_template("messages.html", people=people, selected=selected, conversation=conversation)


@app.route("/messages/send", methods=["POST"])
@login_required
def send_message():
    recipient_id = request.form.get("recipient_id", type=int)
    body = request.form.get("body", "").strip()
    upload = request.files.get("message_media")
    reply_to_id = request.form.get("reply_to_id", type=int)
    if not recipient_id or (not body and (not upload or not upload.filename)):
        flash("Write a message or attach a photo/video.")
        return redirect(url_for("messages", to=recipient_id) if recipient_id else url_for("messages"))
    if len(body) > 4000:
        flash("Messages must be 4000 characters or fewer.")
        return redirect(url_for("messages", to=recipient_id))
    media_path, media_type = "", ""
    if upload and upload.filename:
        safe = secure_filename(upload.filename)
        ext = safe.rsplit(".", 1)[-1].lower() if "." in safe else ""
        if ext not in ALLOWED_IMAGE_EXTENSIONS | ALLOWED_VIDEO_EXTENSIONS:
            flash("Messages support PNG, JPG, GIF, WEBP, MP4, WEBM, or MOV files.")
            return redirect(url_for("messages", to=recipient_id))
        media_type = "video" if ext in ALLOWED_VIDEO_EXTENSIONS else "image"
        try:
            stored_name = uuid.uuid4().hex + "." + ext
            content_type = mimetypes.guess_type(safe)[0] or ("video/mp4" if media_type == "video" else "image/jpeg")
            media_path = save_upload(upload, "messages", stored_name, content_type)
        except Exception:
            app.logger.exception("Direct message media upload failed")
            flash("The attachment could not be uploaded. Please try again.")
            return redirect(url_for("messages", to=recipient_id))
    with get_db() as db:
        recipient = db.execute("SELECT id FROM users WHERE id = ?", (recipient_id,)).fetchone()
        if not recipient or recipient_id == session["user_id"]:
            flash("That member is not available.")
            return redirect(url_for("messages"))
        if reply_to_id:
            parent = db.execute("""SELECT id FROM direct_messages
                WHERE id=? AND ((sender_id=? AND recipient_id=?) OR (sender_id=? AND recipient_id=?))""",
                (reply_to_id, session["user_id"], recipient_id, recipient_id, session["user_id"])).fetchone()
            if not parent:
                flash("That message can no longer be replied to.")
                return redirect(url_for("messages", to=recipient_id))
        db.execute(
            "INSERT INTO direct_messages (sender_id, recipient_id, body, media_path, media_type, reply_to_id) VALUES (?, ?, ?, ?, ?, ?)",
            (session["user_id"], recipient_id, body, media_path, media_type, reply_to_id)
        )
    return redirect(url_for("messages", to=recipient_id))


@app.route("/messages/<int:message_id>/react", methods=["POST"])
@login_required
def react_to_message(message_id):
    recipient_id = request.form.get("recipient_id", type=int)
    reaction = request.form.get("reaction", "")
    if reaction not in {"❤️", "😂", "👍", "🙌"}:
        flash("Choose a supported message reaction.")
        return redirect(url_for("messages", to=recipient_id) if recipient_id else url_for("messages"))
    with get_db() as db:
        message = db.execute("""SELECT id FROM direct_messages
            WHERE id=? AND ((sender_id=? AND recipient_id=?) OR (sender_id=? AND recipient_id=?))""",
            (message_id, session["user_id"], recipient_id, recipient_id, session["user_id"])).fetchone()
        if not recipient_id or not message:
            flash("That message is not available in this conversation.")
            return redirect(url_for("messages"))
        current = db.execute("SELECT reaction FROM message_reactions WHERE message_id=? AND user_id=?",
                             (message_id, session["user_id"])).fetchone()
        if current and current["reaction"] == reaction:
            db.execute("DELETE FROM message_reactions WHERE message_id=? AND user_id=?", (message_id, session["user_id"]))
        else:
            db.execute("""INSERT INTO message_reactions(message_id,user_id,reaction) VALUES(?,?,?)
                ON CONFLICT(message_id,user_id) DO UPDATE SET reaction=excluded.reaction""",
                (message_id, session["user_id"], reaction))
    return redirect(url_for("messages", to=recipient_id) + "#message-" + str(message_id))


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


from social_features import register_social_features
register_social_features(app, get_db, login_required, save_upload, media_url)

init_db()

if __name__ == "__main__":
    app.run(debug=False)
