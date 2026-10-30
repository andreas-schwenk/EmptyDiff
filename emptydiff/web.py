# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""The only file that knows Flask. Only the core scope of Flask (see
DESIGN.md 5)."""

import hashlib
import hmac
import io
import json
import os
import re
import secrets
import tempfile
import time
import zipfile
from datetime import timedelta
from functools import wraps

from markupsafe import Markup
from flask import (
    Flask,
    Request,
    Response,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask.sessions import SecureCookieSessionInterface

from .core import (
    archive,
    auth,
    backup,
    db,
    grader,
    health,
    highlight,
    i18n,
    labs,
    languages,
    mail,
    provided,
    report,
    retention,
    submissions,
    util,
)
from .core import credit
from .core import config as config_module


def _load_secret_key(data_dir):
    path = os.path.join(data_dir, "secret_key")
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        return key


REMEMBER_COOKIE = "emptydiff_remember"


class PathSessionInterface(SecureCookieSessionInterface):
    """The session cookie is valid only under the path the system is
    served at (for example /lab), so that it never reaches other
    applications on the same host."""

    def get_cookie_path(self, app):
        return request.script_root or "/"


def cookie_path():
    return request.script_root or "/"


BACKUP_FORM_OVERHEAD = 1_000_000
STAGING_MAX_AGE = (
    3600  # seconds an uploaded archive may wait for the confirmation
)
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def create_app(cfg=None):
    cfg = cfg or config_module.load()
    dev = config_module.is_dev()
    os.makedirs(cfg["data_dir"], exist_ok=True)
    tmp_dir = os.path.join(cfg["data_dir"], "tmp")
    staging_dir = os.path.join(cfg["data_dir"], "import-staging")

    def tmp_path():
        """Temporary files live in the data directory: /tmp is often a
        small RAM disk (tmpfs)."""
        os.makedirs(tmp_dir, exist_ok=True)
        return tmp_dir

    class UploadRequest(Request):
        def _get_file_stream(
            self,
            total_content_length,
            content_type,
            filename=None,
            content_length=None,
        ):
            if (
                total_content_length is not None
                and total_content_length > 1_000_000
            ):
                return tempfile.TemporaryFile(
                    dir=tmp_path()
                )  # stream large uploads (archives) straight to disk
            return super()._get_file_stream(
                total_content_length, content_type, filename, content_length
            )

    app = Flask(__name__)
    app.request_class = UploadRequest
    config_module.check_production(cfg)
    app.config["EMPTYDIFF"] = cfg
    app.config["EMPTYDIFF_DEV"] = dev
    app.config["MAX_CONTENT_LENGTH"] = (
        cfg["limits"]["max_upload_bytes"] + 100_000
    )  # multipart overhead
    app.config["SECRET_KEY"] = _load_secret_key(cfg["data_dir"])
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    secure_cookies = not dev and config_module.uses_https(cfg)
    app.config["SESSION_COOKIE_SECURE"] = secure_cookies
    app.session_interface = PathSessionInterface()
    remember_days = cfg["auth"]["remember_days"]
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
        hours=cfg["auth"]["session_hours"]
    )
    db.init_db(db.path_for(cfg)).close()

    admins = {a.strip().lower() for a in cfg["auth"]["admins"]}

    # ---------- Helpers ----------

    def get_db():
        if "db" not in g:
            g.db = db.connect(db.path_for(cfg))
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    def current_user():
        return session.get("email")

    def is_admin():
        return current_user() in admins

    def login_required(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not current_user():
                return redirect(url_for("login"))
            return view(*args, **kwargs)

        return wrapper

    def admin_required(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not current_user():
                return redirect(url_for("login"))
            if not is_admin():
                abort(403)
            return view(*args, **kwargs)

        return wrapper

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]

    # ---------- Language ----------

    def current_language():
        """Chosen language (session), otherwise the best one from the
        browser, otherwise `ui.default_language`."""
        chosen = session.get("lang")
        if chosen in i18n.languages():
            return chosen
        return i18n.best_language(
            request.accept_languages, cfg["ui"]["default_language"]
        )

    def t(key, /, **params):
        """Translated text; keys ending in `_html` return HTML (the
        parameters are escaped in the process)."""
        lang = g.get("lang") or cfg["ui"]["default_language"]
        if key.endswith("_html"):
            return Markup(i18n.lookup(lang, key)).format(**params)
        return i18n.translate(lang, key, **params)

    def tn(key, n, /, **params):
        return i18n.plural(
            g.get("lang") or cfg["ui"]["default_language"], key, n, **params
        )

    def msg(error):
        """Translated message of a LocalizedError (access, grading,
        archive etc.)."""
        return error.message(g.get("lang") or cfg["ui"]["default_language"])

    def link(url, text):
        """An escaped link as HTML, to be inserted into `_html`
        texts."""
        return Markup('<a href="{}">{}</a>').format(url, text)

    def contact_html():
        """ " (<a>address</a>)" for `_html` texts that send students to
        their supervisors, or empty."""
        address = cfg["privacy"]["contact_email"].strip()
        return (
            Markup(' (<a href="mailto:{0}">{0}</a>)').format(address)
            if address
            else ""
        )

    app.jinja_env.globals.update(
        csrf_token=csrf_token, t=t, tn=tn, link=link, contact_html=contact_html
    )
    app.jinja_env.filters["localtime"] = lambda text: util.local_str(
        text, t("fmt.datetime")
    )
    app.jinja_env.filters["localdate"] = lambda text: util.local_date_str(
        text, t("fmt.date")
    )

    @app.before_request
    def set_language():
        g.lang = current_language()

    @app.before_request
    def restore_login():
        """Log in via the remember-me cookie when the session is missing
        or expired ("stay logged in")."""
        token = request.cookies.get(REMEMBER_COOKIE)
        if not token or request.endpoint == "static" or session.get("email"):
            return
        email = auth.check_remember_token(get_db(), token, remember_days)
        if email:
            session["email"] = email
        else:
            g.forget_remember = (
                True  # invalid or expired token: remove the cookie
            )

    @app.before_request
    def backup_upload_limit():
        """Only admins may send large files on restore; everyone else
        stays at the small limit."""
        if (
            request.method == "POST"
            and request.path == "/admin/backup/upload"
            and is_admin()
        ):
            request.max_content_length = (
                cfg["limits"]["max_backup_bytes"] + BACKUP_FORM_OVERHEAD
            )

    @app.before_request
    def check_csrf():
        if request.method == "POST":
            expected = session.get("csrf")
            sent = request.form.get("csrf_token", "")
            # without a token in the session there is nothing to
            # compare: reject (not "" == "")
            if not expected or not hmac.compare_digest(sent, expected):
                abort(400)

    @app.after_request
    def security_headers(resp):
        if g.get("forget_remember"):
            resp.delete_cookie(REMEMBER_COOKIE, path=cookie_path())
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "X-Robots-Tag", "noindex, nofollow"
        )  # a private system: nothing belongs into search engines
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; form-action "
            "'self'; object-src 'none'; frame-ancestors 'none'",
        )
        return resp

    # Browsers (Safari above all) keep an icon for a long time. The
    # links carry the age of the icon files, so a new icon is fetched
    # at once.
    icon_version = int(
        max(
            os.path.getmtime(os.path.join(app.static_folder, name))
            for name in ("favicon.svg", "favicon.ico", "apple-touch-icon.png")
        )
    )

    @app.context_processor
    def inject():
        return {
            "dev": dev,
            "user": current_user(),
            "is_admin": is_admin(),
            "lang": g.lang,
            "languages": [
                (code, i18n.language_name(code)) for code in i18n.languages()
            ],
            "sim_mails": mail.pop_simulated() if dev else [],
            "imprint_url": cfg["privacy"]["imprint_url"].strip(),
            "max_attempts": cfg["limits"]["max_attempts"],
            "icon_version": icon_version,
        }

    @app.route("/favicon.ico")
    @app.route("/apple-touch-icon.png")
    def root_icons():
        """Browsers ask for these icons at the root of the site,
        whatever the page says."""
        return send_from_directory(
            app.static_folder, request.path.lstrip("/"), max_age=86400
        )

    @app.route("/lang/<code>")
    def set_lang(code):
        """Choose the language for this session and return to the page
        (local paths only)."""
        code = code.upper()
        if code not in i18n.languages():
            abort(404)
        session["lang"] = code
        back = request.args.get("next", "/")
        if not back.startswith("/") or back.startswith("//") or "\\" in back:
            back = "/"
        return redirect(request.script_root + back)

    # ---------- Login ----------

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            conn = get_db()
            shown_at = session.get("login_form_at")
            # the hidden field: only bots fill it. Answer as if all went
            # well.
            if request.form.get("extra_url"):
                return redirect(url_for("login_verify"))
            if (
                shown_at is None
                or time.time() - shown_at < cfg["auth"]["min_form_seconds"]
            ):
                flash(
                    t("err.auth.too_fast"), "error"
                )  # a person can simply try again
                return redirect(url_for("login"))
            try:
                email = auth.normalize_email(
                    request.form.get("email"), cfg["auth"]["allowed_domains"]
                )
                ttl = cfg["auth"]["code_ttl_minutes"]
                limits = cfg["auth"]
                code = auth.issue_code(
                    conn,
                    email,
                    config_module.client_address(
                        cfg,
                        request.remote_addr,
                        request.headers.get("X-Forwarded-For", ""),
                    ),
                    ttl,
                    per_email=limits["max_codes_per_email_hour"],
                    per_ip=limits["max_codes_per_ip_hour"],
                    cooldown_seconds=limits["code_cooldown_seconds"],
                )
                mail.send(cfg, email, *mail.login_code_mail(code, ttl, g.lang))
            except auth.AuthError as e:
                flash(msg(e), "error")
                return redirect(url_for("login"))
            except mail.MailError:
                # The code exists. Open the page for it anyway, so that
                # one handed out by a supervisor (`issue-code`) works.
                flash(t("flash.mail_failed"), "error")
            session["pending_email"] = email
            session["remember"] = bool(
                request.form.get("remember")
            )  # consent to the remember-me cookie
            return redirect(url_for("login_verify"))
        session["login_form_at"] = (
            time.time()
        )  # a form that comes back faster than a person types is a bot
        domains = cfg["auth"]["allowed_domains"]
        return render_template(
            "login.html",
            remember_days=remember_days,
            domains=", ".join("@" + d for d in domains),
            email_hint=t(
                "login.email_hint",
                domain=domains[0] if domains else "example.org",
            ),
        )

    @app.route("/login/verify", methods=["GET", "POST"])
    def login_verify():
        email = session.get("pending_email")
        if not email:
            return redirect(url_for("login"))
        if request.method == "POST":
            if auth.verify_code(get_db(), email, request.form.get("code")):
                remember, chosen = session.get("remember"), session.get("lang")
                # session cookie without expiry date: ends with the
                # browser (or after session_hours)
                session.clear()
                if chosen:
                    session["lang"] = chosen  # the chosen language is kept
                session["email"] = email
                resp = redirect(url_for("index"))
                if remember:
                    token = auth.issue_remember_token(
                        get_db(), email, remember_days
                    )
                    resp.set_cookie(
                        REMEMBER_COOKIE,
                        token,
                        max_age=remember_days * 86400,
                        httponly=True,
                        samesite="Lax",
                        secure=secure_cookies,
                        path=cookie_path(),
                    )
                return resp
            flash(t("flash.bad_code"), "error")
            return redirect(url_for("login_verify"))
        return render_template(
            "login_verify.html",
            email=email,
            minutes=cfg["auth"]["code_ttl_minutes"],
        )

    @app.route("/logout", methods=["POST"])
    def logout():
        auth.revoke_remember_token(
            get_db(), request.cookies.get(REMEMBER_COOKIE)
        )
        chosen = session.get("lang")
        session.clear()
        if chosen:
            session["lang"] = chosen
        resp = redirect(url_for("login"))
        resp.delete_cookie(REMEMBER_COOKIE, path=cookie_path())
        return resp

    # ---------- Operations ----------

    @app.route("/healthz")
    def healthz():
        """For external monitoring: 200 {"status": "ok"} or 503 with
        problem identifiers (no personal data)."""
        try:
            problems = health.check(get_db(), cfg)
        except Exception:  # broken database file etc.: do not answer with 500
            problems = ["db"]
        body = (
            {"status": "fail", "problems": problems}
            if problems
            else {"status": "ok"}
        )
        resp = Response(
            json.dumps(body) + "\n",
            mimetype="application/json",
            status=503 if problems else 200,
        )
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---------- Privacy ----------

    @app.route("/privacy")
    def privacy():
        days = cfg["retention_days"]
        p = cfg["privacy"]
        authority, url = p["authority"].strip(), p["authority_url"].strip()
        if not authority:
            authority_link = Markup('<mark class="missing">{}</mark>').format(
                t("priv.missing", field="authority")
            )
        elif url.startswith(("https://", "http://")):
            authority_link = Markup(
                '<a href="{}" rel="noopener">{}</a>'
            ).format(url, authority)
        else:
            authority_link = authority
        return render_template(
            "privacy.html",
            p=cfg["privacy"],
            retention_days=days,
            retention_years=round(days / 365, 1),
            remember_days=remember_days,
            session_hours=cfg["auth"]["session_hours"],
            code_ttl=cfg["auth"]["code_ttl_minutes"],
            authority_link=authority_link,
            login_code_hours=int(
                retention.LOGIN_CODE_KEEP.total_seconds() // 3600
            ),
        )

    # ---------- Students ----------

    @app.route("/")
    @login_required
    def index():
        rows = submissions.overview_for_user(
            get_db(), current_user(), all_labs=is_admin()
        )
        return render_template("index.html", rows=rows)

    @app.route("/lab/<name>", methods=["GET", "POST"])
    @login_required
    def lab(name):
        conn = get_db()
        lab_row = conn.execute(
            "SELECT * FROM lab WHERE name = ?", (name,)
        ).fetchone()
        if lab_row is None or not (lab_row["active"] or is_admin()):
            # hidden labs are only for admins, who may try them out
            # (preview)
            abort(404)
        if request.method == "POST":
            files = {}
            for f in request.files.getlist("files"):
                if not f.filename:
                    continue
                files[os.path.basename(f.filename.replace("\\", "/"))] = (
                    f.read()
                )
            try:
                sub_id = submissions.submit(
                    conn, cfg, name, current_user(), files, preview=is_admin()
                )
            except submissions.SubmissionError as e:
                flash(msg(e), "error")
                return redirect(url_for("lab", name=name))
            return redirect(url_for("submission", sub_id=sub_id))
        return render_template(
            "lab.html",
            lab=lab_row,
            required=submissions.required_files(lab_row),
            provided=provided.names(lab_row["language"], lab_row["provided"]),
            subs=submissions.list_for_user(conn, current_user(), name),
            overdue=lab_row["due_at"] < util.to_iso(util.utcnow()),
            upload_mb=cfg["limits"]["max_upload_bytes"] // 1_000_000,
        )

    def lab_source_views(lab_row):
        """Testbench, helper and provided functions of a lab as shown
        (highlighted) under a submission; those that are empty are left
        out."""
        language = lab_row["language"]
        try:
            extension = languages.get(language).extension
        except languages.UnknownLanguage:
            extension = ""
        parts = (
            ("testbench", "form.testbench", lab_row["testbench"]),
            ("helpers", "form.helpers", lab_row["helpers"]),
            ("provided", "form.provided", lab_row["provided"]),
        )
        return [
            {
                "title": title,
                "view": highlight.view(
                    name + extension, text.encode("utf-8"), language
                ),
            }
            for name, title, text in parts
            if text.strip()
        ]

    def render_submission(sub, admin_view):
        """The page of a submission. The admin view (source code, flags
        and the result even if students do not get it) is for lecturers
        who come from the administration; the student view is what the
        student sees."""
        conn = get_db()
        lab_row = conn.execute(
            "SELECT * FROM lab WHERE name = ?", (sub["lab"],)
        ).fetchone()
        show_result = bool(lab_row["show_report"]) or admin_view
        # Whoever reaches this page owns the submission or is a
        # lecturer: both see the submitted files.
        files = []
        try:
            files = [
                highlight.view(n, c, lab_row["language"])
                for n, c in submissions.load_files(cfg, sub).items()
            ]
        except OSError:
            pass
        lab_sources, run = [], None
        if admin_view:
            lab_sources = lab_source_views(lab_row)
            if sub["status"] in ("done", "error"):
                stdout, stderr = report.raw_output(sub, g.lang)
                checks = report.checks_of(sub)
                run = {
                    "stdout": stdout,
                    "stderr": stderr,
                    "checks": checks,
                    "checks_ok": sum(1 for ok, _ in checks if ok),
                    "checks_total": len(checks),
                }
        return render_template(
            "submission.html",
            sub=sub,
            lab=lab_row,
            admin_view=admin_view,
            lab_sources=lab_sources,
            run=run,
            show_result=show_result,
            files=files,
            report=(
                report.build(sub, g.lang, config_module.contact_hint(cfg))
                if sub["status"] in ("done", "error") and show_result
                else None
            ),
            pending=sub["status"] in ("queued", "running"),
        )

    @app.route("/submission/<int:sub_id>")
    @login_required
    def submission(sub_id):
        sub = submissions.get(get_db(), sub_id)
        if sub is None or (sub["email"] != current_user() and not is_admin()):
            # other people's submissions cannot be told apart from
            # non-existent ones
            abort(404)
        # A lecturer sees their own submissions here exactly like a
        # student; those of others in the admin view. The admin view of
        # any submission (also an own one) is /admin/submission/<id>.
        own = sub["email"] == current_user()
        return render_submission(sub, admin_view=is_admin() and not own)

    # ---------- Administration (admin) ----------

    def lab_or_404(name):
        lab_row = labs.get(get_db(), name)
        if lab_row is None:
            abort(404)
        return lab_row

    def course_or_404(course):
        if not labs.course_exists(get_db(), course):
            abort(404)
        return course

    def back_to_admin(anchor=None):
        return redirect(url_for("admin_index", open=anchor, _anchor=anchor))

    @app.route("/admin/")
    @admin_required
    def admin_index():
        plan = retention.schedule(get_db(), cfg["retention_days"])
        missing = [
            k
            for k in (
                "controller",
                "controller_address",
                "controller_email",
                "authority",
            )
            if not cfg["privacy"][k].strip()
        ]
        return render_template(
            "admin_index.html",
            courses=labs.overview(get_db(), exclude=tuple(sorted(admins))),
            now=util.to_iso(util.utcnow()),
            plan=plan,
            due_soon=retention.warnings(get_db(), cfg["retention_days"]),
            privacy_missing=missing,
        )

    @app.route("/admin/help")
    @admin_required
    def admin_help():
        days = cfg["retention_days"]
        return render_template(
            "admin_help.html",
            days=days,
            years=round(days / 365, 1),
            timeout=cfg["grader"]["timeout_seconds"],
        )

    @app.route("/admin/lab/<name>/active", methods=["POST"])
    @admin_required
    def admin_lab_active(name):
        lab_row = lab_or_404(name)
        labs.set_active(get_db(), name, request.form.get("active") == "1")
        return back_to_admin(lab_row["course"])

    def lab_form(name=None, course=None):
        """Shared part of create and edit. `name` is None when creating
        (then `course` is the course)."""
        conn = get_db()
        lab_row = lab_or_404(name) if name else None
        if lab_row is None:
            course_or_404(course)
        if request.method == "POST":
            try:
                fields = labs.clean_fields(
                    request.form,
                    languages.enabled(cfg),
                    lab_row["language"] if lab_row else None,
                )
                if lab_row is None:
                    name_new = labs.create(
                        conn, course, request.form.get("ident"), fields
                    )
                else:
                    name_new = name
                    labs.update(conn, name, fields)
            except labs.LabError as e:
                flash(msg(e), "error")
                return (
                    render_template(
                        "admin_lab_form.html",
                        **form_context(lab_row, request.form, course),
                    ),
                    400,
                )
            for key in labs.warnings_for(
                fields["testbench"], fields["helpers"], fields["language"]
            ):
                flash(t(key), "error")
            flash(
                (
                    t("flash.saved")
                    if lab_row
                    else t("flash.lab_created", name=name_new)
                ),
                "ok",
            )
            return redirect(url_for("admin_lab_edit", name=name_new))
        return render_template(
            "admin_lab_form.html", **form_context(lab_row, None, course)
        )

    def form_context(lab_row, posted, course=None):
        """Values for the form: submitted input, otherwise the lab,
        otherwise defaults."""
        if posted is not None:
            values = {
                k: posted.get(k, "")
                for k in (
                    "ident",
                    "description",
                    "due_at",
                    "required_files",
                    "testbench",
                    "helpers",
                    "provided",
                )
            }
            values["language"] = posted.get("language") or (
                lab_row["language"] if lab_row else default_language(course)
            )
            if (
                values["language"] not in languages.ids()
            ):  # the form is shown again with the error message
                values["language"] = (
                    lab_row["language"]
                    if lab_row
                    else default_language(course)
                )
            values["show_report"] = bool(posted.get("show_report"))
        elif lab_row is not None:
            values = {
                "ident": "",
                "language": lab_row["language"],
                "description": lab_row["description"],
                "due_at": labs.due_for_input(lab_row["due_at"]),
                "required_files": lab_row["required_files"],
                "testbench": lab_row["testbench"],
                "helpers": lab_row["helpers"],
                "provided": lab_row["provided"],
                "show_report": bool(lab_row["show_report"]),
            }
        else:
            language = default_language(course)
            values = {
                "ident": "",
                "language": language,
                "description": "",
                "due_at": "",
                "required_files": "",
                "testbench": t(f"ln.{language}.tb_template"),
                "helpers": "",
                "provided": "",
                "show_report": True,
            }
        n_subs = 0
        if lab_row:
            n_subs = (
                get_db()
                .execute(
                    "SELECT COUNT(*) FROM submission WHERE lab = ?",
                    (lab_row["name"],),
                )
                .fetchone()[0]
            )
        # the languages to choose from: the ones the server offers, and
        # the one the lab has even if it is not offered
        choices = list(
            dict.fromkeys(languages.enabled(cfg) + [values["language"]])
        )
        return {
            "lab": lab_row,
            "course": lab_row["course"] if lab_row else course,
            "v": values,
            "n_subs": n_subs,
            "timeout": cfg["grader"]["timeout_seconds"],
            "language_choices": [
                {
                    "id": i,
                    "name": languages.get(i).name,
                    "extension": languages.get(i).extension,
                    "offered": i in languages.enabled(cfg),
                }
                for i in choices
            ],
            "language_locked": n_subs > 0,
        }

    def default_language(course):
        """Language of a new lab: that of the latest lab of the course,
        otherwise the first one the server offers."""
        row = (
            get_db()
            .execute(
                "SELECT language FROM lab WHERE course = ? ORDER BY "
                "name DESC LIMIT 1",
                (course,),
            )
            .fetchone()
        )
        offered = languages.enabled(cfg)
        return (
            row["language"]
            if row and row["language"] in offered
            else offered[0]
        )

    @app.route("/admin/course/<course>/lab/new", methods=["GET", "POST"])
    @admin_required
    def admin_lab_new(course):
        return lab_form(None, course)

    @app.route("/admin/course/new", methods=["GET", "POST"])
    @admin_required
    def admin_course_new():
        name = ""
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            try:
                labs.create_course(get_db(), name)
            except labs.LabError as e:
                flash(msg(e), "error")
                return render_template("admin_course_new.html", name=name), 400
            flash(t("flash.course_created", name=name), "ok")
            return back_to_admin(name)
        return render_template("admin_course_new.html", name=name)

    @app.route("/admin/course/<course>/delete", methods=["POST"])
    @admin_required
    def admin_course_delete(course):
        course_or_404(course)
        try:
            labs.delete_course(get_db(), course)
        except labs.LabError as e:
            flash(msg(e), "error")
            return back_to_admin(course)
        flash(t("flash.course_deleted", name=course), "ok")
        return redirect(url_for("admin_index"))

    @app.route("/admin/lab/<name>/edit", methods=["GET", "POST"])
    @admin_required
    def admin_lab_edit(name):
        return lab_form(name)

    @app.route("/admin/lab/<name>/delete", methods=["POST"])
    @admin_required
    def admin_lab_delete(name):
        lab_row = lab_or_404(name)
        try:
            count = labs.delete(get_db(), cfg, name)
        except labs.LabError as e:
            flash(msg(e), "error")
            return redirect(url_for("admin_lab_edit", name=name))
        flash(
            (
                tn("flash.lab_deleted_subs", count, name=name)
                if count
                else t("flash.lab_deleted", name=name)
            ),
            "ok",
        )
        return back_to_admin(lab_row["course"])

    @app.route("/admin/course/<course>/clone", methods=["GET", "POST"])
    @admin_required
    def admin_course_clone(course):
        course_or_404(course)
        conn = get_db()
        if request.method == "POST":
            try:
                try:
                    shift = int(request.form.get("shift_days", "0"))
                except ValueError:
                    raise labs.LabError("err.clone.shift_int") from None
                if not -3650 <= shift <= 3650:
                    raise labs.LabError("err.clone.shift_big")
                dst = (request.form.get("new_course") or "").strip()
                names = labs.clone_course(conn, course, dst, shift)
            except labs.LabError as e:
                flash(msg(e), "error")
                return redirect(url_for("admin_course_clone", course=course))
            flash(t("flash.cloned", n=len(names), dst=dst), "ok")
            return back_to_admin(dst)
        n = conn.execute(
            "SELECT COUNT(*) FROM lab WHERE course = ?", (course,)
        ).fetchone()[0]
        return render_template("admin_clone.html", course=course, count=n)

    @app.route("/admin/lab/<name>/submissions")
    @admin_required
    def admin_lab_submissions(name):
        lab_row = lab_or_404(name)
        subs = labs.submissions_of_lab(get_db(), name)
        by_student = {}
        for s in subs:
            by_student.setdefault(s["email"], []).append(s)
        # per student: the latest submission first, the earlier ones
        # (newest first) are folded away in the page
        groups = []
        for email in sorted(by_student):
            items = sorted(by_student[email], key=lambda s: -s["attempt"])
            groups.append(
                {
                    "id": "g-"
                    + hashlib.sha256(email.encode()).hexdigest()[:10],
                    "email": email,
                    "latest": items[0],
                    "older": items[1:],
                    "count": len(items),
                }
            )
        return render_template(
            "admin_submissions.html",
            lab=lab_row,
            subs=subs,
            groups=groups,
            pending=any(s["status"] in ("queued", "running") for s in subs),
        )

    def zip_response(entries, filename):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for path, content in entries:
                z.writestr(path, content)
        resp = Response(buf.getvalue(), mimetype="application/zip")
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="{filename}"'
        )
        return resp

    def safe(text):
        return re.sub(r"[^A-Za-z0-9._-]", "_", text)

    @app.route("/admin/submission/<int:sub_id>")
    @admin_required
    def admin_submission(sub_id):
        """A submission as lecturers see it: source code, flags and
        result, whoever submitted it (the credit list and the list of a
        lab link here)."""
        sub = submissions.get(get_db(), sub_id)
        if sub is None:
            abort(404)
        return render_submission(sub, admin_view=True)

    @app.route("/admin/submission/<int:sub_id>/download")
    @admin_required
    def admin_submission_download(sub_id):
        sub = submissions.get(get_db(), sub_id)
        if sub is None:
            abort(404)
        try:
            files = submissions.load_files(cfg, sub)
        except OSError:
            abort(404)
        base = f"{sub['lab']}-{safe(sub['email'])}-{sub['attempt']:03d}"
        return zip_response(
            [(f"{base}/{n}", c) for n, c in files.items()], base + ".zip"
        )

    def image_response(sub, name):
        """An image of a submission for the source view. Only real
        images are served, with the type found in the data (not the one
        the file name claims), and never as something a browser could
        run.
        """
        if sub is None or not grader.FILENAME_RE.match(name):
            abort(404)
        try:
            content = submissions.load_files(cfg, sub).get(name)
        except OSError:
            abort(404)
        mime = (
            highlight.image_type(name, content)
            if content is not None
            else None
        )
        if mime is None:
            abort(404)
        resp = Response(content, mimetype=mime)
        resp.headers["Content-Disposition"] = "inline"
        resp.headers["Cache-Control"] = (
            "private, no-store"  # personal data of a student
        )
        return resp

    @app.route("/admin/submission/<int:sub_id>/image/<name>")
    @admin_required
    def admin_submission_image(sub_id, name):
        return image_response(submissions.get(get_db(), sub_id), name)

    @app.route("/submission/<int:sub_id>/image/<name>")
    @login_required
    def submission_image(sub_id, name):
        """The same for the student who submitted it (and lecturers)."""
        sub = submissions.get(get_db(), sub_id)
        if sub is not None and sub["email"] != current_user():
            if not is_admin():
                sub = None  # as if it did not exist
        return image_response(sub, name)

    @app.route("/admin/lab/<name>/archive")
    @admin_required
    def admin_lab_archive(name):
        """The whole lab as a ZIP: configuration, testbench, all
        submissions and reports."""
        lab_or_404(name)
        tmp = tempfile.TemporaryFile(
            dir=tmp_path()
        )  # on disk instead of in memory: labs can be large
        archive.write(get_db(), cfg, name, tmp)
        return file_response(tmp, f"{safe(name)}-archive.zip")

    def file_response(tmp, filename):
        """Serve a temporary file as a ZIP download (streamed; the file
        is closed afterwards)."""
        size = tmp.tell()
        tmp.seek(0)

        def chunks():
            try:
                while True:
                    data = tmp.read(65536)
                    if not data:
                        break
                    yield data
            finally:
                tmp.close()

        resp = Response(chunks(), mimetype="application/zip")
        resp.headers["Content-Length"] = str(size)
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="{filename}"'
        )
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---------- Backup (admin) ----------

    def staged_path(token):
        return (
            os.path.join(staging_dir, token + ".zip")
            if TOKEN_RE.match(token or "")
            else None
        )

    def clean_staging():
        """Remove uploaded archives that were never confirmed."""
        try:
            names = os.listdir(staging_dir)
        except OSError:
            return
        for name in names:
            path = os.path.join(staging_dir, name)
            try:
                if time.time() - os.path.getmtime(path) > STAGING_MAX_AGE:
                    os.remove(path)
            except OSError:
                pass

    def stamp():
        return util.utcnow().strftime("%Y%m%d-%H%M%S")

    @app.route("/admin/backup")
    @admin_required
    def admin_backup():
        clean_staging()
        return render_template(
            "admin_backup.html",
            max_mb=cfg["limits"]["max_backup_bytes"] // 1_000_000,
        )

    @app.route("/admin/backup/full.zip")
    @admin_required
    def admin_backup_full():
        tmp = tempfile.TemporaryFile(dir=tmp_path())
        backup.export(get_db(), cfg, tmp, "full")
        return file_response(tmp, f"emptydiff-backup-{stamp()}.zip")

    @app.route("/admin/backup/labs.zip")
    @admin_required
    def admin_backup_labs():
        tmp = tempfile.TemporaryFile(dir=tmp_path())
        backup.export(get_db(), cfg, tmp, "labs")
        return file_response(tmp, f"emptydiff-labs-{stamp()}.zip")

    def confirm_page(token, archive_, error=None, status=200):
        conn = get_db()
        current = {
            "labs": conn.execute("SELECT COUNT(*) FROM lab").fetchone()[0],
            "submissions": conn.execute(
                "SELECT COUNT(*) FROM submission"
            ).fetchone()[0],
        }
        new, updated = (
            backup.classify_labs(conn, archive_)
            if archive_.kind == "labs"
            else ([], [])
        )
        if error:
            flash(error, "error")
        return (
            render_template(
                "admin_backup_confirm.html",
                token=token,
                archive=archive_,
                current=current,
                new=new,
                updated=updated,
                size_mb=round(archive_.total_bytes / 1_000_000, 1),
            ),
            status,
        )

    @app.route("/admin/backup/upload", methods=["POST"])
    @admin_required
    def admin_backup_upload():
        upload = request.files.get("archive")
        if upload is None or not upload.filename:
            flash(t("flash.backup.no_file"), "error")
            return redirect(url_for("admin_backup"))
        clean_staging()
        os.makedirs(staging_dir, exist_ok=True)
        token = secrets.token_hex(16)
        path = staged_path(token)
        upload.save(path)
        try:
            archive_ = backup.inspect(path)
        except backup.BackupError as e:
            os.remove(path)
            flash(msg(e), "error")
            return redirect(url_for("admin_backup"))
        return confirm_page(token, archive_)

    @app.route("/admin/backup/apply", methods=["POST"])
    @admin_required
    def admin_backup_apply():
        token = request.form.get("token", "")
        path = staged_path(token)
        if path is None or not os.path.isfile(path):
            flash(t("flash.backup.gone"), "error")
            return redirect(url_for("admin_backup"))

        def discard():
            try:
                os.remove(path)
            except OSError:
                pass

        try:
            archive_ = backup.inspect(
                path
            )  # the archive may have been changed since the upload
        except backup.BackupError as e:
            discard()
            flash(msg(e), "error")
            return redirect(url_for("admin_backup"))
        word = t("backup.confirm_word")
        if (
            archive_.kind == "full"
            and request.form.get("confirm", "").strip() != word
        ):
            return confirm_page(
                token, archive_, t("flash.backup.need_word", word=word), 400
            )
        try:
            if archive_.kind == "full":
                counts = backup.restore_full(get_db(), cfg, archive_)
                flash(
                    t(
                        "flash.backup.restored",
                        labs=counts["labs"],
                        subs=counts["submissions"],
                        files=counts["submission_files"],
                    ),
                    "ok",
                )
            else:
                new, updated = backup.import_labs(get_db(), archive_)
                flash(
                    t(
                        "flash.backup.labs_imported",
                        new=len(new),
                        updated=len(updated),
                    ),
                    "ok",
                )
        except backup.BackupError as e:
            flash(msg(e), "error")
        finally:
            discard()
        return redirect(url_for("admin_backup"))

    @app.route("/admin/backup/cancel", methods=["POST"])
    @admin_required
    def admin_backup_cancel():
        path = staged_path(request.form.get("token", ""))
        if path and os.path.isfile(path):
            os.remove(path)
        flash(t("flash.backup.cancelled"), "ok")
        return redirect(url_for("admin_backup"))

    @app.route("/admin/submission/<int:sub_id>/delete", methods=["POST"])
    @admin_required
    def admin_submission_delete(sub_id):
        try:
            sub = labs.delete_submission(get_db(), cfg, sub_id)
        except labs.LabError as e:
            flash(msg(e), "error")
            sub = submissions.get(get_db(), sub_id)
            return redirect(
                url_for("admin_lab_submissions", name=sub["lab"])
                if sub
                else url_for("admin_index")
            )
        flash(
            t("flash.sub_deleted", attempt=sub["attempt"], email=sub["email"]),
            "ok",
        )
        return redirect(url_for("admin_lab_submissions", name=sub["lab"]))

    @app.route("/admin/submission/<int:sub_id>/regrade", methods=["POST"])
    @admin_required
    def admin_submission_regrade(sub_id):
        sub = submissions.get(get_db(), sub_id)
        if sub is None:
            abort(404)
        try:
            labs.regrade_submission(get_db(), sub_id)
            flash(t("flash.regrade"), "ok")
        except labs.LabError as e:
            flash(msg(e), "error")
        return redirect(url_for("admin_lab_submissions", name=sub["lab"]))

    def pick_course():
        courses = [c["course"] for c in labs.overview(get_db())]
        course = request.args.get("course") or (
            courses[0] if courses else None
        )
        if course is not None and course not in courses:
            abort(404)
        return courses, course

    @app.route("/admin/credit")
    @admin_required
    def admin_credit():
        courses, course = pick_course()
        lab_rows, rows = (
            credit.matrix(get_db(), course) if course else ([], [])
        )
        return render_template(
            "admin_credit.html",
            courses=courses,
            course=course,
            labs=lab_rows,
            rows=rows,
            labels=credit.labels(g.lang),
        )

    @app.route("/admin/credit.csv")
    @admin_required
    def admin_credit_csv():
        _courses, course = pick_course()
        if course is None:
            abort(404)
        lab_rows, rows = credit.matrix(get_db(), course)
        resp = Response(
            credit.to_csv(lab_rows, rows, g.lang), mimetype="text/csv"
        )
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="credit-{safe(course)}.csv"'
        )
        return resp

    # ---------- Error pages ----------

    def error_handler(code):
        def handler(_e):
            return (
                render_template(
                    "error.html",
                    title=t(f"http.{code}.title"),
                    text=t(f"http.{code}.text"),
                ),
                code,
            )

        return handler

    for status in (400, 403, 404, 413):
        app.register_error_handler(status, error_handler(status))

    return app
