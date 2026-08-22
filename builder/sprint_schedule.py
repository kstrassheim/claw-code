"""When a sprint ends and the next begins, without anybody asking.

THE DEFAULT IS AUTONOMOUS
One-week sprints, rolling over every Saturday at 13:00 Europe/Zurich, numbers
incrementing. Nothing has to be started or stopped by hand for the bot to
work; manual start/stop is an override, not a prerequisite.

WHY THIS IS A CATCH-UP CHECK AND NOT A CRON AT 13:00
A job that fires exactly at Saturday 13:00 misses the rollover COMPLETELY if
the pod happens to be restarting in that minute — and nothing would say so.
The sprint would simply never end, the next would never begin, and the first
symptom would be numbers that quietly stopped making sense.

So instead: every tick asks "has a boundary passed since the last rollover?".
Late is fine and self-correcting; missed is not. If the pod was down for two
days the next tick closes the overdue sprint and opens the current one, and
says that it was late.

TIMEZONE IS EXPLICIT
"Saturday 13:00" is meaningless without one, and the cluster runs UTC. Getting
this wrong shifts every boundary by an hour twice a year, which is exactly the
kind of thing nobody notices until a sprint report straddles two sprints.
"""

from __future__ import annotations

import datetime as dt
import os

# Saturday. Python's Monday=0 convention, spelled out because "5" in a config
# file is unreadable and the off-by-one is easy.
DEFAULT_WEEKDAY = 5          # Saturday
DEFAULT_HOUR = 13
DEFAULT_MINUTE = 0
DEFAULT_TZ = "Europe/Zurich"
DEFAULT_LENGTH_DAYS = 7

CONF_PATH = os.environ.get(
    "SPRINT_SCHEDULE_CONF",
    os.path.expanduser("~/.openclaw/sprint-schedule.conf"))

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")


def _tz(name: str):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        # A missing tzdata must not stop sprints rolling over — UTC shifts the
        # boundary by an hour or two, which is far better than not rolling at
        # all. But it must not be SILENT either: a boundary quietly two hours
        # early every week is the kind of thing that is only ever noticed as
        # "the numbers stopped adding up". `resolved_timezone` exists so the
        # fallback is reportable, and `describe()` says so out loud.
        return dt.timezone.utc


def timezone_available(name: str) -> bool:
    """Whether the configured timezone could actually be loaded."""
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
        return True
    except Exception:
        return False


