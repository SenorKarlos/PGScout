# coding=utf-8
import codecs
import logging
import signal
import sys
import time
from Queue import PriorityQueue
from threading import Thread

import math
from flask import Flask, request, jsonify, url_for

from pgscout.ScoutGuard import ScoutGuard
from pgscout.ScoutJob import ScoutJob
from pgscout.cache import get_cached_encounter, cache_encounter, cleanup_cache, get_cached_count
from pgscout.config import cfg_get, cfg_init, get_pokemon_name
from pgscout.console import print_status, hr_tstamp
from pgscout.stats import get_pokemon_stats
from pgscout.utils import normalize_encounter_id, \
    load_pgpool_accounts, app_state, rss_mem_size, get_pokemon_prio, PRIO_HIGH, PRIO_LOW, PRIO_NAMES

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s [%(threadName)16s][%(module)14s][%(levelname)8s] %(message)s')

log = logging.getLogger(__name__)

# Silence some loggers
logging.getLogger('pgoapi').setLevel(logging.WARNING)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

app = Flask(__name__)

scouts = []
jobs = PriorityQueue()

# ===========================================================================


def have_active_scouts():
    for s in scouts:
        if s.active:
            return True
    return False


def reject(reason):
    log.warning(reason)
    return jsonify({
        'success': False,
        'error': reason
    })


@app.route("/iv", methods=['GET'])
def get_iv():
    if not app_state.accept_new_requests:
        return reject('Not accepting new requests.')
    if not have_active_scouts():
        return reject('No active scout available. All banned?')

    pokemon_id = request.args["pokemon_id"]
    pokemon_name = get_pokemon_name(pokemon_id)
    forced = request.args.get('forced')
    prio = PRIO_HIGH if forced is not None else get_pokemon_prio(pokemon_id)

    cache_enable = cfg_get('cache_timer') > 0
    max_queued_jobs = cfg_get('max_queued_jobs')
    num_jobs = jobs.qsize()
    if max_queued_jobs and num_jobs >= max_queued_jobs and prio == PRIO_LOW:
        return reject(
            "Job queue full ({} items). Rejecting encounter with priority '{}'.".format(num_jobs, PRIO_NAMES[prio]))

    lat = request.args["latitude"]
    lng = request.args["longitude"]
    weather = request.args.get("weather", "unknown")

    encounter_id = normalize_encounter_id(request.args.get("encounter_id"))
    # Spawn point ID is assumed to be a hex string
    spawn_point_id = request.args.get("spawn_point_id")
    despawn_time = request.args.get("despawn_time")

    if cache_enable:
        # Check cache
        cache_key = "{}-{}".format(encounter_id, weather) if encounter_id else "{}-{}-{}".format(pokemon_id, lat, lng)
        result = get_cached_encounter(cache_key)
        if result:
            log.info(
                u"Returning cached result: {:.1f}% level {} {} with {} CP".format(result['iv_percent'], result['level'], pokemon_name, result['cp']))
            return jsonify(result)

    # Create a ScoutJob
    job = ScoutJob(pokemon_id, encounter_id, spawn_point_id, lat, lng, despawn_time=despawn_time)

    # Enqueue and wait for job to be processed
    jobs.put((prio, time.time(), job))
    while not job.processed:
        time.sleep(1)

    # Cache successful jobs and return result
    if cache_enable and job.result['success']:
        cache_encounter(cache_key, job.result)
    return jsonify(job.result)


