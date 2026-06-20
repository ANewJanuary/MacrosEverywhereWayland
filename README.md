# Dependancies

Linux and Wayland only.

Everything is a hard dependency.

```
pip install evdev --user
```

Oder applications:
```
fuzzel
wtype
wl-clipboard
zenity
```

```
sudo dnf install fuzzel wtype wl-clipboard zenity
```


# Snippet Format

This is a simple python script for Linux on WAYLAND ONLY that scans a directory for simple text files, prompts the user to select from the available files, and pastes the content where the cursor is.

Additionally, in said files, you may use the following syntax for additional behavior:


`$TAB`: tab stop, snippet like. Pressing tab on the keyboard jumps the cursor to tab. Note that the script has to grab all input devices so that it can count the cursor position and jump to the correct place.

`$TAB('text')`: tab stop with default text; cursor lands after it.

Variables:

(Use \\' for literal quotes in inputs)

`$VAR('name', 'prompt')`: prompt for a value and store it
`$ASK('name', 'prompt')`: same as var but paste directly
`$USE('name')`: insert a saved variable

loops:

`$REPEAT('name', 'sep')`
body
`$DONE`

Repeat the body `name` times, joined by `sep`. `body` may use $I for the current 1-based iteration index, and may itself contain further $ASK/$VAR/$USE/

Example for a snippet of a table in typst:

```
#figure(
    caption: [$TAB('my table')],
    kind: table,
  table(columns:$ASK('cols', 'Number of columns:'), stroke: 1pt,$VAR('rows', 'Number of rows:')
    $REPEAT('cols', ', ')[*$TAB('column $I')*]$DONE,
    $REPEAT('rows', ',
    ')$REPEAT('cols', ', ')[$TAB]$DONE$DONE
  )
) <table>
```

Note: comments are not supported inside snippet files. Why should snippets be multiple files? Idk. It was easier for me.

A name is only ever asked once per snippet expansion, even if $ASK or $VAR for it appears more than once. Use different names for variables. Otherwise, it won't ask twice.

# Config options

location can be:
```
$XDG_CONFIG_HOME/macros/config.toml 
~/.config/macros/config.toml
MACRO_DIR env var
```

Directory containing snippet files (one snippet per file).
```
macro_dir = "~/Snippets/"
```

Where pick-frequency stats (for sorting snippets by recency/usage) are stored.
```
usage_file = "~/Snippets/macro-usage.json"
```
Seconds of inactivity before a tab-stop cycling session gives up and releases the keyboard grab.
```
select_timeout = 10.0
```
Seconds to pause at each settle point (after creating the virtual keyboard/before grabbing devices, before typing the snippet, before starting tab-stop navigation). Make it at least 0.2 or 0.25, because otherwise the arrow keys do not work.
```
sleep = 0.2
```
