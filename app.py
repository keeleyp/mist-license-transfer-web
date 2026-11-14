#!/usr/bin/env python3
"""
Mist License Transfer - web edition.

A Flask front end for the Mist License Transfer CLI: pick a config, pick a
destination org, then move license capacity out of the source org or return
an earlier move - each behind a confirmation page.

    python3 app.py                  # serve on [web] host/port from web.conf
    python3 app.py --dry-run        # never call the PUT, for anyone
    python3 app.py --hash-password  # print a password_hash line for web.conf

Configs are the CLI's .ini files, placed in this folder. Users, their
passwords (hashed) and the configs each may use live in web.conf.
"""

import argparse
import configparser
import getpass
import logging
import logging.handlers
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import license_core as core
from license_core import TransferError

__version__ = "1.1.0"

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_WEB_CONF = BASE_DIR / "web.conf"
CHANGE_LOG = BASE_DIR / "license_transfer_changes.log"
ACTIVITY_LOG = BASE_DIR / "license_transfer_activity.log"
ACTIVITY_LOG_MAX_BYTES = 5 * 1024 * 1024   # rotated at 5 MB, keeping 10 old files
ACTIVITY_LOG_BACKUPS = 10
DEFAULT_PORT = 5030

MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 15 * 60
ORG_NAME_TTL = 10 * 60

log = logging.getLogger("mist_license_transfer_web")


class WebConfError(Exception):
    pass


# --------------------------------------------------------------------------
# web.conf: [web] settings + one [user NAME] section per login
# --------------------------------------------------------------------------

def find_config_files(folder: Path = BASE_DIR) -> dict:
    """{file name: path} for every .ini in the app folder (the .ini.example
    template doesn't match *.ini)."""
    return {p.name: p for p in sorted(folder.glob("*.ini"), key=lambda p: p.name.lower())}


def load_web_conf(path: Path, config_folder: Path = BASE_DIR) -> dict:
    if not path.exists():
        raise WebConfError(
            f"{path.name} not found. Copy web.conf.example to {path.name} and add at least one "
            f"[user NAME] section (python3 app.py --hash-password makes the password_hash line)."
        )
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path)
    except configparser.Error as e:
        raise WebConfError(f"{path.name} could not be parsed: {e}")

    web = parser["web"] if "web" in parser else {}
    try:
        port = int(web.get("port", DEFAULT_PORT))
        session_minutes = int(web.get("session_minutes", 60))
    except ValueError:
        raise WebConfError(f"{path.name}: [web] port and session_minutes must be whole numbers.")

    available = find_config_files(config_folder)
    users = {}
    problems = []
    for section in parser.sections():
        if not section.lower().startswith("user "):
            if section != "web":
                problems.append(f"unknown section [{section}] (users are [user NAME])")
            continue
        name = section[5:].strip()
        sec = parser[section]
        pw_hash = sec.get("password_hash", "").strip()
        if not name:
            problems.append(f"[{section}] has no user name")
            continue
        if not pw_hash or ":" not in pw_hash:
            problems.append(f"[{section}] password_hash is missing or not a hash "
                            f"(make one with python3 app.py --hash-password)")
            continue
        raw = [c.strip() for c in sec.get("configs", "").replace("\n", ",").split(",") if c.strip()]
        if not raw:
            problems.append(f"[{section}] configs is empty - list the .ini files this user may use, or *")
            continue
        if raw == ["*"]:
            configs = list(available)
        else:
            missing = [c for c in raw if c not in available]
            if missing:
                problems.append(f"[{section}] configs not found in {config_folder}: {', '.join(missing)}")
                continue
            configs = raw
        try:
            dry_run_only = sec.getboolean("dry_run_only", fallback=False)
        except ValueError:
            problems.append(f"[{section}] dry_run_only must be true or false")
            continue
        if name.lower() in {u.lower() for u in users}:
            problems.append(f"user {name} is defined twice")
            continue
        users[name] = {"name": name, "password_hash": pw_hash, "configs": configs,
                       "all_configs": raw == ["*"], "dry_run_only": dry_run_only}

    if problems:
        raise WebConfError(f"{path.name} has problems:\n  - " + "\n  - ".join(problems))
    if not users:
        raise WebConfError(f"{path.name} has no [user NAME] sections - add at least one login.")

    return {
        "host": web.get("host", "127.0.0.1").strip() or "127.0.0.1",
        "port": port,
        "session_minutes": session_minutes,
        "users": users,
    }