def load_config() -> dict:
    """Schedule settings, with the defaults applied.

    Deliberately the same shape as the other runtime settings: a plain file on
    the workspace volume, read fresh, falling back on anything unparseable.
    """
    cfg = {
        "enabled": True,
        "weekday": DEFAULT_WEEKDAY,
        "hour": DEFAULT_HOUR,
        "minute": DEFAULT_MINUTE,
        "timezone": DEFAULT_TZ,
        "lengthDays": DEFAULT_LENGTH_DAYS,
    }
    try:
        with open(CONF_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if not line or "=" not in line:
                    continue
                k, v = (p.strip() for p in line.split("=", 1))
                k = k.lower()
                if k == "enabled":
                    cfg["enabled"] = v.lower() in ("1", "true", "on", "yes")
                elif k == "weekday":
                    if v.lower() in _WEEKDAYS:
                        cfg["weekday"] = _WEEKDAYS.index(v.lower())
                    elif v.isdigit() and 0 <= int(v) <= 6:
                        cfg["weekday"] = int(v)
                elif k == "hour" and v.isdigit() and 0 <= int(v) <= 23:
                    cfg["hour"] = int(v)
                elif k == "minute" and v.isdigit() and 0 <= int(v) <= 59:
                    cfg["minute"] = int(v)
                elif k == "timezone" and v:
                    cfg["timezone"] = v
                elif k == "lengthdays" and v.isdigit() and int(v) > 0:
                    cfg["lengthDays"] = int(v)
    except OSError:
        pass
    return cfg


def boundary_on_or_before(now: dt.datetime, cfg: dict) -> dt.datetime:
    """The most recent scheduled boundary at or before `now`.

    This is the anchor everything else derives from: a sprint that started at
    a boundary, and the next boundary a week later.
    """
    tz = _tz(cfg["timezone"])
    local = now.astimezone(tz)
    # Step back to the configured weekday, then to the configured time.
    delta = (local.weekday() - cfg["weekday"]) % 7
    candidate = (local - dt.timedelta(days=delta)).replace(
        hour=cfg["hour"], minute=cfg["minute"], second=0, microsecond=0)
    if candidate > local:
        candidate -= dt.timedelta(days=7)
    return candidate


def next_boundary(now: dt.datetime, cfg: dict) -> dt.datetime:
    return boundary_on_or_before(now, cfg) + dt.timedelta(days=cfg["lengthDays"])


def rollover_due(active_started_at: str | None, now: dt.datetime,
                 cfg: dict) -> tuple[bool, str]:
    """Should a rollover happen right now? Returns (due, why).

    `active_started_at` is the running sprint's start, or None when no sprint
    is running at all — which is also a reason to roll: the very first tick
    after this feature ships must open sprint 1 without being asked.
    """
    if not cfg.get("enabled", True):
        return (False, "automatic sprints are switched off")
    boundary = boundary_on_or_before(now, cfg)
    if not active_started_at:
        return (True, "no sprint is running")
    try:
        started = dt.datetime.fromisoformat(
            active_started_at.replace("Z", "+00:00"))
    except ValueError:
        # An unreadable start is not a reason to roll over — that would open a
        # new sprint on every tick. Say nothing and let a human look.
        return (False, f"cannot read the active sprint's start "
                       f"({active_started_at!r})")
    if started.tzinfo is None:
        started = started.replace(tzinfo=dt.timezone.utc)
    if started < boundary:
        late = now.astimezone(_tz(cfg["timezone"])) - boundary
        hours = late.total_seconds() / 3600
        when = ("on time" if hours < 1
                else f"{hours:.0f}h late — the boundary was "
                     f"{boundary.isoformat()}")
        return (True, f"the sprint started before the last boundary ({when})")
    return (False, "the running sprint is current")


def sprint_window(now: dt.datetime, cfg: dict) -> tuple[str, str]:
    """(startedAt, endsAt) for the sprint that should be running now."""
    start = boundary_on_or_before(now, cfg)
    end = start + dt.timedelta(days=cfg["lengthDays"])
    return (start.isoformat(), end.isoformat())


_DAY_WORDS = {
    "mo": 0, "mon": 0, "monday": 0, "montag": 0,
    "tu": 1, "tue": 1, "tues": 1, "tuesday": 1, "di": 1, "dienstag": 1,
    "we": 2, "wed": 2, "wednesday": 2, "mi": 2, "mittwoch": 2,
    "th": 3, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "do": 3, "donnerstag": 3,
    "fr": 4, "fri": 4, "friday": 4, "freitag": 4,
    "sa": 5, "sat": 5, "saturday": 5, "samstag": 5, "sonnabend": 5,
    "su": 6, "sun": 6, "sunday": 6, "so": 6, "sonntag": 6,
}


def parse_spec(text: str) -> tuple[int, int, int]:
    """"Sat 13:00", "Mo 9h", "Tuesday 9am", "samstag 13" -> (weekday, h, m).

    People say the boundary out loud, in whatever form comes to mind, and in
    two languages here. Refusing anything but one canonical spelling would
    just move the work onto the person asking.

    Raises ValueError with a usable message — the caller reports it rather
    than guessing, because a MISREAD schedule is worse than a rejected one:
    it silently moves every sprint boundary.
    """
    import re
    raw = (text or "").strip().lower().replace(",", " ")
    if not raw:
        raise ValueError("no schedule given")
    parts = raw.split()

    day = None
    for p in parts:
        word = re.sub(r"[^a-zä]", "", p)
        if word in _DAY_WORDS:
            day = _DAY_WORDS[word]
            break
    if day is None:
        raise ValueError(
            f"could not find a weekday in {text!r} — say e.g. 'Sat 13:00', "
            "'Mo 9h' or 'Tuesday 9am'")

    hour = minute = None
    for p in parts:
        m = re.fullmatch(r"(\d{1,2})[:.h]?(\d{2})?\s*(am|pm)?", p)
        if not m or (m.group(1) is None):
            continue
        # A bare weekday number would match here; skip the token we read as
        # the day.
        if re.sub(r"[^a-zä]", "", p) in _DAY_WORDS:
            continue
        h = int(m.group(1))
        mi = int(m.group(2) or 0)
        ampm = m.group(3)
        if ampm == "pm" and h < 12:
            h += 12
        if ampm == "am" and h == 12:
            h = 0
        if 0 <= h <= 23 and 0 <= mi <= 59:
            hour, minute = h, mi
            break
    if hour is None:
        raise ValueError(
            f"could not find a time in {text!r} — say e.g. '13:00', '9h' or "
            "'9am'")
    return (day, hour, minute)


def save_config(cfg: dict) -> None:
    """Persist the schedule. Written atomically: a tick may read it at any
    moment, and half a schedule is worse than an old one."""
    path = CONF_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("# When sprints roll over. Managed from chat.\n")
        f.write("# The running sprint's end is recomputed when this changes.\n")
        f.write(f"enabled = {'true' if cfg.get('enabled', True) else 'false'}\n")
        f.write(f"weekday = {_WEEKDAYS[cfg['weekday']]}\n")
        f.write(f"hour = {cfg['hour']}\n")
        f.write(f"minute = {cfg['minute']}\n")
        f.write(f"timezone = {cfg['timezone']}\n")
        f.write(f"lengthDays = {cfg['lengthDays']}\n")
    os.replace(tmp, path)


def reschedule_active(started_at: str, now: dt.datetime, cfg: dict) -> dict:
    """What changing the schedule does to the sprint that is already running.

    A schedule change is NOT only about future sprints. Moving the boundary
    earlier has to end the running sprint earlier, and moving it later has to
    extend it — otherwise the change appears to do nothing for a week and the
    stored end date quietly contradicts the schedule.

    The new end is the first scheduled boundary strictly after NOW — not after
    the sprint began, which was the first thing I wrote and is wrong. Moving
    Saturday to Sunday would then have ended a sprint that started on Saturday
    the very next day, i.e. after twenty hours, while the operator had asked
    for it to run LATER.

    Anchoring on now also disposes of the other surprise: setting the boundary
    to a weekday that has already passed this week does not retroactively end
    the sprint mid-week. It runs to that weekday's next occurrence, which is
    what anyone would expect from "sprints now end on Mondays".

    Returns {endsAt, startedAt, overdue}. `overdue` should not occur by
    construction and is reported rather than assumed away — a clock that has
    gone backwards is exactly the situation where silent assumptions hurt.
    """
    tz = _tz(cfg["timezone"])
    try:
        started = dt.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return {}
    if started.tzinfo is None:
        started = started.replace(tzinfo=dt.timezone.utc)

    anchor = max(started, now).astimezone(tz)
    delta = (cfg["weekday"] - anchor.weekday()) % 7
    end = (anchor + dt.timedelta(days=delta)).replace(
        hour=cfg["hour"], minute=cfg["minute"], second=0, microsecond=0)
    if end <= anchor:
        end += dt.timedelta(days=7)

    return {
        "endsAt": end.isoformat(),
        "overdue": end <= now.astimezone(tz),
        "startedAt": started.isoformat(),
    }


def describe(cfg: dict) -> str:
    day = _WEEKDAYS[cfg["weekday"]].capitalize()
    state = "on" if cfg.get("enabled", True) else "OFF"
    text = (f"Automatic sprints: {state} — every {day} at "
            f"{cfg['hour']:02d}:{cfg['minute']:02d} {cfg['timezone']}, "
            f"{cfg['lengthDays']}-day sprints")
    if not timezone_available(cfg["timezone"]):
        text += (f"\n  WARNING: {cfg['timezone']} could not be loaded (no "
                 "tzdata here), so boundaries are being computed in UTC. "
                 "Every sprint starts and ends one or two hours off.")
    return text
