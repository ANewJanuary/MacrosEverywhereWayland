#!/usr/bin/env python3

import json
import os
import select
import shutil
import subprocess
import sys
import time
import tomllib
import traceback
from pathlib import Path

import evdev
from evdev import ecodes, UInput

SCRIPT_DIR = Path(__file__).resolve().parent


# print errors to clipboard
def notify(msg):
    """Surface a message to the user. Clipboard is the only reliable channel
    since this runs headless from a keybind."""
    if shutil.which("wl-copy"):
        subprocess.run(["wl-copy"], input=msg, text=True, check=False)


def load_config():
    """
    Look for a config file, first one found wins:
      1. $XDG_CONFIG_HOME/macros/config.toml (~/.config/macros/config.toml
         if XDG_CONFIG_HOME is unset)
      2. macros.toml next to this script
    Missing file or parse error leads to {} (all settings fall back to defaults).
    """
    xdg_config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    for candidate in (xdg_config_home / "macros" / "config.toml", SCRIPT_DIR / "macros.toml"):
        if candidate.is_file():
            try:
                return tomllib.loads(candidate.read_text())
            except Exception:
                notify(f"macro-pick: failed to parse config {candidate}")
                return {}
    return {}


_config = load_config()

MACRO_DIR = Path(os.environ.get(
    "MACRO_DIR", _config.get("macro_dir", str(Path.home() / "Vshrd/macros"))
)).expanduser()

# location of file tracking usage for recents
USAGE_FILE = Path(
    _config.get("usage_file", str(Path.home() / "Vshrd" / "shell-scripts" / "macro-usage.json"))
).expanduser()

SELECT_TIMEOUT = float(_config.get("select_timeout", 10.0))

# delay (seconds) used at each settle point: after creating the virtual
# keyboard, before grabbing devices; before typing the snippet; before
# starting tab-stop navigation.
SLEEP = float(_config.get("sleep", 0.1))


# load json for tracking usage
def load_usage():
    """Return {filename: {"count": int, "last": float}}. Missing/corrupt leads to {}."""
    try:
        return json.loads(USAGE_FILE.read_text())
    except Exception:
        return {}


# track usage for recents
def record_usage(name):
    """Increment the pick count for `name` and stamp the time."""
    usage = load_usage()
    entry = usage.get(name, {"count": 0, "last": 0.0})
    entry["count"] = entry.get("count", 0) + 1
    entry["last"] = time.time()
    usage[name] = entry
    try:
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        USAGE_FILE.write_text(json.dumps(usage))
    except Exception:
        pass


# sort
def order_by_usage(files):
    """
    Sort filenames most-used first, ties broken by most-recent. Snippets never
    picked keep their alphabetical order after the ranked ones.
    """
    usage = load_usage()
    ranked = [f for f in files if f in usage]
    unranked = [f for f in files if f not in usage]
    ranked.sort(
        key=lambda f: (usage[f].get("count", 0), usage[f].get("last", 0.0)),
        reverse=True,
    )
    return ranked + unranked