# --------------------------------------------------------------------------
# Small in-memory helpers
# --------------------------------------------------------------------------

class LoginLimiter:
    """Locks a (client IP, user name) pair out after repeated bad passwords."""

    def __init__(self):
        self._failures = {}
        self._lock = threading.Lock()

    def _recent(self, key, now):
        return [t for t in self._failures.get(key, []) if now - t < LOCKOUT_SECONDS]

    def locked(self, key) -> bool:
        with self._lock:
            return len(self._recent(key, time.time())) >= MAX_FAILED_LOGINS

    def fail(self, key):
        with self._lock:
            now = time.time()
            self._failures[key] = self._recent(key, now) + [now]

    def clear(self, key):
        with self._lock:
            self._failures.pop(key, None)


class OrgNameCache:
    """Org names per config (tokens differ in what they can read), kept for a
    few minutes so each page load doesn't re-fetch every org."""

    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()

    def get(self, config_name: str) -> dict:
        now = time.time()
        with self._lock:
            entry = self._data.get(config_name)
            if not entry or now - entry[0] > ORG_NAME_TTL:
                entry = (now, {})
                self._data[config_name] = entry
            return entry[1]


_activity_logger = logging.getLogger("mist_license_transfer_web.activity")
_activity_logger.propagate = False   # its own file; the journal gets a copy below


def setup_activity_log(path: Path):
    """(Re)point the activity logger at `path`, a size-rotated file."""
    for handler in list(_activity_logger.handlers):
        _activity_logger.removeHandler(handler)
        handler.close()
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=ACTIVITY_LOG_MAX_BYTES, backupCount=ACTIVITY_LOG_BACKUPS, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    _activity_logger.addHandler(handler)
    _activity_logger.setLevel(logging.INFO)


def _clean(value) -> str:
    """One log line per event: no tabs or newlines from user input."""
    return " ".join(str(value).split())


def activity(event: str, user=None, **fields):
    """Writes one tab-separated line to the activity log:
    time, client IP, user, event, then key=value details."""
    if user is None:
        user = session.get("user") or "-"
    parts = [datetime.now().isoformat(timespec="seconds"), f"ip={request.remote_addr}",
             f"user={_clean(user) or '-'}", f"event={event}"]
    parts += [f"{k}={_clean(v)}" for k, v in fields.items() if v is not None and v != ""]
    line = "\t".join(parts)
    try:
        _activity_logger.info(line)
    except Exception as e:  # never let logging break a request
        log.error("Could not write activity log: %s", e)
    log.info("activity: %s", line)


