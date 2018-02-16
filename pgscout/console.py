# Helper function to calculate start and end line for paginated output
import logging
import math
import os
import platform
import shlex
import struct
import subprocess
import time
from datetime import datetime
from threading import Thread

from pgscout.cache import get_cached_count
from pgscout.config import cfg_get, get_pokemon_name
from pgscout.stats import get_pokemon_stats
from pgscout.utils import rss_mem_size, app_state, PRIO_NAMES

default_log_level = 0

def input_processor(state):
    mainlog = logging.getLogger()
    global default_log_level

    while True:
        # Wait for the user to press a key.
        try:
            command = raw_input()
        except Exception as e:
            command = ''

        if command.isdigit():
            state['page'] = int(command)
        elif command == 'q':
            os._exit(0)
        elif command == 'p':
            state['display'] = 'pokemon'
            state['page'] = 1
        elif command == 'u':
            state['display'] = 'queue'
            state['page'] = 1
        elif command == 't':
            app_state.toggle_new_requests()
        elif command == '':
            # Toggle between scouts and log view
            state['display'] = 'scouts' if state['display'] == 'logs' else 'logs'
            state['page'] = 1

        # Disable logging if in fullscreen more
        if state['display'] == 'logs':
            mainlog.setLevel(default_log_level)
        else:
            mainlog.setLevel(logging.CRITICAL)


def print_status(scouts, initial_display, jobs):
    global status
    global default_log_level

    state = {
        'page': 1,
        'display': initial_display
    }

    default_log_level = logging.getLogger().getEffectiveLevel()
    if initial_display != 'logs':
        logging.getLogger().setLevel(logging.CRITICAL)
    
    # Start another thread to get user input.
    t = Thread(target=input_processor,
               name='input_processor',
               args=(state,))
    t.daemon = True
    t.start()

    while True:
        time.sleep(1)
        if state['display'] == 'logs':
            continue

        lines = []
        lines.append(
            "Accepting requests: {} | Job queue length: {} | Cached encounters: {} | Mem Usage: {}".format(
                app_state.accept_new_requests, jobs.qsize(), get_cached_count(), rss_mem_size()))

        if state['display'] == 'scouts':
            total_pages = print_scouts(lines, state, scouts)
        elif state['display'] == 'pokemon':
            total_pages = print_pokemon(lines, state)
        elif state['display'] == 'queue':
            total_pages = print_job_queue(lines, state, jobs)

        # Encounters
        enctotal = 0
        for scout in scouts:
            enctotal   = enctotal   + (scout.acc.encounters_per_hour if scout.active else 0.0)

        if state['display'] == 'scouts':
            lines.append("")
            lines.append("Enc/hr Total:   {:5.0f}".format(enctotal))
            lines.append("")

        # Footer
        lines.append('Page {}/{}. Page number to switch pages. <enter> to '
                     'toggle log view. "p" for Pokemon stats.'
                     ' "u" for queue.'
                     ' "t" to toggle accepting new requests.'.format(
            state['page'], total_pages))

        # Print lines
        os.system('cls' if os.name == 'nt' else 'clear')
        print ('\n'.join(filter(None, lines))).encode('utf-8')  #Filter None lines from print


def print_job_queue(lines, state, queue):
    def print_job(position, entry):
        prio = PRIO_NAMES[entry[0]].upper()
        time = entry[1]
        job = entry[2]
        return line_tmpl.format(position, prio, time, job.pokemon_name)

    line_tmpl = u'{:4} | {:6} | {:15} | {}'
    lines.append("")
    lines.append("Scout Job Queue")
    lines.append(line_tmpl.format('Pos', 'Prio', 'Time', 'Pokemon'))
    return print_lines(lines, print_job, sorted(queue.queue), 5, state)


def print_scouts(lines, state, scouts):
    def scout_line(current_line, scout_guard):
        scout = scout_guard.acc
        if not scout:                           #If no scout, skip by returning None, filtered out later
            if cfg_get('proxies'):
                return line_tmpl.format(current_line, "None", "", "", "", 'No', "", "", "", "",
                                        "Waiting for new account...")
            else:
                return line_tmpl.format(current_line, "None", "", "", 'No', "", "", "", "",
                                        "Waiting for new account...")
        warn = scout.get_state('warn')
        warn_str = '' if warn is None else ('Yes' if warn else 'No')
        active = 'Yes' if scout_guard.active else 'No'
        if cfg_get('proxies'):
            return line_tmpl.format(current_line, scout.username, scout.proxy_url,
                                    hr_tstamp(scout.start_time), warn_str, active,
                                    scout.total_encounters,
                                    "{:5.1f}".format(scout.encounters_per_hour),
                                    scout.errors,
                                    hr_tstamp(scout.previous_encounter),
                                    scout.last_msg)
        else:
            return line_tmpl.format(current_line,
                                    scout.username,
                                    hr_tstamp(scout.start_time), warn_str, active,
                                    scout.total_encounters,
                                    "{:5.1f}".format(scout.encounters_per_hour),
                                    scout.errors,
                                    hr_tstamp(scout.previous_encounter),
                                    scout.last_msg)

    len_username = str(reduce(lambda l1, l2: max(l1, l2),
                              map(lambda s: len(s.get_account_username()), scouts)))
    len_num = str(len(str(len(scouts))))
    if cfg_get('proxies'):
        len_proxies = str(reduce(lambda l1, l2: max(l1, l2),
                                  map(lambda s: len(str(s.acc.proxy_url)), scouts)))
        line_tmpl = u'{:' + len_num + '} | {:' + len_username + '} | {:' + len_proxies + '} | {:14} | {:4} | {:6} | {:10} | {:6} | {:6} | {:14} | {}'
        lines.append(
            line_tmpl.format('#', 'Scout', 'Proxy', 'Start', 'Warn', 'Active', 'Encounters', 'Enc/h', 'Errors',
                             'Last Encounter', 'Message'))
    else:
        line_tmpl = u'{:' + len_num + '} | {:' + len_username + '} | {:14} | {:4} | {:6} | {:10} | {:6} | {:6} | {:14} | {}'
        lines.append(line_tmpl.format('#', 'Scout', 'Start', 'Warn', 'Active', 'Encounters', 'Enc/h', 'Errors',
                                      'Last Encounter', 'Message'))
    return print_lines(lines, scout_line, scouts, 7, state)


