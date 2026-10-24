from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, abort
from werkzeug.utils import secure_filename
from pathlib import Path
import mimetypes
import uuid
import os
import json
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

social = Blueprint("social", __name__)
_SCHEMA_READY = False
_AUDIO_EXTENSIONS = {"mp3", "m4a", "wav", "ogg", "aac", "webm"}
_STORY_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "mp4", "webm", "mov"}


def register_social_features(app, get_db, login_required, save_upload, media_url):
    """Add stories, post audio, follows, public profiles and profile settings."""
    is_postgres = os.environ.get("HOUSE_OF_JACOB_STORAGE", "sqlite").strip().lower() == "postgres"

    def ensure_social_schema():
        global _SCHEMA_READY
        if _SCHEMA_READY:
            return
        with get_db() as db:
            if is_postgres:
                db.execute("""ALTER TABLE posts ADD COLUMN IF NOT EXISTS music_path TEXT NOT NULL DEFAULT ''""")
                db.execute("""ALTER TABLE posts ADD COLUMN IF NOT EXISTS music_title TEXT NOT NULL DEFAULT ''""")
                db.execute("""CREATE TABLE IF NOT EXISTS follows (
                    follower_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    followed_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (follower_id, followed_id),
                    CHECK (follower_id <> followed_id))""")
                db.execute("""CREATE TABLE IF NOT EXISTS stories (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    media_path TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    caption TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL)""")
                db.execute("CREATE INDEX IF NOT EXISTS idx_stories_expiry ON stories(expires_at)")
                db.execute("CREATE INDEX IF NOT EXISTS idx_follows_followed ON follows(followed_id)")
                db.execute("""ALTER TABLE stories ADD COLUMN IF NOT EXISTS music_path TEXT NOT NULL DEFAULT ''""")
                db.execute("""ALTER TABLE stories ADD COLUMN IF NOT EXISTS music_title TEXT NOT NULL DEFAULT ''""")
            else:
                columns = {row["name"] for row in db.execute("PRAGMA table_info(posts)").fetchall()}
                if "music_path" not in columns:
                    db.execute("ALTER TABLE posts ADD COLUMN music_path TEXT NOT NULL DEFAULT ''")
                if "music_title" not in columns:
                    db.execute("ALTER TABLE posts ADD COLUMN music_title TEXT NOT NULL DEFAULT ''")
                db.execute("""CREATE TABLE IF NOT EXISTS follows (
                    follower_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    followed_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (follower_id, followed_id),
                    CHECK (follower_id <> followed_id))""")
                db.execute("""CREATE TABLE IF NOT EXISTS stories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    media_path TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    caption TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL)""")
                db.execute("CREATE INDEX IF NOT EXISTS idx_stories_expiry ON stories(expires_at)")
                db.execute("CREATE INDEX IF NOT EXISTS idx_follows_followed ON follows(followed_id)")
                story_columns = {row["name"] for row in db.execute("PRAGMA table_info(stories)").fetchall()}
                if "music_path" not in story_columns:
                    db.execute("ALTER TABLE stories ADD COLUMN music_path TEXT NOT NULL DEFAULT ''")
                if "music_title" not in story_columns:
                    db.execute("ALTER TABLE stories ADD COLUMN music_title TEXT NOT NULL DEFAULT ''")
        _SCHEMA_READY = True

    @app.before_request
    def _ensure_social_tables():
        ensure_social_schema()

    @app.context_processor
    def _social_template_context():
        data = {"stories": [], "follower_count": 0, "following_count": 0}
        uid = session.get("user_id")
        if not uid:
            return data
        try:
            with get_db() as db:
                data["stories"] = [dict(row) for row in db.execute("""
                    SELECT s.id, s.user_id, s.media_path, s.media_type, s.caption, s.music_path, s.music_title, s.created_at,
                           u.display_name, u.profile_image
                    FROM stories s JOIN users u ON u.id=s.user_id
                    WHERE s.expires_at > CURRENT_TIMESTAMP
                    ORDER BY s.created_at DESC, s.id DESC LIMIT 40
                """).fetchall()]
                row = db.execute("SELECT COUNT(*) AS n FROM follows WHERE followed_id=?", (uid,)).fetchone()
                data["follower_count"] = row["n"]
                row = db.execute("SELECT COUNT(*) AS n FROM follows WHERE follower_id=?", (uid,)).fetchone()
                data["following_count"] = row["n"]
        except Exception:
            app.logger.exception("Could not load social feed context")
        return data

    @social.route("/stories/create", methods=["POST"])
    @login_required
    def create_story():
        upload = request.files.get("story_media")
        caption = request.form.get("caption", "").strip()[:180]
        if not upload or not upload.filename:
            flash("Choose a photo or video for your story.")
            return redirect(url_for("dashboard"))
        safe = secure_filename(upload.filename)
        ext = safe.rsplit(".", 1)[-1].lower() if "." in safe else ""
        if ext not in _STORY_EXTENSIONS:
            flash("Stories support PNG, JPG, GIF, WEBP, MP4, WEBM, or MOV.")
            return redirect(url_for("dashboard"))
        media_type = "video" if ext in {"mp4", "webm", "mov"} else "image"
        music_upload = request.files.get("story_music")
        online_preview = request.form.get("story_music_url", "").strip()
        music_title = request.form.get("story_music_title", "").strip()[:120]
        music_path = ""
        if online_preview:
            parsed = urlparse(online_preview)
            if parsed.scheme != "https" or parsed.hostname not in {"audio-ssl.itunes.apple.com", "audio.itunes.apple.com"}:
                flash("Choose a valid online song preview from the music catalog.")
                return redirect(url_for("dashboard") + "#stories")
            music_path = online_preview
        elif music_upload and music_upload.filename:
            music_safe = secure_filename(music_upload.filename)
            music_ext = music_safe.rsplit(".", 1)[-1].lower() if "." in music_safe else ""
            if music_ext not in _AUDIO_EXTENSIONS:
                flash("Story music must be MP3, M4A, WAV, OGG, AAC, or WEBM.")
                return redirect(url_for("dashboard") + "#stories")
            try:
                music_path = save_upload(music_upload, "story-music", uuid.uuid4().hex + "." + music_ext,
                                         mimetypes.guess_type(music_safe)[0] or "audio/mpeg")
                music_title = music_title or Path(music_safe).stem
            except Exception:
                app.logger.exception("Story music upload failed")
                flash("Story music could not be uploaded.")
                return redirect(url_for("dashboard") + "#stories")
        stored_name = uuid.uuid4().hex + "." + ext
        content_type = mimetypes.guess_type(safe)[0] or ("video/mp4" if media_type == "video" else "image/jpeg")
        try:
            media_path = save_upload(upload, "stories", stored_name, content_type)
            with get_db() as db:
                db.execute(
                    "INSERT INTO stories (user_id,media_path,media_type,caption,music_path,music_title,expires_at) VALUES (?,?,?,?,?,?,?)",
                    (session["user_id"], media_path, media_type, caption, music_path, music_title, datetime.utcnow() + timedelta(hours=24))
                )
            flash("Your story is live for 24 hours.")
        except Exception:
            app.logger.exception("Story upload failed")
            flash("The story could not be uploaded. Please try again.")
        return redirect(url_for("dashboard") + "#stories")

    @social.route("/stories/<int:story_id>")
    @login_required
    def view_story(story_id):
        with get_db() as db:
            story = db.execute("""
                SELECT s.*, u.display_name, u.profile_image
                FROM stories s JOIN users u ON u.id=s.user_id
                WHERE s.id=? AND s.expires_at > CURRENT_TIMESTAMP
            """, (story_id,)).fetchone()
        if not story:
            flash("That story has expired or is no longer available.")
            return redirect(url_for("dashboard"))
        return render_template("story.html", story=story, media_url=media_url)

    @social.route("/people/<int:person_id>")
    @login_required
    def public_profile(person_id):
        with get_db() as db:
            person = db.execute("SELECT id,display_name,bio,interests,profile_image,created_at FROM users WHERE id=?", (person_id,)).fetchone()
            if not person:
                abort(404)
            counts = db.execute("""
                SELECT
                  (SELECT COUNT(*) FROM follows WHERE followed_id=?) AS followers,
                  (SELECT COUNT(*) FROM follows WHERE follower_id=?) AS following,
                  (SELECT COUNT(*) FROM posts WHERE user_id=?) AS posts
            """, (person_id, person_id, person_id)).fetchone()
            follows = False
            if person_id != session["user_id"]:
                follows = bool(db.execute("SELECT 1 FROM follows WHERE follower_id=? AND followed_id=?", (session["user_id"], person_id)).fetchone())
            posts = [dict(p) for p in db.execute("""
                SELECT id,user_id,content,media_path,media_type,music_path,music_title,created_at
                FROM posts WHERE user_id=? ORDER BY created_at DESC,id DESC LIMIT 60
            """, (person_id,)).fetchall()]
        return render_template("public_profile.html", person=person, counts=counts, follows=follows, profile_posts=posts, media_url=media_url)

    @social.route("/people/<int:person_id>/follow", methods=["POST"])
    @login_required
    def toggle_follow(person_id):
        if person_id == session["user_id"]:
            flash("You cannot follow your own profile.")
            return redirect(url_for("social.public_profile", person_id=person_id))
        with get_db() as db:
            exists = db.execute("SELECT id FROM users WHERE id=?", (person_id,)).fetchone()
            if not exists:
                abort(404)
            current = db.execute("SELECT 1 FROM follows WHERE follower_id=? AND followed_id=?", (session["user_id"], person_id)).fetchone()
            if current:
                db.execute("DELETE FROM follows WHERE follower_id=? AND followed_id=?", (session["user_id"], person_id))
                flash("You unfollowed this member.")
            else:
                db.execute("INSERT INTO follows (follower_id,followed_id) VALUES (?,?)", (session["user_id"], person_id))
                flash("You are now following this member.")
        return redirect(url_for("social.public_profile", person_id=person_id))

    @social.route("/posts/<int:post_id>/music", methods=["POST"])
    @login_required
    def attach_music(post_id):
        upload = request.files.get("music_file")
        title = request.form.get("music_title", "").strip()[:120]
        if not upload or not upload.filename:
            flash("Choose an audio file to attach to your post.")
            return redirect(url_for("dashboard") + "#post-" + str(post_id))
        safe = secure_filename(upload.filename)
        ext = safe.rsplit(".", 1)[-1].lower() if "." in safe else ""
        if ext not in _AUDIO_EXTENSIONS:
            flash("Music files must be MP3, M4A, WAV, OGG, AAC, or WEBM audio.")
            return redirect(url_for("dashboard") + "#post-" + str(post_id))
        with get_db() as db:
            post = db.execute("SELECT id,user_id FROM posts WHERE id=?", (post_id,)).fetchone()
            if not post:
                abort(404)
            if post["user_id"] != session["user_id"]:
                abort(403)
            try:
                stored_name = uuid.uuid4().hex + "." + ext
                content_type = mimetypes.guess_type(safe)[0] or "audio/mpeg"
                music_path = save_upload(upload, "music", stored_name, content_type)
                db.execute("UPDATE posts SET music_path=?, music_title=? WHERE id=?", (music_path, title or Path(safe).stem, post_id))
            except Exception:
                app.logger.exception("Post music upload failed")
                flash("The music upload failed. Please try a smaller audio file.")
                return redirect(url_for("dashboard") + "#post-" + str(post_id))
        flash("Music added to your post.")
        return redirect(url_for("dashboard") + "#post-" + str(post_id))


    @social.route("/posts/<int:post_id>/music/search", methods=["GET", "POST"])
    @login_required
    def search_online_music(post_id):
        with get_db() as db:
            post = db.execute("SELECT id,user_id FROM posts WHERE id=?", (post_id,)).fetchone()
            if not post:
                abort(404)
            if post["user_id"] != session["user_id"]:
                abort(403)

        if request.method == "POST":
            preview_url = request.form.get("preview_url", "").strip()
            title = request.form.get("track_title", "").strip()[:120]
            parsed = urlparse(preview_url)
            allowed_hosts = {"audio-ssl.itunes.apple.com", "audio.itunes.apple.com"}
            if parsed.scheme != "https" or parsed.hostname not in allowed_hosts or not title:
                flash("Please choose a valid song preview from the online search results.")
                return redirect(url_for("social.search_online_music", post_id=post_id))
            with get_db() as db:
                db.execute("UPDATE posts SET music_path=?, music_title=? WHERE id=? AND user_id=?",
                           (preview_url, title, post_id, session["user_id"]))
            flash("Online song preview added to your post.")
            return redirect(url_for("dashboard") + "#post-" + str(post_id))

        query = request.args.get("q", "").strip()[:100]
        tracks = []
        error = ""
        if query:
            try:
                params = urlencode({"term": query, "media": "music", "entity": "song", "limit": 15, "country": "KE"})
                req = Request("https://itunes.apple.com/search?" + params, headers={"User-Agent": "HouseOfJacob/1.0"})
                with urlopen(req, timeout=8) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                for item in payload.get("results", []):
                    preview = item.get("previewUrl", "")
                    host = urlparse(preview).hostname if preview else None
                    if preview and host in {"audio-ssl.itunes.apple.com", "audio.itunes.apple.com"}:
                        tracks.append({
                            "title": item.get("trackName", "Unknown track"),
                            "artist": item.get("artistName", "Unknown artist"),
                            "preview_url": preview,
                            "artwork": item.get("artworkUrl100", ""),
                            "store_url": item.get("trackViewUrl", "")
                        })
            except Exception:
                app.logger.exception("Online music search failed")
                error = "Online music search is temporarily unavailable. Please try again later."
        return render_template("online_music.html", post_id=post_id, query=query, tracks=tracks, error=error)

    @social.route("/settings", methods=["GET", "POST"])
    @login_required
    def profile_settings():
        if request.method == "POST":
            display_name = request.form.get("display_name", "").strip()[:80]
            bio = request.form.get("bio", "").strip()[:240]
            interests = request.form.get("interests", "").strip()[:240]
            upload = request.files.get("profile_image")
            profile_image = None
            if not display_name:
                flash("Display name cannot be empty.")
                return redirect(url_for("social.profile_settings"))
            if upload and upload.filename:
                safe = secure_filename(upload.filename)
                ext = safe.rsplit(".", 1)[-1].lower() if "." in safe else ""
                if ext not in {"png", "jpg", "jpeg", "gif", "webp"}:
                    flash("Profile photos must be PNG, JPG, GIF, or WEBP.")
                    return redirect(url_for("social.profile_settings"))
                try:
                    content_type = mimetypes.guess_type(safe)[0] or "image/jpeg"
                    profile_image = save_upload(upload, "profiles", uuid.uuid4().hex + "." + ext, content_type)
                except Exception:
                    app.logger.exception("Settings profile photo upload failed")
                    flash("The profile photo could not be uploaded.")
                    return redirect(url_for("social.profile_settings"))
            with get_db() as db:
                if profile_image:
                    db.execute("UPDATE users SET display_name=?,bio=?,interests=?,profile_image=? WHERE id=?", (display_name,bio,interests,profile_image,session["user_id"]))
                else:
                    db.execute("UPDATE users SET display_name=?,bio=?,interests=? WHERE id=?", (display_name,bio,interests,session["user_id"]))
            flash("Your profile settings have been saved.")
            return redirect(url_for("social.profile_settings"))
        with get_db() as db:
            user = db.execute("SELECT id,display_name,email,bio,interests,profile_image,created_at FROM users WHERE id=?", (session["user_id"],)).fetchone()
        return render_template("profile_settings.html", user=user, media_url=media_url)


    @social.route("/community-rules")
    @login_required
    def community_rules():
        return render_template("community_rules.html")

    @social.route("/verification", methods=["GET", "POST"])
    @login_required
    def verification_request():
        with get_db() as db:
            if is_postgres:
                db.execute("""CREATE TABLE IF NOT EXISTS verification_requests (
                    id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    public_links TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, reviewed_at TIMESTAMP)""")
            else:
                db.execute("""CREATE TABLE IF NOT EXISTS verification_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    public_links TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, reviewed_at TIMESTAMP)""")
            current = db.execute("SELECT id,status,created_at FROM verification_requests WHERE user_id=? ORDER BY id DESC LIMIT 1",
                                 (session["user_id"],)).fetchone()
            if request.method == "POST":
                if current and current["status"] == "pending":
                    flash("Your verification request is already waiting for review.")
                    return redirect(url_for("social.verification_request"))
                public_links = request.form.get("public_links", "").strip()[:1200]
                reason = request.form.get("reason", "").strip()[:1000]
                if not public_links or not reason:
                    flash("Add public links and explain why the account should be verified.")
                    return redirect(url_for("social.verification_request"))
                db.execute("INSERT INTO verification_requests (user_id,public_links,reason,status) VALUES (?,?,?,'pending')",
                           (session["user_id"], public_links, reason))
                flash("Verification request submitted. A moderator must review it before any badge is shown.")
                return redirect(url_for("social.verification_request"))
        return render_template("verification.html", current=current)

    @social.route("/live")
    @login_required
    def live_access():
        with get_db() as db:
            row = db.execute("SELECT COUNT(*) AS n FROM follows WHERE followed_id=?", (session["user_id"],)).fetchone()
        eligible = row["n"] >= 1000
        return render_template("live_access.html", eligible=eligible, followers=row["n"])

    app.register_blueprint(social)