def write_change_log(user, config_name, op, details, outcome):
    line = (f"{datetime.now().isoformat(timespec='seconds')}\tuser={user}\tip={request.remote_addr}\t"
            f"config={config_name}\top={op}\t{details}\tresult={outcome}\n")
    try:
        with open(CHANGE_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        log.error("Could not write change log %s: %s", CHANGE_LOG, e)
    fields = dict(kv.split("=", 1) for kv in details.split("\t") if "=" in kv)
    event = {"amend": "move", "unamend": "return"}.get(op, op)
    activity(event, user=user, config=config_name, **fields, result=outcome)


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------

def create_app(web_conf: dict, force_dry_run: bool = False, config_folder: Path = BASE_DIR,
               activity_log: Path = None) -> Flask:
    setup_activity_log(activity_log or ACTIVITY_LOG)
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(32)  # new each start: restarting signs everyone out
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=web_conf["session_minutes"]),
    )
    users = web_conf["users"]
    limiter = LoginLimiter()
    org_names = OrgNameCache()
    dummy_hash = generate_password_hash(secrets.token_hex(16))

    # -- helpers ----------------------------------------------------------

    def current_user():
        name = session.get("user")
        return users.get(name) if name else None

    def dry_run_state(user):
        """(effective dry-run, can the user switch it off?)"""
        locked = force_dry_run or user["dry_run_only"]
        return locked or session.get("dry_run", False), not locked

    def allowed_configs(user) -> dict:
        available = find_config_files(config_folder)
        names = list(available) if user["all_configs"] else user["configs"]
        return {n: available[n] for n in names if n in available}

    def current_config(user):
        name = session.get("config")
        configs = allowed_configs(user)
        if name not in configs:
            if len(configs) != 1:
                return None
            name = session["config"] = next(iter(configs))
        return core.load_config(configs[name])

    def check_csrf():
        token = request.form.get("csrf", "")
        if not token or not secrets.compare_digest(token, session.get("csrf", "")):
            activity("rejected form", path=request.path, reason="missing or expired form token")
            abort(400, "Form expired - go back, reload the page and try again.")

    def load_state(user, cfg):
        """Everything the main page and the confirm/execute steps need, read
        fresh from Mist (the listing is never trusted from the browser)."""
        api = core.build_session(cfg)
        source_name = core.get_source_org_name(api, cfg)
        names = org_names.get(cfg["name"])
        names[cfg["source_org_id"]] = source_name
        core.resolve_org_names(api, cfg["base_url"], cfg["dest_org_ids"], names)

        dest_id = session.get("dest")
        if dest_id not in cfg["dest_org_ids"]:
            dest_id = cfg["dest_org_ids"][0]
            session["dest"] = dest_id

        data = core.get_licenses(api, cfg)
        licenses = core.collect_valid_licenses(data)
        amendments = core.collect_returnable_amendments(data)
        core.resolve_org_names(api, cfg["base_url"], {a["dst_org_id"] for a in amendments}, names)
        return {
            "api": api,
            "source_name": source_name,
            "dest_id": dest_id,
            "dest_name": names.get(dest_id),
            "destinations": [(d, names.get(d)) for d in cfg["dest_org_ids"]],
            "names": names,
            "licenses_raw": data,
            "licenses": licenses,
            "known_amendments": [a for a in amendments if names.get(a["dst_org_id"])],
            "unknown_amendments": [a for a in amendments if not names.get(a["dst_org_id"])],
        }

    def org_label(names, org_id):
        return f"{names[org_id]} ({org_id})" if names.get(org_id) else org_id

    def find_license(state, sub_id):
        return next((l for l in state["licenses"] if l["subscription_id"] == sub_id), None)

    def find_amendment(state, cfg, amendment_id):
        for a in state["known_amendments"]:
            if a["amendment_id"] == amendment_id:
                return a, True
        for a in state["unknown_amendments"]:
            if a["amendment_id"] == amendment_id:
                if not cfg["allow_unknown_org_returns"]:
                    raise TransferError("That move went to an org this API token can't access, so it can't be returned here.")
                return a, False
        raise TransferError("That move is no longer listed - it may have been returned or expired already. "
                            "The table has been refreshed.")

    def validate_quantity(lic, raw):
        try:
            qty = int(str(raw).strip())
        except ValueError:
            raise TransferError("Enter a whole number to move.")
        if not 1 <= qty <= lic["remaining"]:
            raise TransferError(f"Quantity must be between 1 and {lic['remaining']} (the units still available).")
        return qty

    @app.context_processor
    def inject_globals():
        user = current_user()
        ctx = {"version": __version__, "user": user, "fmt_date": core.fmt_date,
               "csrf": session.get("csrf", "")}
        if user:
            ctx["dry_run"], ctx["dry_run_switchable"] = dry_run_state(user)
            ctx["config_name"] = session.get("config")
            ctx["config_count"] = len(allowed_configs(user))
        return ctx

    @app.before_request
    def require_login():
        if request.endpoint in ("login", "static"):
            return None
        if not current_user():
            if session.get("user"):
                # signed in as a user who has since been removed from web.conf
                activity("signed out", reason="user no longer in web.conf")
            session.clear()
            return redirect(url_for("login"))
        session.permanent = True
        return None

    @app.after_request
    def no_cache(resp):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Frame-Options"] = "DENY"
        return resp

    # -- login ------------------------------------------------------------

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            return render_template("login.html")
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        key = (request.remote_addr, username.lower())
        agent = request.headers.get("User-Agent", "")[:200]
        if limiter.locked(key):
            activity("sign-in blocked", user=username, reason="locked out after repeated wrong passwords")
            flash(f"Too many failed sign-ins - try again in {LOCKOUT_SECONDS // 60} minutes.", "error")
            return render_template("login.html", username=username), 429
        user = next((u for n, u in users.items() if n.lower() == username.lower()), None)
        ok = check_password_hash(user["password_hash"] if user else dummy_hash, password) and user is not None
        if not ok:
            limiter.fail(key)
            activity("sign-in failed", user=username,
                     reason="wrong password" if user else "unknown user name",
                     locked_out="yes" if limiter.locked(key) else None)
            flash("Wrong user name or password.", "error")
            return render_template("login.html", username=username), 401
        limiter.clear(key)
        session.clear()
        session["user"] = user["name"]
        session["csrf"] = secrets.token_urlsafe(32)
        session.permanent = True
        activity("signed in", browser=agent)
        return redirect(url_for("index"))

    @app.post("/logout")
    def logout():
        check_csrf()
        activity("signed out")
        session.clear()
        flash("Signed out.", "info")
        return redirect(url_for("login"))

    # -- config + options -------------------------------------------------

    @app.route("/configs", methods=["GET", "POST"])
    def configs():
        user = current_user()
        available = allowed_configs(user)
        if request.method == "POST":
            check_csrf()
            name = request.form.get("config", "")
            if name not in available:
                activity("config refused", config=name, reason="not assigned to this user")
                abort(403)
            session["config"] = name
            session.pop("dest", None)
            session.pop("pending", None)
            activity("chose config", config=name)
            return redirect(url_for("index"))
        rows = [(n, core.describe_config(p)) for n, p in available.items()]
        return render_template("configs.html", rows=rows)

    @app.post("/destination")
    def destination():
        check_csrf()
        session["dest"] = request.form.get("dest", "")
        session.pop("pending", None)
        activity("chose destination", config=session.get("config"), dst_org_id=session["dest"])
        return redirect(url_for("index"))

    @app.post("/dry-run")
    def toggle_dry_run():
        check_csrf()
        _, switchable = dry_run_state(current_user())
        if switchable:
            session["dry_run"] = request.form.get("on") == "1"
            activity("dry-run on" if session["dry_run"] else "dry-run off (live)")
        return redirect(url_for("index"))

    # -- main page --------------------------------------------------------

    @app.get("/")
    def index():
        user = current_user()
        available = allowed_configs(user)
        if not available:
            return render_template("error.html", message=(
                "None of the config files assigned to you exist in the app folder any more. "
                "Ask the admin to check web.conf.")), 500
        if session.get("config") not in available:
            if len(available) == 1:
                session["config"] = next(iter(available))
            else:
                return redirect(url_for("configs"))
        try:
            cfg = current_config(user)
            state = load_state(user, cfg)
        except TransferError as e:
            activity("error", config=session.get("config"), message=str(e).splitlines()[0])
            return render_template("error.html", message=str(e), config_error=True), 502
        pending = session.pop("pending", None)
        if pending:
            # back on the main page without confirming = the confirmation was cancelled
            activity("move cancelled" if pending["op"] == "amend" else "return cancelled",
                     config=pending.get("config"), subscription_id=pending.get("subscription_id"),
                     amendment_id=pending.get("amendment_id"), quantity=pending.get("quantity"))
        activity("viewed licenses", config=cfg["name"], dst_org_id=state["dest_id"],
                 dry_run="yes" if dry_run_state(user)[0] else "no")
        nothing = not state["licenses"] and not state["known_amendments"] and not state["unknown_amendments"]
        return render_template("index.html", cfg=cfg, s=state, nothing=nothing,
                               no_data=not state["licenses_raw"])

    # -- move -------------------------------------------------------------

    def summary_move(cfg, state, lic, qty):
        return [
            ("Subscription ID", lic["subscription_id"]),
            ("Type", lic["type"]),
            ("Quantity", f"{qty} (of {lic['remaining']} available, {lic['quantity']} total)"),
            ("From", f"{state['source_name']} ({cfg['source_org_id']})"),
            ("To", org_label(state["names"], state["dest_id"])),
        ]

    @app.post("/move/confirm")
    def move_confirm():
        check_csrf()
        user = current_user()
        try:
            cfg = current_config(user)
            if not cfg:
                return redirect(url_for("index"))
            state = load_state(user, cfg)
            lic = find_license(state, request.form.get("subscription_id", ""))
            if not lic:
                raise TransferError("That subscription has no units left to move any more - the table has been refreshed.")
            qty = validate_quantity(lic, request.form.get("quantity", ""))
        except TransferError as e:
            activity("move refused", config=session.get("config"),
                     subscription_id=request.form.get("subscription_id"),
                     quantity=request.form.get("quantity"), reason=str(e).splitlines()[0])
            flash(str(e), "error")
            return redirect(url_for("index"))
        token = secrets.token_urlsafe(16)
        session["pending"] = {"token": token, "op": "amend", "config": cfg["name"],
                              "subscription_id": lic["subscription_id"],
                              "dst_org_id": state["dest_id"], "quantity": qty}
        activity("move confirmation opened", config=cfg["name"], subscription_id=lic["subscription_id"],
                 dst_org_id=state["dest_id"], quantity=qty)
        return render_template("confirm.html", title="Confirm move", token=token,
                               action=url_for("move_execute"), button="Move licenses",
                               rows=summary_move(cfg, state, lic, qty), warnings=[])

    @app.post("/move")
    def move_execute():
        check_csrf()
        user = current_user()
        pending = session.pop("pending", None)
        if not pending or pending.get("op") != "amend" or pending.get("token") != request.form.get("token"):
            activity("move not done", reason="confirmation expired or already used")
            flash("That confirmation has expired or was already used - nothing was changed.", "error")
            return redirect(url_for("index"))
        dry_run, _ = dry_run_state(user)
        details = (f"subscription_id={pending['subscription_id']}\tdst_org_id={pending['dst_org_id']}\t"
                   f"quantity={pending['quantity']}")
        try:
            cfg = current_config(user)
            if not cfg or cfg["name"] != pending["config"]:
                raise TransferError("The config changed since the confirmation page - nothing was changed.")
            state = load_state(user, cfg)
            if pending["dst_org_id"] not in cfg["dest_org_ids"]:
                raise TransferError("That destination is no longer in the config - nothing was changed.")
            lic = find_license(state, pending["subscription_id"])
            if not lic:
                raise TransferError("That subscription has no units left to move any more - nothing was changed.")
            qty = validate_quantity(lic, pending["quantity"])
            dest = org_label(state["names"], pending["dst_org_id"])
            if dry_run:
                write_change_log(user["name"], cfg["name"], "amend", details, "DRY-RUN")
                flash(f"[dry-run] Would have moved {qty} units of {lic['subscription_id']} to {dest}. "
                      f"No changes were made.", "warn")
                return redirect(url_for("index"))
            core.amend(state["api"], cfg, lic["subscription_id"], pending["dst_org_id"], qty)
        except TransferError as e:
            write_change_log(user["name"], pending.get("config"), "amend", details,
                             f"FAILED{' (dry-run)' if dry_run else ''}: {str(e).splitlines()[0]}")
            flash(f"Move failed: {e}", "error")
            return redirect(url_for("index"))
        write_change_log(user["name"], cfg["name"], "amend", details, "OK")
        left = lic["remaining"] - qty
        flash(f"Moved {qty} units of {lic['subscription_id']} from {state['source_name']} to {dest}. "
              + (f"{left} units left in {state['source_name']}." if left else
                 f"All units of this subscription have now been moved out of {state['source_name']}.")
              + f" Please check the subscription appears in {state['names'].get(pending['dst_org_id']) or 'the destination org'}.",
              "success")
        return redirect(url_for("index"))

    # -- return (unamend) -------------------------------------------------

    def summary_return(cfg, state, amendment):
        return [
            ("Amendment ID", amendment["amendment_id"]),
            ("Subscription ID", amendment["subscription_id"]),
            ("Type", amendment["type"]),
            ("Quantity", f"{abs(amendment['quantity'])} returned"),
            ("From", org_label(state["names"], amendment["dst_org_id"])),
            ("Back to", f"{state['source_name']} ({cfg['source_org_id']})"),
        ]

    @app.post("/return/confirm")
    def return_confirm():
        check_csrf()
        user = current_user()
        try:
            cfg = current_config(user)
            if not cfg:
                return redirect(url_for("index"))
            state = load_state(user, cfg)
            amendment, known = find_amendment(state, cfg, request.form.get("amendment_id", ""))
        except TransferError as e:
            activity("return refused", config=session.get("config"),
                     amendment_id=request.form.get("amendment_id"), reason=str(e).splitlines()[0])
            flash(str(e), "error")
            return redirect(url_for("index"))
        token = secrets.token_urlsafe(16)
        session["pending"] = {"token": token, "op": "unamend", "config": cfg["name"],
                              "amendment_id": amendment["amendment_id"]}
        activity("return confirmation opened", config=cfg["name"], amendment_id=amendment["amendment_id"],
                 subscription_id=amendment["subscription_id"], from_org_id=amendment["dst_org_id"],
                 quantity=abs(amendment["quantity"]))
        warnings = [] if known else [
            "This API token can't access the destination org, so its name and current usage "
            "of these licenses can't be checked."]
        return render_template("confirm.html", title="Confirm return (unamend)", token=token,
                               action=url_for("return_execute"), button="Return licenses",
                               rows=summary_return(cfg, state, amendment), warnings=warnings)

    @app.post("/return")
    def return_execute():
        check_csrf()
        user = current_user()
        pending = session.pop("pending", None)
        if not pending or pending.get("op") != "unamend" or pending.get("token") != request.form.get("token"):
            activity("return not done", reason="confirmation expired or already used")
            flash("That confirmation has expired or was already used - nothing was changed.", "error")
            return redirect(url_for("index"))
        dry_run, _ = dry_run_state(user)
        details = f"amendment_id={pending['amendment_id']}"
        try:
            cfg = current_config(user)
            if not cfg or cfg["name"] != pending["config"]:
                raise TransferError("The config changed since the confirmation page - nothing was changed.")
            state = load_state(user, cfg)
            amendment, _ = find_amendment(state, cfg, pending["amendment_id"])
            details += f"\tsubscription_id={amendment['subscription_id']}\tfrom_org_id={amendment['dst_org_id']}" \
                       f"\tquantity={abs(amendment['quantity'])}"
            src = org_label(state["names"], amendment["dst_org_id"])
            if dry_run:
                write_change_log(user["name"], cfg["name"], "unamend", details, "DRY-RUN")
                flash(f"[dry-run] Would have returned {abs(amendment['quantity'])} units of "
                      f"{amendment['subscription_id']} from {src}. No changes were made.", "warn")
                return redirect(url_for("index"))
            core.unamend(state["api"], cfg, amendment["amendment_id"])
        except TransferError as e:
            write_change_log(user["name"], pending.get("config"), "unamend", details,
                             f"FAILED{' (dry-run)' if dry_run else ''}: {str(e).splitlines()[0]}")
            flash(f"Return failed: {e}", "error")
            return redirect(url_for("index"))
        write_change_log(user["name"], cfg["name"], "unamend", details, "OK")
        flash(f"Returned {abs(amendment['quantity'])} units of {amendment['subscription_id']} from {src} "
              f"back to {state['source_name']}. Please check the subscription is back in {state['source_name']}.",
              "success")
        return redirect(url_for("index"))

    return app


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def hash_password_cli():
    if not sys.stdin.isatty():
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("New password: ")
        if password != getpass.getpass("Repeat password: "):
            sys.exit("Passwords don't match.")
    if len(password) < 8:
        sys.exit("Use at least 8 characters.")
    print(f"password_hash = {generate_password_hash(password)}")


def main():
    parser = argparse.ArgumentParser(description="Mist License Transfer web app.")
    parser.add_argument("--web-conf", type=Path, default=DEFAULT_WEB_CONF,
                        help=f"users/settings file (default {DEFAULT_WEB_CONF.name} in the app folder)")
    parser.add_argument("--dry-run", action="store_true",
                        help="never call the Mist API to make changes, whatever users choose")
    parser.add_argument("--hash-password", action="store_true",
                        help="prompt for a password and print the password_hash line for web.conf")
    parser.add_argument("--host", help="override [web] host")
    parser.add_argument("--port", type=int, help="override [web] port")
    args = parser.parse_args()

    if args.hash_password:
        hash_password_cli()
        return

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        web_conf = load_web_conf(args.web_conf)
    except WebConfError as e:
        sys.exit(f"Error: {e}")
    host = args.host or web_conf["host"]
    port = args.port or web_conf["port"]

    print(f"Mist License Transfer web {__version__}: http://{host}:{port}  "
          f"({len(web_conf['users'])} user(s), configs in {BASE_DIR})")
    if args.dry_run:
        print("Running with --dry-run: no changes will be made, whatever users choose.")
    create_app(web_conf, force_dry_run=args.dry_run).run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