@app.route("/status/", methods=['GET'])
@app.route("/status/<int:page>", methods=['GET'])
def status(page=1):

    def td(cell):
        return "<td>{}</td>".format(cell)

    max_scouts_per_page = 25
    max_page = int(math.ceil(len(scouts)/float(max_scouts_per_page)))
    lines = "<style> th,td { padding-left: 10px; padding-right: 10px; border: 1px solid #ddd; } table { border-collapse: collapse } td { text-align:center }</style>"
    lines += "<meta http-equiv='Refresh' content='5'>"
    lines += "Accepting requests: {} | Job queue length: {} | Cached encounters: {} | Mem Usage: {}".format(
                app_state.accept_new_requests, jobs.qsize(), get_cached_count(), rss_mem_size())
    lines += "<br><br>"

    if cfg_get('proxies'):
        headers = ['#', 'Scout', 'Proxy', 'Start', 'Warn', 'Active', 'Encounters', 'Enc/h', 'Errors',
                             'Last Encounter', 'Message']
    else:
        headers = ['#', 'Scout', 'Start', 'Warn', 'Active', 'Encounters', 'Enc/h', 'Errors',
                                      'Last Encounter', 'Message']

    lines += "<table><tr>"
    for h in headers:
        lines += "<th>{}</th>".format(h)
    lines += "</tr>"

    if page * max_scouts_per_page > len(scouts):    #Page number is too great, set to last page
        page = max_page
    if page < 1:
        page = 1
    for i in range((page-1)*max_scouts_per_page, page*max_scouts_per_page):
        if i >= len(scouts):
            break
        lines += "<tr>"
        s = scouts[i].acc
        warn = s.get_state('warn')
        warn_str = '' if warn is None else ('Yes' if warn else 'No')
        lines += td(i+1)
        lines += td(s.username)
        lines += td(s.proxy_url) if cfg_get('proxies') else ""
        lines += td(hr_tstamp(s.start_time))
        lines += td(warn_str)
        lines += td('Yes' if scouts[i].active else 'No')
        lines += td(s.total_encounters)
        lines += td("{:5.1f}".format(s.encounters_per_hour))
        lines += td(s.errors)
        lines += td(hr_tstamp(s.previous_encounter))
        lines += td(s.last_msg.encode('utf-8'))
        lines += "</tr>"
    lines += "</table>"

    # Encounters
    enctotal = 0
    active = 0
    for scout in scouts:
        enctotal   = enctotal   + (scout.acc.encounters_per_hour if scout.active else 0.0)
        active     = active     + (1 if scout.active else 0)
    lines += "<br>"
    lines += "Enc/hr Total:   {:5.0f} ({} active)".format(enctotal,active)
    lines += "<br>"

    if len(scouts) > max_scouts_per_page:  # Use pages if we have more than max_scouts_per_page
        lines += "Page: "
        if max_page > 1 and page > 1:
            lines += "<a href={}>&lt;</a> | ".format(url_for('status', page=page-1))
        for p in range(1, max_page+1):
            if p == page:
                lines += str(p)
            else:
                url = url_for('status', page=p)
                lines += "<a href={}>{}</a>".format(url, p)
            if p < max_page:
                lines += " | "
        if max_page > 1 and page < max_page:
            lines += " | <a href={}>&gt;</a>".format(url_for('status', page=page+1))

    return lines


@app.route("/pokemon/<int:page>", methods=['GET', 'POST'])
@app.route("/pokemon/", methods=['GET', 'POST'])
def pokemon(page=1):

    def td(cell):
        return u"<td>{}</td>".format(cell)

    pstats = get_pokemon_stats()
    headers = ["#", "Pokemon Name", "Encounters"]
    hdict = {'#': 'pid', 'Pokemon Name': 'pname', 'Encounters': 'count'}
    sort = request.args.get('sort', 'count')
    reverse = request.args.get('reverse', True)
    max_pokemon_per_page = int(request.args.get('max_per_page', 25))
    if reverse == "False":
        reverse = False
    elif reverse == "True":
        reverse = True
    for i in range(0, len(pstats)):
        pstats[i]['pname'] = get_pokemon_name(pstats[i]['pid'])
    pstats.sort(key=lambda x: x[sort], reverse=reverse)
    max_page = int(math.ceil(len(pstats) / float(max_pokemon_per_page)))

    lines = u"<style> th,td { padding-left: 10px; padding-right: 10px; border: 1px solid #ddd; } table " \
            u"{ border-collapse: collapse } td { text-align:center }</style>"
    lines += "<h3>Pokemon Stats</h3>"
    lines += "<table><tr>"
    for h in headers:
        if hdict[h] == sort:
            r = not reverse
            arrow = u" ▲" if reverse else u" ▼"
        else:
            r = False
            arrow = ""
        lines += u"<th><a href=./{}?sort={}&reverse={}&max_per_page={}>{}{}</a></th>".format(page, hdict[h], r,
                                                                                             max_pokemon_per_page, h, arrow)
    lines += "</tr>"

    if page * max_pokemon_per_page > len(pstats):    #Page number is too great, set to last page
        page = max_page
    if page < 1:
        page = 1

    for i in range((page-1)*max_pokemon_per_page, page*max_pokemon_per_page):
        if i >= len(pstats):
            break
        pid = pstats[i]['pid']
        lines += "<tr>"
        lines += td(pid)
        lines += td(get_pokemon_name(pid))
        lines += td(pstats[i]['count'])
        lines += "</tr>"
    lines += "</table>"

    if len(pstats) > max_pokemon_per_page:  # Use pages if we have more than max_scouts_per_page
        lines += "<br>"
        lines += "Page: "
        if max_page > 1 and page > 1:
            lines += u"<a href=./{}?sort={}&reverse={}&max_per_page={}>&lt;</a> | ".format(page - 1, sort, reverse,
                                                                                           max_pokemon_per_page)
        for p in range(1, max_page + 1):
            if p == page:
                lines += str(p)
            else:
                lines += u"<a href=./{}?sort={}&reverse={}&max_per_page={}>{}</a>".format(p, sort, reverse,
                                                                                          max_pokemon_per_page, p)
            if p < max_page:
                lines += " | "
        if max_page > 1 and page < max_page:
            lines += u" | <a href=./{}?sort={}&reverse={}&max_per_page={}>&gt;</a>".format(page + 1, sort, reverse,
                                                                                           max_pokemon_per_page)
        lines += "<br>"

    lines += "<br>Max Per Page:&nbsp;&nbsp;"
    lines += "<select onchange='this.options[this.selectedIndex].value && (window.location = this.options[this.selectedIndex].value);'>"
    lines += u"<option value=./{}?sort={}&reverse={}&max_per_page=10 {}>10</option>".format(page, sort, reverse,
                                                                                            "selected" if max_pokemon_per_page == 10 else "")
    lines += u"<option value=./{}?sort={}&reverse={}&max_per_page=25 {}>25</option>".format(page, sort, reverse,
                                                                                            "selected" if max_pokemon_per_page == 25 else "")
    lines += u"<option value=./{}?sort={}&reverse={}&max_per_page=50 {}>50</option>".format(page, sort, reverse,
                                                                                            "selected" if max_pokemon_per_page == 50 else "")
    lines += u"<option value=./{}?sort={}&reverse={}&max_per_page=100 {}>100</option>".format(page, sort, reverse,
                                                                                              "selected" if max_pokemon_per_page == 100 else "")
    lines += "</select><br>"

    return lines


