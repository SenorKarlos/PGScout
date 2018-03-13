import logging
import sys
import time

from pgscout.Scout import Scout
from pgscout.config import use_pgpool, cfg_get
from pgscout.utils import load_pgpool_accounts

log = logging.getLogger(__name__)

class ScoutGuard(object):

    def __init__(self, auth, username, password, job_queue, duplicate, index):
        self.job_queue = job_queue
        self.active = False
        self.index = index
        self.newacc = {}
        self.scouts = []
        
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

    def run(self, *scouts):
        while True:
            self.scouts = list(scouts)
            if self.acc.username != "Waiting for account":
                self.active = True
                self.acc.run()
                self.active = False
            else:
                self.active = False
                self.acc.last_msg=""

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
                
            if self.acc.duplicate == 0:
                if use_pgpool():
                    self.acc.release(reason=self.acc.last_msg)
                    self.swap_account(self.scouts)
                else:
                    # We don't have a replacement account since using CSV file, so just wait a very long time.
                    self.acc.release(reason=self.acc.last_msg)
                    time.sleep(60*60*24*1000)
                    break

    def swap_account(self, scouts):
        username = self.acc.username
        password = self.acc.password
        markedwaiting = False
        while True:
            new_acc = load_pgpool_accounts(1)
            if new_acc:
                log.info("Swapping bad account {} with new account {}".format(self.acc.username, new_acc['username']))
                self.update_multiplier_accounts(scouts,self.index,username,password,new_acc,2)
                self.acc = self.init_scout(new_acc, 0)
                break
            elif not markedwaiting:
                self.acc.username = "Waiting for account"
                self.acc.last_msg = ""
                self.update_multiplier_accounts(scouts,self.index,username,password,{'username' : self.acc.username},1)
                markedwaiting = True
            log.warning("Could not request new account from PGPool. Out of accounts? Retrying in 1 minute.")
            time.sleep(60)

    def update_multiplier_accounts(self, scouts, scoutindex, username, password, acctinfo, duplicate_setting):
        s = scoutindex * cfg_get('pgpool_acct_multiplier')
        log.info("Changing {} duplicate {}-{} accounts from {} to {}".format(cfg_get('pgpool_acct_multiplier')-1,s+1,s+cfg_get('pgpool_acct_multiplier'),username,acctinfo['username']))
        for x in range(s+1, s+cfg_get('pgpool_acct_multiplier')):  
            scouts[x].newacc = acctinfo
            scouts[x].acc.duplicate = duplicate_setting