def print_pokemon(lines, state):
    def format_pstat_line(current_line, e):
        return line_tmpl.format(e['pid'], get_pokemon_name(e['pid']), e['count'])

    line_tmpl = u'{:3} | {:20} | {:10}'
    lines.append(line_tmpl.format('#', 'Pokemon', 'Encounters'))
    pstats = get_pokemon_stats()
    return print_lines(lines, format_pstat_line, pstats, 4, state)


def print_lines(lines, print_entity, entities, addl_lines, state):
    # Pagination.
    start_line, end_line, total_pages = calc_pagination(len(entities), addl_lines,
                                                        state)

    current_line = 0
    for e in entities:
        # Skip over items that don't belong on this page.
        current_line += 1
        if current_line < start_line:
            continue
        if current_line > end_line:
            break

        lines.append(print_entity(current_line, e))

    return total_pages


def calc_pagination(total_rows, non_data_rows, state):
    width, height = get_terminal_size()
    # Title and table header is not usable space
    usable_height = height - non_data_rows
    # Prevent people running terminals only 6 lines high from getting a
    # divide by zero.
    if usable_height < 1:
        usable_height = 1

    total_pages = int(math.ceil(total_rows / float(usable_height)))

    # Prevent moving outside the valid range of pages.
    if state['page'] > total_pages:
        state['page'] = total_pages
    if state['page'] < 1:
        state['page'] = 1

    # Calculate which lines to print (1-based).
    start_line = usable_height * (state['page'] - 1) + 1
    end_line = start_line + usable_height - 1

    return start_line, end_line, total_pages


def hr_tstamp(tstamp):
    if isinstance(tstamp, float):
        return datetime.fromtimestamp(tstamp).strftime("%d/%m %H:%M:%S")
    else:
        return tstamp


def get_terminal_size():
    """ getTerminalSize()
     - get width and height of console
     - works on linux,os x,windows,cygwin(windows)
     originally retrieved from:
     http://stackoverflow.com/questions/566746/how-to-get-console-window-width-in-python
    """
    current_os = platform.system()
    tuple_xy = None
    if current_os == 'Windows':
        tuple_xy = _get_terminal_size_windows()
        if tuple_xy is None:
            tuple_xy = _get_terminal_size_tput()
            # Needed for window's python in cygwin's xterm!
    if current_os in ['Linux', 'Darwin'] or current_os.startswith('CYGWIN'):
        tuple_xy = _get_terminal_size_linux()
    if tuple_xy is None:
        tuple_xy = (80, 25)      # Default value.
    return tuple_xy


def _get_terminal_size_windows():
    try:
        from ctypes import windll, create_string_buffer
        # stdin handle is -10
        # stdout handle is -11
        # stderr handle is -12
        h = windll.kernel32.GetStdHandle(-12)
        csbi = create_string_buffer(22)
        res = windll.kernel32.GetConsoleScreenBufferInfo(h, csbi)
        if res:
            (bufx, bufy, curx, cury, wattr,
             left, top, right, bottom,
             maxx, maxy) = struct.unpack("hhhhHhhhhhh", csbi.raw)
            sizex = right - left + 1
            sizey = bottom - top + 1
            return sizex, sizey
    except:
        pass


def _get_terminal_size_tput():
    # Get terminal width.
    # src: How do I find the width & height of a terminal window?
    # url: http://stackoverflow.com/q/263890/1706351
    try:
        cols = int(subprocess.check_call(shlex.split('tput cols')))
        rows = int(subprocess.check_call(shlex.split('tput lines')))
        return (cols, rows)
    except:
        pass


def _get_terminal_size_linux():
    def ioctl_GWINSZ(fd):
        try:
            import fcntl
            import termios
            cr = struct.unpack('hh',
                               fcntl.ioctl(fd, termios.TIOCGWINSZ, '1234'))
            return cr
        except:
            pass
    cr = ioctl_GWINSZ(0) or ioctl_GWINSZ(1) or ioctl_GWINSZ(2)
    if not cr:
        try:
            fd = os.open(os.ctermid(), os.O_RDONLY)
            cr = ioctl_GWINSZ(fd)
            os.close(fd)
        except:
            pass
    if not cr:
        try:
            cr = (os.environ['LINES'], os.environ['COLUMNS'])
        except:
            return None
    return int(cr[1]), int(cr[0])
