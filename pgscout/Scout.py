import logging
from base64 import b64encode
from collections import deque

import time
from mrmime.pogoaccount import POGOAccount, CaptchaException
from mrmime.shadowbans import COMMON_POKEMON
from mrmime.utils import jitter_location
from pgoapi.exceptions import AuthException, BannedAccountException
from pgoapi.protos.pogoprotos.networking.responses.encounter_response_pb2 import *

from pgscout.config import cfg_get
from pgscout.moveset_grades import get_moveset_grades
from pgscout.stats import inc_for_pokemon
from pgscout.utils import calc_pokemon_level, calc_iv, distance, PRIO_NAMES

log = logging.getLogger(__name__)


# Collect this many samples to determine an encounters/hour value.
NUM_PAUSE_SAMPLES = 60

ENCOUNTER_RESULTS = {
    0: "ENCOUNTER_ERROR",
    1: "ENCOUNTER_SUCCESS",
    2: "ENCOUNTER_NOT_FOUND",
    3: "ENCOUNTER_CLOSED",
    4: "ENCOUNTER_POKEMON_FLED",
    5: "ENCOUNTER_NOT_IN_RANGE",
    6: "ENCOUNTER_ALREADY_HAPPENED",
    7: "POKEMON_INVENTORY_FULL",
    8: "ENCOUNTER_BLOCKED_BY_ANTICHEAT"
}


