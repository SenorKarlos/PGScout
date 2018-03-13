# Configuration with default values
import json
import logging
import os
import sys
from threading import Thread

import configargparse
import time
from mrmime import init_mr_mime
from mrmime.cyclicresourceprovider import CyclicResourceProvider

from pgscout.proxy import check_proxies

log = logging.getLogger(__name__)

args = None


def cfg_get(key, default=None):
    global args
    return getattr(args, key)


def cfg_set(key, value):
    global args
    setattr(args, key, value)


def parse_args():
    global args
    defaultconfigfiles = []
    if '-c' not in sys.argv and '--config' not in sys.argv:
        defaultconfigfiles = ['config.ini']

    parser = configargparse.ArgParser(
        default_config_files=defaultconfigfiles)

    parser.add_argument('-c', '--config',
                        is_config_file=True, help='Specify configuration file.')

    parser.add_argument('-hs', '--host', default='127.0.0.1',
                        help='Host or IP to bind to.')

    parser.add_argument('-p', '--port', type=int, default=4242,
                        help='Port to bind to.')

    parser.add_argument('-hk', '--hash-key', required=True, action='append',
                        help='Hash key(s) to use.')

    parser.add_argument('-pf', '--proxies-file',
                        help='Load proxy list from text file (one proxy per line).')

    parser.add_argument('-l', '--level', type=int, default=30,
                        help='Minimum trainer level required. Lower levels will yield an error.')

    parser.add_argument('-pgpmult', '--pgpool-acct-multiplier', type=int, default=1,
                        help='Use each account fetched from PGPOOL this number of times')

    parser.add_argument('-mqj', '--max-queued-jobs', type=int, default=0,
                        help='Maximum number of queued scout jobs before rejecting new jobs. 0 (default) means no restriction.')

    parser.add_argument('-mjttl', '--max-job-ttl', type=int, default=0,
                        help='Maximum number of minutes a job is allowed to be queued before it expires (Time-To-Live). '
                             'Expired jobs will be rejected when it''s their turn. 0 (default) means no restriction.')

    parser.add_argument('-sb', '--shadowban-threshold', type=int, default=5,
                        help='Mark an account as shadowbanned after this many errors. ' +
                             'If --pgpool_url is specified the account gets swapped out.')

    parser.add_argument('-iv', '--initial-view', default="logs",
                        help=('Initial view. Can be one of "logs", "scouts" or "pokemon". Default is "logs".'))

    parser.add_argument('-pgpu', '--pgpool-url',
                        help='Address of PGPool to load accounts from and/or update their details.')

    parser.add_argument('-pgpsid', '--pgpool-system-id',
                        help='System ID for PGPool. Required if --pgpool-url given.')

    parser.add_argument('-lpf', '--low-prio-file',
                        help='File with Pokemon names or IDs that will be treated with low priority or even dropped.')

    parser.add_argument('-ct', '--cache-timer', type=int, default=60,
                        help='Minutes of caching to perform (default 60)')

    accs = parser.add_mutually_exclusive_group(required=True)
    accs.add_argument('-pgpn', '--pgpool-num-accounts', type=int, default=0,
                      help='Use this many accounts from PGPool. --pgpool-url required.')

    accs.add_argument('-a', '--accounts-file',
                      help='Load accounts from CSV file containing "auth_service,username,passwd" lines.')

    args = parser.parse_args()


def init_resoures_from_file(resource_file):
    resources = []
    if resource_file:
        try:
            with open(resource_file) as f:
                for line in f:
                    # Ignore blank lines and comment lines.
                    if len(line.strip()) == 0 or line.startswith('#'):
                        continue
                    resources.append(line.strip())
        except IOError:
            log.exception('Could not load {} from {}.'.format(resource_file))
            exit(1)
    return resources


def get_pokemon_name(pokemon_id):
    if not hasattr(get_pokemon_name, 'pokemon'):
        file_path = os.path.join('pokemon.json')

        with open(file_path, 'r') as f:
            get_pokemon_name.pokemon = json.loads(f.read())
    return get_pokemon_name.pokemon[str(pokemon_id)]


def get_pokemon_id(pokemon_name):
    if not hasattr(get_pokemon_id, 'ids'):
        if not hasattr(get_pokemon_name, 'pokemon'):
            # initialize from file
            get_pokemon_name(1)

        get_pokemon_id.ids = {}
        for pokemon_id, name in get_pokemon_name.pokemon.iteritems():
            get_pokemon_id.ids[name] = int(pokemon_id)

    return get_pokemon_id.ids.get(pokemon_name, -1)


def read_pokemon_ids_from_file(f):
    pokemon_ids = set()
    for name in f:
        name = name.strip()
        # Lines starting with # mean: skip this line
        if name[0] in ('#'):
            continue
        try:
            # Pokemon can be given as Pokedex ID
            pid = int(name)
        except ValueError:
            # Perform the usual name -> ID lookup
            pid = get_pokemon_id(unicode(name, 'utf-8'))
        if pid and not pid == -1:
            pokemon_ids.add(pid)
    return sorted(pokemon_ids)


def cfg_init():
    log.info("Loading PGScout configuration...")

    parse_args()

    # MrMime config
    mrmime_cfg = {
        'pgpool_system_id': args.pgpool_system_id,
        'exception_on_captcha': True
    }

    if args.pgpool_acct_multiplier > 1:
        mrmime_cfg.update ({
            'request_retry_delay': 1,
            'full_login_flow': False,
            'scan_delay' : 5
        })

    if args.pgpool_url:
        mrmime_cfg['pgpool_url'] = args.pgpool_url
        log.info("Attaching to PGPool at {}".format(args.pgpool_url))
    init_mr_mime(mrmime_cfg)

    # Collect hash keys
    args.hash_key_provider = CyclicResourceProvider()
    for hk in args.hash_key:
        args.hash_key_provider.add_resource(hk)

    # Collect proxies
    args.proxies = check_proxies(cfg_get('proxies_file'))
    args.proxy_provider = CyclicResourceProvider()
    for proxy in args.proxies:
        args.proxy_provider.add_resource(proxy)

    args.low_prio_pokemon = []
    if args.low_prio_file:
        with open(args.low_prio_file) as f:
            args.low_prio_pokemon = read_pokemon_ids_from_file(f)
        if args.low_prio_pokemon:
            log.info("{} low priority Pokemon loaded from {}".format(len(args.low_prio_pokemon), args.low_prio_file))
            t = Thread(target=watch_low_prio_file, args=(args.low_prio_file,))
            t.daemon = True
            t.start()


def watch_low_prio_file(filename):
    statbuf = os.stat(filename)
    watch_low_prio_file.tstamp = statbuf.st_mtime
    while True:
        statbuf = os.stat(filename)
        current_mtime = statbuf.st_mtime

        if current_mtime != watch_low_prio_file.tstamp:
            with open(filename) as f:
                cfg_set('low_prio_pokemon', read_pokemon_ids_from_file(f))
                log.info("File {} changed on disk. Re-read.".format(filename))
                watch_low_prio_file.tstamp = current_mtime

        time.sleep(5)


def use_pgpool():
    return bool(args.pgpool_url and args.pgpool_system_id and args.pgpool_num_accounts > 0)
