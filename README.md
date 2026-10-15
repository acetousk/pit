# pit

Persistent Pi sessions with a tmux sidebar.

## Requirements

- Python 3 with curses
- tmux
- Pi (`pi` on your PATH)
- Optional: `wl-clipboard` for Wayland clipboard integration; `xdg-open` for links

## Setup

```sh
git clone https://github.com/acetousk/pit.git
cd pit
mkdir -p ~/.pi/agent/extensions
cp pit-sync.ts ~/.pi/agent/extensions/
```

Start from your project directory:

```sh
/path/to/pit/pit.sh
```

Restart existing Pi processes to load the session-sync extension.

## Controls

In the sidebar: `n` new, `Enter` open, `a` done, `x` close process,
`h` hide sidebar, `q` detach. Use arrow keys or `j`/`k` to navigate.
`Ctrl+Tab` / `Ctrl+Shift+Tab` switch sessions when supported by your terminal.
Use tmux prefix + `z` to restore a hidden sidebar.

Session processes stay alive after detaching. Run pit again in the same directory
to reconnect. State is stored in `~/.local/state/pit`; conversations remain in
`~/.pi/agent/sessions`.