class Scout(POGOAccount):
    def __init__(self, auth, username, password, job_queue, duplicate):
        super(Scout, self).__init__(auth, username, password,
                                    hash_key_provider=cfg_get('hash_key_provider'),
                                    proxy_provider=cfg_get('proxy_provider'))

        self.job_queue = job_queue

        # Stats
        self.start_time = time.time()
        self.previous_encounter = None
        self.total_encounters = 0
        self.duplicate = duplicate   # 0 = master account, 1 = duplicate account, 2 = release duplicate account from semaphore loop 

        # Collects the last few pauses between encounters to measure a "encounters per hour" value
        self.past_pauses = deque()
        self.encounters_per_hour = float(0)

        # Number of errors that may be the cause of a shadowban
        self.errors = 0

    def run(self):
        self.log_info("Waiting for job...")
        while True:
            if self.duplicate == 2:   # master account is banned so stop all duplicate accounts
                break
            (prio, t, job) = self.job_queue.get()
            try:
                if job.expired():
                    self.log_warning(
                        u"Scout job for {} at {}, {} expired. Rejecting.".format(job.pokemon_name, job.lat, job.lng))
                    job.result = self.scout_error(self.last_msg, False)
                    continue

                self.log_info(u"Scouting a {} at {}, {} with {} priority".format(job.pokemon_name, job.lat, job.lng,
                                                                                 PRIO_NAMES[prio]))

                # Initialize API
                (lat, lng) = jitter_location(job.lat, job.lng)
                self.set_position(lat, lng, job.altitude)
                if not self.check_login():
                    job.result = self.scout_error(self.last_msg, False)
                    if self.is_banned() or self.has_captcha():
                        break
                    else:
                        continue

                if job.encounter_id and job.spawn_point_id:
                    job.result = self.scout_by_encounter_id(job)
                else:
                    if self.find_pokemon(job):
                        time.sleep(2)
                        job.result = self.scout_by_encounter_id(job)
                    else:
                        job.result = self.scout_error("Could not determine encounter_id for {} at {}, {}".format(job.pokemon_name, job.lat, job.lng), False)

                # Mark shadowbanned if too many errors
                sb_threshold = cfg_get('shadowban_threshold')
                if sb_threshold and self.errors >= sb_threshold:
                    self.shadowbanned = True

                if self.shadowbanned:
                    self.log_warning("Account probably shadowbanned. Stopping.")
                    break

            except (AuthException, BannedAccountException, CaptchaException) as e:
                job.result = self.scout_error(self.last_msg, False)
                break
            except Exception:
                job.result = self.scout_error(repr(sys.exc_info()), False)
            finally:
                job.processed = True
                if self.is_banned() or self.has_captcha() or job.result["swap_account"]:
                    break

    def update_history(self):
        if self.previous_encounter:
            # Determine current pause
            now = time.time()
            pause = now - self.previous_encounter
            self.past_pauses.append(pause)
            if len(self.past_pauses) > NUM_PAUSE_SAMPLES:
                self.past_pauses.popleft()
            avg_pause = reduce(lambda x, y: x + y, self.past_pauses) / len(
                self.past_pauses)
            self.encounters_per_hour = 3600 / avg_pause

        self.total_encounters += 1
        self.previous_encounter = time.time()

    def parse_wild_pokemon(self, responses):
        wild_pokemon = []
        cells = responses['GET_MAP_OBJECTS'].map_cells
        for cell in cells:
            wild_pokemon += cell.wild_pokemons
        return wild_pokemon

    def find_pokemon(self, job):
        tries = 0
        max_tries = 3
        wild_pokemon = []
        while tries < max_tries:
            tries += 1
            try:
                self.log_info("Looking for {} at {}, {} - try {}".format(job.pokemon_name, job.lat, job.lng, tries))
                self.set_position(job.lat, job.lng, job.altitude)
                response = self.req_get_map_objects()
                wild_pokemon = self.parse_wild_pokemon(response)
                if len(wild_pokemon) > 0:
                    break
            except Exception as e:
                self.log_error('Exception on GMO try {}: {}'.format(tries, repr(e)))
            time.sleep(11)

        if len(wild_pokemon) == 0:
            self.log_info("Still no wild Pokemon found. Giving up.")
            return False

        # find all pokemon with desired id
        candidates = filter(
            lambda pkm: pkm.pokemon_data.pokemon_id == job.pokemon_id,
            wild_pokemon)

        target = None
        if len(candidates) == 1:
            # exactly one pokemon of this id found
            target = candidates[0]
        elif len(candidates) > 1:
            # multiple pokemon found, pick one with lowest distance to search position
            loc = (job.lat, job.lng)
            min_dist = False
            for pkm in candidates:
                d = distance(loc, (pkm.latitude, pkm.longitude))
                if not min_dist or d < min_dist:
                    min_dist = d
                    target = pkm

        # no pokemon found
        if target is None:
            self.log_info("No wild {} found at {}, {}.".format(job.pokemon_name, job.lat, job.lng))
            return False

        # now set encounter id and spawn point id
        self.log_info("Got encounter_id for {} at {}, {}.".format(job.pokemon_name, target.latitude, target.longitude))
        job.encounter_id = target.encounter_id
        job.spawn_point_id = target.spawn_point_id
        return True

    def scout_by_encounter_id(self, job):
        self.log_info("Performing encounter request at {}, {}".format(job.lat, job.lng))
        responses = self.req_encounter(job.encounter_id, job.spawn_point_id, float(job.lat), float(job.lng))
        return self.parse_encounter_response(responses, job)

    def parse_encounter_response(self, responses, job):
        if not responses:
            return self.scout_error("Empty encounter response", False)

        encounter = responses.get('ENCOUNTER')
        if not encounter:
            return self.scout_error("No encounter result returned.", False)
        if not encounter.HasField('wild_pokemon'):
            # Only count as error if it was a rare Pokemon - errors on common Pokemon won't mean shadowban
            if job.pokemon_id not in COMMON_POKEMON:
                self.errors += 1
            return self.scout_error("No wild pokemon info found.", False)

        enc_status = encounter.status

        # Check for shadowban - ENCOUNTER_BLOCKED_BY_ANTICHEAT
        if enc_status == 8:
            self.errors += 1
            self.shadowbanned = True

        if enc_status != 1:
            return self.scout_error(ENCOUNTER_RESULTS[enc_status], False)

        # Reset error counter if rare Pokemon was found
        if job.pokemon_id not in COMMON_POKEMON:
            self.errors = 0

        scout_level = self.get_stats('level')
        if scout_level < cfg_get('level'):
            return self.scout_error(
                "Trainer level {} is too low. Needs to be {}+".format(
                    scout_level, cfg_get("level")), True)

        pokemon_info = encounter.wild_pokemon.pokemon_data
        cp = pokemon_info.cp
        pokemon_level = calc_pokemon_level(pokemon_info.cp_multiplier)
        probs = encounter.capture_probability.capture_probability

        at = pokemon_info.individual_attack
        df = pokemon_info.individual_defense
        st = pokemon_info.individual_stamina
        iv = calc_iv(at, df, st)
        moveset_grades = get_moveset_grades(job.pokemon_id, job.pokemon_name,
                                            pokemon_info.move_1,
                                            pokemon_info.move_2)

        responses = {
            'success': True,
            'swap_account': False,
            'encounter_id': job.encounter_id,
            'encounter_id_b64': b64encode(str(job.encounter_id)),
            'height': pokemon_info.height_m,
            'weight': pokemon_info.weight_kg,
            'gender': pokemon_info.pokemon_display.gender,
            'iv_percent': iv,
            'iv_attack': at,
            'iv_defense': df,
            'iv_stamina': st,
            'move_1': pokemon_info.move_1,
            'move_2': pokemon_info.move_2,
            'rating_attack': moveset_grades['offense'],
            'rating_defense': moveset_grades['defense'],
            'cp': cp,
            'cp_multiplier': pokemon_info.cp_multiplier,
            'level': pokemon_level,
            'catch_prob_1': probs[0],
            'catch_prob_2': probs[1],
            'catch_prob_3': probs[2],
            'scout_level': scout_level,
            'encountered_time': time.time()
        }

        # Add form of Unown
        if job.pokemon_id == 201:
            responses['form'] = pokemon_info.pokemon_display.form

        self.update_history()

        self.log_info(
            u"Found a {:.1f}% ({}/{}/{}) L{} {} with {} CP (scout level {}).".format(
                iv, at, df, st, pokemon_level, job.pokemon_name, cp, scout_level))
        inc_for_pokemon(job.pokemon_id)
        return responses

    def scout_error(self, error_msg, swap_acct):
        if error_msg != self.last_msg:
            self.log_error(error_msg)
        return {
            'success': False,
            'error': error_msg,
            'swap_account': swap_acct
        }

    def jittered_location(self, job):
        (lat, lng) = jitter_location(job.lat, job.lng)
        self.set_position(lat, lng, job.altitude)
        return lat, lng