def run_webserver():
    app.run(threaded=True, host=cfg_get('host'), port=cfg_get('port'))


def cache_cleanup_thread():
    minutes = cfg_get('cache_timer')
    while True:
        time.sleep(60)
        num_deleted = cleanup_cache(minutes)
        log.info("Cleaned up {} entries from encounter cache.".format(num_deleted))


def load_accounts(jobs):
    accounts_file = cfg_get('accounts_file')

    accounts = []
    if accounts_file:
        log.info("Loading accounts from file {}.".format(accounts_file))
        with codecs.open(accounts_file, mode='r', encoding='utf-8') as f:
            for line in f:
                fields = line.split(",")
                fields = map(unicode.strip, fields)
                accounts.append(ScoutGuard(fields[0], fields[1], fields[2], jobs, 0, 0))
    elif cfg_get('pgpool_url') and cfg_get('pgpool_system_id') and cfg_get('pgpool_num_accounts') > 0:

        acc_json = load_pgpool_accounts(cfg_get('pgpool_num_accounts'), reuse=True)
        if isinstance(acc_json, dict) and len(acc_json) > 0:
            acc_json = [acc_json]

        log.info("Loaded {} accounts from PGPool.".format(len(acc_json)))
        for i in range(0, cfg_get('pgpool_num_accounts')):
            if i < len(acc_json):
                for x in range(0,cfg_get('pgpool_acct_multiplier')):
                    accounts.append(ScoutGuard(acc_json[i]['auth_service'], acc_json[i]['username'], acc_json[i]['password'], jobs, 0 if x==0 else 1, i if x==0 else x))
            else:
                #We are using PGPool, load empty ScoutGuards that can be filled later
                for x in range(0,cfg_get('pgpool_acct_multiplier')):
                    accounts.append(ScoutGuard(auth="", username="Waiting for account", password="", job_queue=jobs, duplicate=0 if x==0 else 1, index=i if x==0 else x))

    if len(accounts) == 0:
        log.error("Could not load any accounts. Nothing to do. Exiting.")
        sys.exit(1)

    return accounts

def signal_handler(signal, frame):
        print "Exiting"
        sys.exit(0)

# ===========================================================================

log.info("PGScout starting up.")

cfg_init()

scouts = load_accounts(jobs)
for scout in scouts:
    t = Thread(target=scout.run, args=(scouts))
    t.daemon = True
    t.start()

if cfg_get('cache_timer') > 0:
    # Cleanup cache in background
    t = Thread(target=cache_cleanup_thread, name="cache_cleaner")
    t.daemon = True
    t.start()

# Start thread to print current status and get user input.
t = Thread(target=print_status,
           name='status_printer', args=(scouts, cfg_get('initial_view'), jobs))
t.daemon = True
t.start()

# Launch the webserver
t = Thread(target=run_webserver, name='webserver')
t.daemon = True
t.start()

# Catch SIGINT to exit
signal.signal(signal.SIGINT, signal_handler)
while True:
    time.sleep(1)
