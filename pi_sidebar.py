#!/usr/bin/env python3
"""Pi sessions with a persistent tmux sidebar and a Pi session-sync hook."""
import curses
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


def tmux(*args, check=True, socket=None):
    return subprocess.run(['tmux', *(['-S', socket] if socket else []), *map(str, args)], text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check).stdout.strip()


def read_state(path):
    return json.loads(Path(path).read_text())


def write_state(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as file:
        json.dump(data, file)
    os.replace(file.name, path)


def window_pane(window):
    return tmux('list-panes', '-t', window, '-F', '#{pane_id}').splitlines()[0]


def fix_sidebar_width(session, pane):
    window = f'{session}:board'
    for hook in ('after-resize-pane', 'window-resized'):
        tmux('set-hook', '-u', '-t', session, hook)
        tmux('set-hook', '-wu', '-t', window, hook)
    tmux('set-option', '-w', '-t', window, '@pit_sidebar', pane)
    tmux('set-option', '-s', 'extended-keys', 'on')
    for key, direction in (('C-Tab', 'C-n'), ('C-BTab', 'C-p'), ('C-S-Tab', 'C-p')):
        tmux('bind-key', '-T', 'root', key, 'if-shell', '-F', '#{@pit_sidebar}',
             f"run-shell -C 'send-keys -t #{{@pit_sidebar}} {direction}'",
             f'send-keys {key}')
    tmux('set-option', '-w', '-t', window, '@sidebar_width',
         tmux('display-message', '-p', '-t', pane, '#{pane_width}'))
    tmux('set-hook', '-w', '-t', window, 'after-resize-pane',
         f"set-option -wF -t {pane} @sidebar_width '#{{pane_width}}'")
    tmux('set-hook', '-w', '-t', window, 'window-resized',
         f"run-shell -C -t {pane} 'resize-pane -t {pane} -x #{{@sidebar_width}}'")
    # resize-pane -M returns before dragging finishes, so save on mouse release.
    record_width = ('tmux set-option -w -t #{@pit_sidebar} @sidebar_width '
                    '"$(tmux display-message -p -t #{@pit_sidebar} \'##{pane_width}\')"')
    for key in ('MouseDragEnd1Border', 'MouseDragEnd1Pane'):
        action = 'run-shell -t = ' + shlex_quote(record_width)
        if key == 'MouseDragEnd1Pane':
            action = 'if-shell -F "#{mouse_any_flag}" "send-keys -M" ' + shlex_quote(action)
        tmux('bind-key', '-T', 'root', key, 'if-shell', '-F', '-t', '=',
             '#{@pit_sidebar}', action)


def board_right(session):
    lines = tmux('list-panes', '-t', f'{session}:board', '-F', '#{pane_index} #{pane_id}').splitlines()
    # The right pane was split off last, so it has the highest index regardless of pane-base-index.
    return max(lines, key=lambda line: int(line.split()[0])).split()[1]


def switch(data, index):
    if index == data['active']:
        return
    right = board_right(data['session'])
    current = data['active']
    if current is not None:
        tmux('swap-pane', '-d', '-s', right, '-t', window_pane(data['items'][current]['window']))
        right = board_right(data['session'])
    item = data['items'][index]
    if not item.get('window'):
        item['window'] = start_window(data, item)
    else:
        pane = window_pane(item['window'])
        if tmux('display-message', '-p', '-t', pane, '#{pane_dead}') == '1':
            tmux('respawn-pane', '-k', '-t', pane, '-c', data['cwd'], pi_command(item))
            watch_status(pane, item)
    tmux('swap-pane', '-d', '-s', right, '-t', window_pane(item['window']))
    data['active'] = index
    tmux('select-pane', '-t', board_right(data['session']), '-T', data['items'][index]['title'])


def sync_sessions(data):
    changed = False
    for item in list(data['items']):
        if item not in data['items'] or not item.get('window'):
            continue
        index = data['items'].index(item)
        try:
            pane = (board_right(data['session']) if index == data['active']
                    else window_pane(item['window']))
        except (subprocess.CalledProcessError, IndexError):
            if index == data['active']:
                raise
            # The window was closed outside the sidebar; keep the session resumable.
            item['window'] = None
            changed = True
            continue
        report = tmux('display-message', '-p', '-t', pane,
                      '#{pane_dead}|#{@pit_session}')
        dead, payload = report.split('|', 1)
        if dead == '1' or not payload:
            continue
        current = json.loads(payload)
        if current['session_id'] == item['session_id']:
            continue
        # The process now owns another conversation; retain the old one for resume.
        previous = dict(item, window=None)
        existing = next((other for other in data['items'] if other is not item
                         and other['session_id'] == current['session_id']
                         and not other.get('window')), None)
        if existing:
            window = item['window']
            item.update(existing, window=window)
            removed = data['items'].index(existing)
            data['items'].remove(existing)
            if data['active'] is not None and removed < data['active']:
                data['active'] -= 1
        else:
            item.update(title='New Agent Thread', created=time.time())
        item.update(current)
        data['items'].append(previous)
        changed = True
    return changed


def session_name(item, cache):
    session_id = item.get('session_id')
    if not session_id:
        return item['title']
    for file in Path(item['session_dir']).glob(f'*_{session_id}.jsonl'):
        stamp = file.stat().st_mtime_ns
        previous_stamp, name = cache.get(session_id, (None, item['title']))
        if stamp != previous_stamp:
            name = None
            first_message = None
            with file.open() as entries:
                for line in entries:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get('type') == 'session_info' and entry.get('name'):
                        name = entry['name']
                    message = entry.get('message', {})
                    if message.get('role') == 'user' and first_message is None:
                        content = message.get('content', '')
                        if isinstance(content, list):
                            content = ' '.join(block.get('text', '') for block in content
                                               if block.get('type') == 'text')
                        first_message = ' '.join(content.split())[:100]
            if not name or re.fullmatch(r'New Agent Thread(?: \d+)?', name):
                name = first_message or name or item['title']
            cache[session_id] = (stamp, name)
        return name
    return item['title']


def pi_command(item):
    # Wait until pipe-pane is listening, including for the initial idle report.
    return (f'tmux wait-for pit-{item["session_id"]}; exec ' +
            ' '.join(shlex_quote(arg) for arg in
                     ('env', 'PI_PROGRAM_STATUS=1', 'pi', '--session-id',
                      item['session_id'], '--session-dir', item['session_dir'])))


def watch_status(pane, item):
    tmux('set-option', '-p', '-t', pane, '@pit_session', '')
    tmux('set-option', '-p', '-t', pane, '@pit_status', 'idle')
    command = ' '.join(shlex_quote(arg) for arg in
                       (sys.executable, Path(__file__).resolve(), '--status', pane,
                        tmux('display-message', '-p', '#{socket_path}')))
    tmux('pipe-pane', '-O', '-t', pane, command)
    tmux('wait-for', '-S', f'pit-{item["session_id"]}')


def program_status(pane, socket=None):
    pattern = re.compile(rb'\x1b\]7501;([^\x07\x1b]*)(?:\x07|\x1b\\)')
    pending = b''
    while chunk := os.read(sys.stdin.fileno(), 4096):
        pending += chunk
        consumed = 0
        for report in pattern.finditer(pending):
            consumed = report.end()
            if len(report.group()) > 4096:
                continue
            fields = dict(pair.strip().split(b'=', 1) for pair in
                          report[1].split(b':') if b'=' in pair)
            state = fields.get(b'state')
            if fields.get(b'app') == b'pi' and not fields.get(b'id') and state in (
                    b'idle', b'working', b'blocked', b'done', b'error', b'clear'):
                tmux('set-option', '-p', '-t', pane, '@pit_status',
                     'idle' if state == b'clear' else state.decode(), check=False, socket=socket)
        # Keep only an unfinished OSC; reports may span reads.
        pending = pending[consumed:]
        start = pending.rfind(b'\x1b]7501;')
        pending = pending[start:] if start >= 0 else pending[-6:]
        pending = pending[-4096:]


def configure_clipboard(session):
    tmux('set-option', '-t', session, 'mouse', 'on')
    # mouse_word uses the global separators; keep ':' and '/' inside URLs.
    tmux('set-option', '-g', 'word-separators', ' ')
    # Fullscreen Pi owns selection and virtual scrolling; do not freeze its viewport.
    agent = '#{&&:#{@pit_sidebar},#{!=:#{pane_id},#{@pit_sidebar}}}'
    # Quote for both tmux's nested command parser and the shell.
    link = '#{?mouse_hyperlink,#{mouse_hyperlink},#{mouse_word}}'
    opener = (' '.join(shlex_quote(arg) for arg in
                       (sys.executable, Path(__file__).resolve(), '--open-link')) +
              ' #{?mouse_hyperlink,#{q:#{q:mouse_hyperlink}},#{q:#{q:mouse_word}}}'
              ' #{?mouse_hyperlink,hyperlink,word}')
    tmux('bind-key', '-T', 'root', 'MouseDown1Pane', 'if-shell', '-F', '-t', '=',
         '#{&&:' + agent + ',#{m/r:^https?://,' + link + '}}',
         'run-shell -b -t = ' + shlex_quote(opener), 'select-pane -t = ; send-keys -M')
    tmux('bind-key', '-T', 'root', 'MouseDrag1Pane', 'if-shell', '-F', '-t', '=',
         '#{||:#{pane_in_mode},#{mouse_any_flag}}', 'send-keys -M', 'copy-mode -M')
    if shutil.which('wl-copy'):
        for table in ('copy-mode', 'copy-mode-vi'):
            for key in ('MouseDragEnd1Pane', 'Enter' if table == 'copy-mode' else 'y'):
                tmux('bind-key', '-T', table, key, 'if-shell', '-F', '#{@pit_sidebar}',
                     'send-keys -X copy-pipe-and-cancel wl-copy',
                     'send-keys -X copy-pipe-and-cancel')
    if shutil.which('wl-paste'):
        paste = ('wl-paste --no-newline | tmux load-buffer -b pit-clipboard -; '
                 'tmux paste-buffer -d -p -b pit-clipboard -t #{pane_id}')
        for key in ('MouseDown2Pane', 'S-Insert', 'C-S-v'):
            fallback = ('if-shell -F "#{||:#{pane_in_mode},#{mouse_any_flag}}" '
                        '"send-keys -M" "paste-buffer -p"' if key == 'MouseDown2Pane'
                        else f'send-keys {key}')
            tmux('bind-key', '-T', 'root', key, 'if-shell', '-F', '-t', '=',
                 agent, 'run-shell ' + shlex_quote(paste), fallback)


def start_window(data, item):
    # Resume the exact Pi session without overwriting its name.
    window = tmux('new-window', '-d', '-P', '-F', '#{window_id}', '-t', data['session'],
                  '-n', 'pi', '-c', data['cwd'], pi_command(item))
    watch_status(window_pane(window), item)
    return window


def create(data, path, title=None):
    count = data['next_id']
    data['next_id'] += 1
    name = title or f'New Agent Thread {count}'
    session_id = str(uuid.uuid4())
    session_dir = str(Path.home() / '.pi/agent/sessions' /
                      ('--' + re.sub(r'[/\\:]', '-', data['cwd'].lstrip('/')) + '--'))
    # Every agent lives in its own tmux window when not visible in the right pane.
    item = {'title': name, 'created': time.time(),
            'session_id': session_id, 'session_dir': session_dir}
    item['window'] = start_window(data, item)
    data['items'].insert(0, item)
    if data['active'] is not None:
        data['active'] += 1
    switch(data, 0)
    tmux('select-pane', '-t', board_right(data['session']))
    write_state(path, data)


def archive(data, index):
    items = data['items']
    if index == data['active']:
        tmux('swap-pane', '-d', '-s', board_right(data['session']),
             '-t', window_pane(items[index]['window']))
        data['active'] = None
    item = items.pop(index)
    if item.get('window'):
        tmux('kill-window', '-t', item['window'], check=False)
    item['window'] = None
    item['completed'] = time.time()
    data.setdefault('archived', []).append(item)
    if data['active'] is not None and index < data['active']:
        data['active'] -= 1
    if items and data['active'] is None:
        switch(data, min(index, len(items) - 1))


def prompt(stdscr, label, default=''):
    height, width = stdscr.getmaxyx()
    curses.echo()
    curses.curs_set(1)
    try:
        stdscr.move(height - 2, 0)
        stdscr.clrtoeol()
        stdscr.addnstr(height - 2, 0, label, width - 1, curses.A_BOLD)
        stdscr.addnstr(height - 1, 0, default, width - 1)
        stdscr.move(height - 1, min(len(default), width - 1))
        value = stdscr.getstr(height - 1, 0, width - 1).decode(errors='replace').strip()
        return value or default
    finally:
        curses.noecho()
        curses.curs_set(0)


def sidebar(stdscr, path):
    curses.curs_set(0)
    stdscr.keypad(True)
    curses.mousemask(curses.BUTTON1_CLICKED | curses.BUTTON1_RELEASED)
    stdscr.timeout(300)
    selected = 0
    offset = 0
    last_redraw = 0
    name_cache = {}
    while True:
        data = read_state(path)
        if not data.get('ready', True):
            time.sleep(.1)
            continue
        if not data['items']:
            create(data, path)
            selected = offset = 0
        items = data['items']
        changed = sync_sessions(data)
        for i, item in enumerate(items):
            title = session_name(item, name_cache)
            if title != item['title']:
                item['title'] = title
                changed = True
                if i == data['active']:
                    tmux('select-pane', '-t', board_right(data['session']), '-T', title)
        if changed:
            write_state(path, data)
        selected = max(0, min(selected, len(items) - 1))
        height, width = stdscr.getmaxyx()
        if time.time() - last_redraw > .2:
            stdscr.erase()
            def line(y, text, attr=0):
                if 0 <= y < height:
                    try:
                        stdscr.addnstr(y, 1, text, max(width - 2, 0), attr)
                    except curses.error:
                        pass
            line(0, 'PI SESSIONS', curses.A_BOLD)
            if width >= 18:
                stdscr.addstr(0, width - 4, '[x]', curses.A_BOLD)
            line(1, 'Today')
            # Each entry has a title, status, and completion button.
            available = max(1, (height - 5) // 3)
            if selected < offset:
                offset = selected
            if selected >= offset + available:
                offset = selected - available + 1
            for i in range(offset, min(len(items), offset + available)):
                item = items[i]
                y = 3 + (i - offset) * 3
                active = i == data['active']
                try:
                    pane = board_right(data['session']) if active else window_pane(item['window'])
                    info = tmux('display-message', '-p', '-t', pane, '#{pane_dead}|#{@pit_status}')
                    dead, reported = info.split('|', 1)
                    status = 'exited' if dead == '1' else reported or 'unknown'
                except Exception:
                    status = 'closed' if not item.get('window') else '?'
                marker = '●' if active else '○'
                attr = curses.A_REVERSE if i == selected else 0
                line(y, f'{marker} {item["title"]}', attr | (curses.A_BOLD if active else 0))
                elapsed = max(0, int((time.time() - item['created']) / 60))
                age = f'{elapsed}m' if elapsed < 60 else f'{elapsed // 60}h'
                line(y + 1, f'  {Path(data["cwd"]).name} · {age} · {status}', curses.A_DIM)
                if width >= 8:
                    line(y + 2, '[Done]', curses.A_BOLD)
            line(height - 2, 'n new  a done  x close', curses.A_DIM)
            line(height - 1, 'Enter open  h hide  q detach', curses.A_DIM)
            stdscr.refresh()
            last_redraw = time.time()
        key = stdscr.getch()
        if key == curses.KEY_MOUSE:
            try:
                _, x, y, _, buttons = curses.getmouse()
                if (width >= 18 and y == 0 and width - 4 <= x < width - 1
                        and buttons & (curses.BUTTON1_CLICKED | curses.BUTTON1_RELEASED)):
                    key = ord('h')
                elif (buttons & (curses.BUTTON1_CLICKED | curses.BUTTON1_RELEASED)
                      and 0 <= x < width and 3 <= y < height - 2):
                    index = offset + (y - 3) // 3
                    if index < min(len(items), offset + available):
                        row = (y - 3) % 3
                        if row < 2:
                            selected = index
                            key = curses.KEY_ENTER
                        elif width >= 8 and 1 <= x < 7:
                            selected = index
                            key = ord('a')
            except curses.error:
                pass
        if key in (14, 16) and items:  # Ctrl+Tab / Ctrl+Shift+Tab, forwarded by tmux.
            active = data['active'] if data['active'] is not None else selected
            selected = (active + (1 if key == 14 else -1)) % len(items)
            switch(data, selected)
            tmux('select-pane', '-t', board_right(data['session']))
            write_state(path, data)
        elif key in (curses.KEY_UP, ord('k')):
            selected = max(0, selected - 1)
        elif key in (curses.KEY_DOWN, ord('j')):
            selected = min(len(items) - 1, selected + 1)
        elif key in (10, 13, curses.KEY_ENTER) and items:
            switch(data, selected)
            tmux('select-pane', '-t', board_right(data['session']))
            write_state(path, data)
        elif key == ord('n'):
            create(data, path)
            selected = 0
            offset = 0
        elif key == ord('a') and items:
            archive(data, selected)
            selected = max(0, min(selected, len(items) - 1))
            write_state(path, data)
        elif key == ord('x') and items:
            # Close this session's process/window. The active process is first parked.
            idx = selected
            if idx == data['active']:
                tmux('swap-pane', '-s', board_right(data['session']),
                     '-t', window_pane(items[idx]['window']))
                data['active'] = None
            if items[idx].get('window'):
                tmux('kill-window', '-t', items[idx]['window'], check=False)
            items[idx]['window'] = None
            # Closing a process is not Done; keep its conversation resumable.
            if data['active'] is None:
                for i, item in enumerate(items):
                    if item.get('window'):
                        switch(data, i)
                        break
            write_state(path, data)
        elif key == ord('h'):
            # Hide the sidebar without killing it; prefix + z brings it back.
            right = board_right(data['session'])
            tmux('select-pane', '-t', right)
            tmux('resize-pane', '-Z', '-t', right)
        elif key == ord('q'):
            tmux('detach-client', '-s', data['session'], check=False)
        last_redraw = 0


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == '--open-link':
        url = sys.argv[2]
        if sys.argv[3] == 'word':
            url = url.rstrip('.,;:!?)]}>\"\'')
        if re.match(r'^https?://', url):
            subprocess.Popen(['xdg-open', url], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        return
    if len(sys.argv) >= 3 and sys.argv[1] == '--status':
        program_status(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
        return
    if len(sys.argv) >= 3 and sys.argv[1] == '--sidebar':
        curses.wrapper(sidebar, sys.argv[2])
        return
    if not shutil.which('tmux'):
        sys.exit('tmux missing: install with brew install tmux or sudo apt install tmux')
    if not shutil.which('pi'):
        sys.exit('pi not found on PATH')
    cwd = str(Path.cwd().resolve())
    key = hashlib.sha256(cwd.encode()).hexdigest()[:16]
    path = str(Path.home() / '.local/state/pit' / f'{key}.json')
    name = f'pit-{key}'
    data = read_state(path) if Path(path).exists() else None
    if data is None:
        # Adopt unfinished threads from the previous temporary-state version.
        for old_path in sorted(Path(tempfile.gettempdir()).glob('pi-sidebar-*.json'),
                               key=lambda file: file.stat().st_mtime, reverse=True):
            try:
                previous = read_state(old_path)
            except (OSError, ValueError):
                continue
            if previous.get('cwd') == cwd:
                data = previous
                break
    if data is None:
        data = {'session': name, 'cwd': cwd, 'items': [], 'active': None, 'next_id': 1}
    sidebar_cmd = f'{shlex_quote(sys.executable)} {shlex_quote(Path(__file__).resolve())} --sidebar {shlex_quote(path)}'
    live = subprocess.run(['tmux', 'has-session', '-t', data['session']],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if live:
        name = data['session']
    board_exists = live and bool(tmux('list-panes', '-t', f'{name}:board',
                                     '-F', '#{pane_id}', check=False))
    if board_exists:
        sync_sessions(data)
        if data['items']:
            switch(data, 0)
        data['ready'] = True
        write_state(path, data)
        left = tmux('list-panes', '-t', f'{name}:board', '-F', '#{pane_id}').splitlines()[0]
        tmux('respawn-pane', '-k', '-t', left, sidebar_cmd)
        fix_sidebar_width(name, left)
        configure_clipboard(name)
        tmux('select-window', '-t', f'{name}:board')
        tmux('select-pane', '-t', board_right(name))
        tmux('switch-client', '-t', name, check=False) if os.environ.get('TMUX') else tmux('attach-session', '-t', name, check=False)
        return
    if live:
        # The board (and its active agent) was closed; preserve parked agents.
        if data['active'] is not None:
            data['items'][data['active']]['window'] = None
        data['active'] = None
        sync_sessions(data)
    data.update(session=name, active=None, ready=False)
    write_state(path, data)
    width, height = (map(int, tmux('display-message', '-p', '#{client_width} #{client_height}').split())
                     if os.environ.get('TMUX') else shutil.get_terminal_size())
    if live:
        pane = tmux('new-window', '-d', '-P', '-F', '#{pane_id}', '-t', name,
                    '-n', 'board', '-c', os.getcwd(), sidebar_cmd)
    else:
        pane = tmux('new-session', '-d', '-P', '-F', '#{pane_id}', '-s', name, '-n', 'board',
                    '-x', width, '-y', height, '-c', os.getcwd(), sidebar_cmd)
    tmux('split-window', '-h', '-p', '67', '-t', pane, '-c', os.getcwd())
    fix_sidebar_width(name, pane)
    tmux('resize-pane', '-t', pane, '-x', '20')
    # tmux handles dragging the pane divider to resize the sidebar.
    configure_clipboard(name)
    tmux('set-option', '-t', name, 'remain-on-exit', 'on')
    tmux('set-window-option', '-t', f'{name}:board', 'pane-border-status', 'top')
    for item in data['items']:
        if not live or not item.get('window'):
            item['window'] = start_window(data, item)
    if data['items']:
        switch(data, 0)
    data['ready'] = True
    if not data['items']:
        create(data, path, 'New Agent Thread')
    else:
        write_state(path, data)
    tmux('switch-client', '-t', name, check=False) if os.environ.get('TMUX') else tmux('attach-session', '-t', name, check=False)
    print(f'Your Pi sessions remain alive. Reattach: tmux attach -t {name}')


def shlex_quote(s):
    import shlex
    return shlex.quote(str(s))


if __name__ == '__main__':
    main()