# prompt function
def fuzzel_pick(items, prompt="snippet> ", width=40, lines=15):
    result = subprocess.run(
        ["fuzzel", "--dmenu", "--prompt", prompt, f"--width={width}", f"--lines={lines}"],
        input="\n".join(items), capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


# check variables

class AskCancelled(Exception):
    """Raised when the user dismisses a zenity prompt; aborts the snippet."""

# ask for variable
def ask_value(prompt):
    if not shutil.which("zenity"):
        notify("macro-pick: missing required tool 'zenity' (needed for $ASK/$VAR)")
        raise AskCancelled()
    result = subprocess.run(
        ["zenity", "--entry", "--title", "macro-pick", "--text", prompt],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AskCancelled()
    return result.stdout.strip()


# read the things between variable prompts
def parse_args(content, open_paren):
    """
    content[open_paren] must be '('. Reads comma-separated, single-quoted
    string arguments up to the matching ')'. \\' inside an argument is a
    literal quote, not the end of the argument. Returns (list_of_args,
    index_after_closing_paren).
    """
    assert content[open_paren] == '('
    args = []
    i = open_paren + 1
    n = len(content)
    while True:
        while content[i] in ' \t':
            i += 1
        assert content[i] == "'", f"expected a quoted argument at {i}"
        i += 1
        chars = []
        while True:
            c = content[i]
            if c == '\\' and i + 1 < n and content[i + 1] == "'":
                chars.append("'")
                i += 2
            elif c == "'":
                i += 1
                break
            else:
                chars.append(c)
                i += 1
        args.append(''.join(chars))
        while content[i] in ' \t':
            i += 1
        if content[i] == ',':
            i += 1
            continue
        assert content[i] == ')', f"expected ',' or ')' at {i}"
        return args, i + 1


# find the end of a block
def find_block_end(content, start):
    """
    Find the $DONE matching the $REPEAT(...) whose body starts at `start`.
    Tracks nesting by counting further '$REPEAT(' openers against '$DONE'
    closers. Returns the index of the matching '$DONE'.
    """
    depth = 1
    i = start
    while True:
        next_open = content.find('$REPEAT(', i)
        next_close = content.find('$DONE', i)
        if next_close == -1:
            raise ValueError("unterminated $REPEAT (missing $DONE)")
        if next_open != -1 and next_open < next_close:
            depth += 1
            i = next_open + len('$REPEAT(')
        else:
            depth -= 1
            if depth == 0:
                return next_close
            i = next_close + len('$DONE')

# follow the logic of putting the variables in
def expand_template(content, values, index=None):
    """
    Resolve $ASK/$VAR/$USE/$REPEAT.../$DONE/$I in `content`. `values` is a
    dict of name -> resolved string, shared and mutated across the whole
    expansion so each name is only asked once. `index` is the current
    1-based $REPEAT iteration, or None outside any $REPEAT.
    """
    out = []
    i = 0
    n = len(content)
    while i < n:
        if content[i] != '$':
            j = content.find('$', i)
            if j == -1:
                out.append(content[i:])
                break
            out.append(content[i:j])
            i = j

        if content.startswith('$ASK(', i):
            (name, prompt), after = parse_args(content, i + len('$ASK'))
            if name not in values:
                values[name] = ask_value(prompt)
            out.append(values[name])
            i = after
        elif content.startswith('$VAR(', i):
            (name, prompt), after = parse_args(content, i + len('$VAR'))
            if name not in values:
                values[name] = ask_value(prompt)
            i = after
        elif content.startswith('$USE(', i):
            (name,), after = parse_args(content, i + len('$USE'))
            out.append(values.get(name, ''))
            i = after
        elif content.startswith('$REPEAT(', i):
            (name, sep), after_header = parse_args(content, i + len('$REPEAT'))
            done_pos = find_block_end(content, after_header)
            body = content[after_header:done_pos]
            count = int(values.get(name, '0') or 0)
            parts = [expand_template(body, values, idx) for idx in range(1, count + 1)]
            out.append(sep.join(parts))
            i = done_pos + len('$DONE')
        elif content[i:i + 2] == '$I' and index is not None and (
            i + 2 >= n or not content[i + 2].isalnum()
        ):
            out.append(str(index))
            i += 2
        else:
            out.append('$')
            i += 1

    return ''.join(out)


# tab stops
def parse_tab_stops(content):
    """
    Strip $TAB / $TAB('default') markers.
    Returns (clean_content, [(line, col, line_length), ...]) where (line, col)
    is the stop's position in clean_content and line_length is the full
    length of that line in clean_content to be used as an End-anchored
    navigation reference (see move_multiline) rather than a Home-anchored
    one.
    """
    clean = ""
    raw_stops = []
    last_end = 0
    n = len(content)
    i = 0

    while True:
        idx = content.find('$TAB', i)
        if idx == -1:
            break
        after = idx + len('$TAB')
        if after < n and content[after] == '(':
            (default,), end = parse_args(content, after)
        elif after < n and content[after].isalnum():
            # Not a real $TAB token (e.g. part of $TABLE) so keep scanning.
            i = after
            continue
        else:
            default, end = '', after

        clean += content[last_end:idx] + default
        last_end = end
        lines_so_far = clean.split("\n")
        raw_stops.append((len(lines_so_far) - 1, len(lines_so_far[-1])))
        i = end

    clean += content[last_end:]
    lines = clean.split("\n")
    stops = [(line, col, len(lines[line])) for line, col in raw_stops]
    return clean, stops


def end_line(content):
    return content.count("\n")


# grab keyboard
def emit_key(ui, code, count=1):
    for _ in range(count):
        ui.write(ecodes.EV_KEY, code, 1)
        ui.syn()
        ui.write(ecodes.EV_KEY, code, 0)
        ui.syn()

# move the cursor when you need to
def move_multiline(ui, from_line, to_line, to_col, line_length):
    """
    Multi-line navigation: Up/Down to the target line, then End followed by
    Left * (line_length - to_col).

    Anchoring from End rather than Home avoids two pitfalls of a Home
    anchor: editors with "smart Home" (jumps to the first non-blank
    character instead of column 0, e.g. VS Code) land in the wrong place,
    and any extra leading whitespace an editor's autoindent injects on a
    freshly-typed line shifts a Home-anchored column but leaves an
    End-anchored one correct, since the injected text is always to the
    left of every same-line target and so doesn't change the distance from
    that target to the end of the line.
    """
    line_delta = to_line - from_line
    if line_delta < 0:
        emit_key(ui, ecodes.KEY_UP, abs(line_delta))
    elif line_delta > 0:
        emit_key(ui, ecodes.KEY_DOWN, line_delta)

    emit_key(ui, ecodes.KEY_END)
    left_count = line_length - to_col
    if left_count > 0:
        emit_key(ui, ecodes.KEY_LEFT, left_count)

    return to_line


def move_horizontal(ui, from_col, to_col):
    """
    Single-line navigation: pure Left/Right by column delta. No Home, no
    vertical movement so it works inline anywhere because there are no newlines to
    trigger autoindent and no line-0 prefix ambiguity. Returns the new column.
    """
    delta = to_col - from_col
    if delta < 0:
        emit_key(ui, ecodes.KEY_LEFT, abs(delta))
    elif delta > 0:
        emit_key(ui, ecodes.KEY_RIGHT, delta)
    return to_col



# evdev keycodes that insert exactly one character on the current line.
PRINTABLE_KEYS = set()
for _c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
    PRINTABLE_KEYS.add(getattr(ecodes, f'KEY_{_c}'))
for _k in ('KEY_0', 'KEY_1', 'KEY_2', 'KEY_3', 'KEY_4', 'KEY_5', 'KEY_6',
           'KEY_7', 'KEY_8', 'KEY_9', 'KEY_SPACE', 'KEY_MINUS', 'KEY_EQUAL',
           'KEY_LEFTBRACE', 'KEY_RIGHTBRACE', 'KEY_SEMICOLON', 'KEY_APOSTROPHE',
           'KEY_COMMA', 'KEY_DOT', 'KEY_SLASH', 'KEY_BACKSLASH', 'KEY_GRAVE'):
    PRINTABLE_KEYS.add(getattr(ecodes, _k))

# track movements
def track_col(cur_col, code):
    """Update current column based on a forwarded keypress (single-line mode)."""
    if code in PRINTABLE_KEYS:
        return cur_col + 1
    elif code == ecodes.KEY_BACKSPACE:
        return max(0, cur_col - 1)
    elif code == ecodes.KEY_LEFT:
        return max(0, cur_col - 1)
    elif code == ecodes.KEY_RIGHT:
        return cur_col + 1
    return cur_col


# grab input
def find_keyboards():
    keyboards = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
            caps = dev.capabilities()
            if ecodes.EV_KEY in caps and ecodes.KEY_TAB in caps[ecodes.EV_KEY]:
                keyboards.append(dev)
        except Exception:
            pass
    return keyboards


def create_passthrough(keyboards):
    """Virtual device that forwards only real keyboard keys (code < 256),
    so it can never generate mouse motion or buttons."""
    all_keys = set()
    for dev in keyboards:
        caps = dev.capabilities()
        if ecodes.EV_KEY in caps:
            all_keys.update(k for k in caps[ecodes.EV_KEY] if k < 256)
    all_keys.discard(ecodes.KEY_TAB)
    return UInput({ecodes.EV_KEY: list(all_keys)}, name="macro-pick-passthrough")


def run_tab_cycling(clean_content, stops):
    multiline = "\n" in clean_content

    remaining = list(stops)

    # Stored stops are in original-content coordinates. As the user types at a
    # stop, every later stop shifts. We correct for this:
    #
    # single-line: cur = current column. col_offset = net chars inserted so far,
    #              added to every later stop's column.
    #
    # multi-line:  cur = current (real) line. line_offset = net newlines
    #              inserted so far, added to every later stop's line number
    #              to get its real line. Horizontal position is End-anchored
    #              (see move_multiline), which needs no equivalent column
    #              offset because inserted characters shift a same-line target and
    #              the line's end by the same amount, so the distance
    #              between them, which is all an End anchor cares about,
    #              stays constant.
    if multiline:
        cur = end_line(clean_content)   # current line
    else:
        cur = len(clean_content)        # current column

    col_offset = 0   # single-line
    line_offset = 0  # multi-line: extra lines inserted

    keyboards = find_keyboards()
    if not keyboards:
        notify("macro-pick: no keyboards found")
        return

    ui = create_passthrough(keyboards)
    time.sleep(SLEEP)

    grabbed = []
    for dev in keyboards:
        try:
            dev.grab()
            grabbed.append(dev)
        except Exception:
            pass

    if not grabbed:
        notify("macro-pick: could not grab any keyboard")
        ui.close()
        return

    def goto(stop):
        nonlocal cur
        line, col, line_length = stop
        if multiline:
            cur = move_multiline(ui, cur, line + line_offset, col, line_length)
        else:
            cur = move_horizontal(ui, cur, col + col_offset)

    # Move to the first stop
    goto(remaining.pop(0))

    try:
        fds = {dev.fd: dev for dev in grabbed}

        while True:
            readable, _, _ = select.select(fds.keys(), [], [], SELECT_TIMEOUT)
            if not readable:
                break  # inactivity timeout, release everything

            for fd in readable:
                for event in fds[fd].read():
                    if event.type != ecodes.EV_KEY:
                        continue  # drop non-key events (no mouse passthrough)

                    if event.code == ecodes.KEY_TAB and event.value == 1:
                        if remaining:
                            goto(remaining.pop(0))
                        else:
                            return  # last stop consumed

                    elif event.code == ecodes.KEY_ESC and event.value == 1:
                        # Forward Escape so the editor reacts, then exit
                        emit_key(ui, ecodes.KEY_ESC)
                        return

                    else:
                        ui.write(ecodes.EV_KEY, event.code, event.value)
                        ui.syn()
                        if event.value != 1:
                            continue

                        if multiline:
                            if event.code in (ecodes.KEY_ENTER, ecodes.KEY_KPENTER):
                                # New line inserted: later stops move down.
                                line_offset += 1
                                cur += 1
                        else:
                            prev = cur
                            cur = track_col(cur, event.code)
                            col_offset += cur - prev

    finally:
        for dev in grabbed:
            try:
                dev.ungrab()
            except Exception:
                pass
        ui.close()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    try:
        for tool in ("fuzzel", "wtype"):
            if not shutil.which(tool):
                notify(f"macro-pick: missing required tool '{tool}'")
                sys.exit(1)

        if not MACRO_DIR.is_dir():
            notify(f"macro-pick: macro dir not found: {MACRO_DIR}")
            sys.exit(1)

        files = sorted(
            p.name for p in MACRO_DIR.iterdir()
            if p.is_file() and not p.name.startswith(".")
        )
        if not files:
            notify("macro-pick: no snippets found")
            sys.exit(1)

        # Most-used snippets first, then the rest alphabetically.
        chosen = fuzzel_pick(order_by_usage(files))
        if not chosen:
            sys.exit(0)

        snippet_path = MACRO_DIR / chosen
        if not snippet_path.is_file():
            sys.exit(0)

        record_usage(chosen)

        # Strip one trailing newline which is almost always the file's final newline,
        # not part of the snippet. Without this a single-line snippet would be
        # misread as multi-line and trigger autoindent navigation.
        raw = snippet_path.read_text()
        if raw.endswith("\n"):
            raw = raw[:-1]

        try:
            raw = expand_template(raw, {})
        except AskCancelled:
            sys.exit(0)

        clean_content, stops = parse_tab_stops(raw)

        time.sleep(SLEEP)
        subprocess.run(["wtype", "-"], input=clean_content, text=True, check=False)

        if stops:
            time.sleep(SLEEP)
            run_tab_cycling(clean_content, stops)

    except Exception:
        notify("macro-pick crashed:\n" + traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
