import time
import logging
import sys
import time

import pgscout.Scout
from pgscout.Scout import Scout
from pgscout.config import use_pgpool, cfg_get
from pgscout.utils import load_pgpool_accounts

log = logging.getLogger(__name__)

def update_multiplier_accounts(username, password, acctinfo):
    for s in range(0,len(pgscout.Scout.scouts)):
        if (pgscout.Scout.scouts[s].acc.duplicate == 0) and (pgscout.Scout.scouts[s].acc.username == username)  and (pgscout.Scout.scouts[s].acc.password == password):  # found original account
            log.info("Changing {} duplicate {}-{} accounts from {} to {}".format(cfg_get('pgpool_acct_multiplier')-1,s+1,s+cfg_get('pgpool_acct_multiplier'),username,acctinfo['username']))
            for x in range(s+1, s+cfg_get('pgpool_acct_multiplier')):  
                pgscout.Scout.scouts[x].newacc = acctinfo
                pgscout.Scout.scouts[x].acc.duplicate = 2
            break

class ScoutGuard(object):

    def __init__(self, auth, username, password, job_queue, duplicate, index):
        self.job_queue = job_queue
        self.active = False
        self.index = index
        self.newacc = {}

        # Set up initial account
        initial_account = {
            'auth_service': auth,
            'username': username,
            'password': password
        }
        if not username and use_pgpool():
                initial_account = load_pgpool_accounts(1, reuse=True)
        self.acc = self.init_scout(initial_account, duplicate)
        self.active = True

    def init_scout(self, acc_data, duplicate):
        return Scout(acc_data['auth_service'], acc_data['username'], acc_data['password'], self.job_queue, duplicate)

    def run(self):
        while True:
            self.active = True
            self.acc.run()
            self.active = False

            # if duplicate wait for master account to reconfigure this account to new login info
            if self.acc.duplicate == 1:
                log.info("semaphore waiting, index {}".format(self.index))
                while self.acc.duplicate == 1:
                    time.sleep(1)
                    pass
                log.info("exited semaphore, index {}".format(self.index))

            if self.acc.duplicate == 2:
                log.info("duplicate index {} changing accounts from {} to {}".format(self.index,self.acc.username,self.newacc['username']))
                self.acc.release(reason="removing multiplier account")
                self.acc = self.init_scout(self.newacc, 1)
                self.acc.duplicate = 1;
                
            # Scout disabled, probably (shadow)banned.
            if self.acc.duplicate == 0:
                if use_pgpool():
                    self.acc.release(reason=self.acc.last_msg)
                    self.swap_account()
                else:
                    # We don't have a replacement account, so just wait a veeeery long time.
                    self.acc.release(reason=self.acc.last_msg)
                    time.sleep(60*60*24*1000)
                    break

    def swap_account(self):
        username = self.acc.username
        password = self.acc.password
        while True:
            new_acc = load_pgpool_accounts(1)
            if new_acc:
                log.info("Swapping bad account {} with new account {}".format(self.acc.username, new_acc['username']))
                update_multiplier_accounts(username,password,new_acc)
                self.acc = self.init_scout(new_acc, 0)
                break
            log.warning("Could not request new account from PGPool. Out of accounts? Retrying in 1 minute.")
            time.sleep(60)
