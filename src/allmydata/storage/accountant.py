
import simplejson
import os, time, weakref, re
from zope.interface import implements
from twisted.application import service
from foolscap.api import Referenceable
from allmydata.interfaces import RIStorageServer
from allmydata.util import log, keyutil, dbutil
from allmydata.storage.crawler import ShareCrawler

class BadAccountName(Exception):
    pass

LEASE_SCHEMA_V1 = """
CREATE TABLE version
(
 version INTEGER -- contains one row, set to 1
);

CREATE TABLE shares
(
 `id` INTEGER PRIMARY KEY AUTOINCREMENT,
 `prefix` VARCHAR(2),
 `storage_index` VARCHAR(26),
 `shnum` INTEGER,
 `size` INTEGER
);
CREATE INDEX `prefix` ON shares (`prefix`);
CREATE UNIQUE INDEX `share_id` ON shares (`storage_index`,`shnum`);

CREATE TABLE leases
(
 -- FOREIGN KEY (`share_id`) REFERENCES shares(id), -- not enabled?
 -- FOREIGN KEY (`account_id`) REFERENCES accounts(id),
 `share_id` INTEGER,
 `account_id` INTEGER,
 `expiration_time` INTEGER,
 `renew_secret` VARCHAR(52),
 `cancel_secret` VARCHAR(52)
);
CREATE INDEX `account_id` ON leases (`account_id`);
CREATE INDEX `expiration_time` ON leases (`expiration_time`);

CREATE TABLE accounts
(
 `id` INTEGER PRIMARY KEY AUTOINCREMENT,
 `creation_time` INTEGER
);

"""

DAY = 24*60*60
MONTH = 30*DAY

class LeaseDB:
    STARTER_LEASE_ACCOUNTID = 1
    STARTER_LEASE_DURATION = 2*MONTH

    def __init__(self, dbfile):
        (self._sqlite,
         self._db) = dbutil.get_db(dbfile, create_version=(LEASE_SCHEMA_V1, 1))
        self._cursor = self._db.cursor()
        self._dirty = False

    def get_shares_for_prefix(self, prefix):
        self._cursor.execute("SELECT `storage_index`,`shnum`"
                             " FROM `shares`"
                             " WHERE `prefix` == ?",
                             (prefix,))
        db_shares = set([(si,shnum) for (si,shnum) in self._cursor.fetchall()])
        return db_shares

    def add_share(self, prefix, storage_index, shnum, size):
        self._dirty = True
        self._cursor.execute("INSERT INTO `shares`"
                             " VALUES (?,?,?,?,?)",
                             (None, prefix, storage_index, shnum, size))
        shareid = self._cursor.lastrowid
        self._cursor.execute("INSERT INTO `leases`"
                             " VALUES (?,?,?)",
                             (shareid,
                              self.STARTER_LEASE_ACCOUNTID,
                              time.time()+self.STARTER_LEASE_DURATION))

    def remove_deleted_shares(self, shareids):
        if shareids:
            self._dirty = True
        for deleted_shareid in shareids:
            storage_index, shnum = deleted_shareid
            self._cursor.execute("DELETE FROM `shares`"
                                 " WHERE `storage_index`=? AND `shnum`=?",
                                 (storage_index, str(shnum)))

    def change_share_size(self, storage_index, shnum, size):
        self._dirty = True
        self._cursor.execute("UPDATE `shares` SET `size`=?"
                             " WHERE storage_index=? AND shnum=?",
                             (size, storage_index, shnum))

    def commit(self):
        if self._dirty:
            self._db.commit()


class AnonymousAccount(Referenceable):
    implements(RIStorageServer)

    def __init__(self, owner_num, server, accountdir):
        self.owner_num = owner_num
        self.server = server
        self.accountdir = accountdir

    def remote_get_version(self):
        return self.server.remote_get_version()
    # all other RIStorageServer methods should pass through to self.server
    # but add owner_num=

    def remote_allocate_buckets(self, storage_index,
                                renew_secret, cancel_secret,
                                sharenums, allocated_size,
                                canary, owner_num=0):
        return self.server.remote_allocate_buckets(
            storage_index,
            renew_secret, cancel_secret,
            sharenums, allocated_size,
            canary, owner_num=self.owner_num)
    def remote_add_lease(self, storage_index, renew_secret, cancel_secret,
                         owner_num=1):
        return self.server.remote_add_lease(
            storage_index, renew_secret, cancel_secret,
            owner_num=self.owner_num)
    def remote_renew_lease(self, storage_index, renew_secret):
        return self.server.remote_renew_lease(storage_index, renew_secret)
    def remote_cancel_lease(self, storage_index, cancel_secret):
        return self.server.remote_cancel_lease(storage_index, cancel_secret)
    def remote_get_buckets(self, storage_index):
        return self.server.remote_get_buckets(storage_index)
    # TODO: add leases and ownernums to mutable shares
    def remote_slot_testv_and_readv_and_writev(self, storage_index,
                                               secrets,
                                               test_and_write_vectors,
                                               read_vector):
        return self.server.remote_slot_testv_and_readv_and_writev(
            storage_index,
            secrets,
            test_and_write_vectors,
            read_vector) # TODO: ownernum=
    def remote_slot_readv(self, storage_index, shares, readv):
        return self.server.remote_slot_readv(storage_index, shares, readv)
    def remote_advise_corrupt_share(self, share_type, storage_index, shnum,
                                    reason):
        return self.server.remote_advise_corrupt_share(
            share_type, storage_index, shnum, reason)

class Account(AnonymousAccount):
    def __init__(self, owner_num, server, accountdir):
        AnonymousAccount.__init__(self, owner_num, server, accountdir)
        self.connected = False
        self.connected_since = None
        self.connection = None
        import random
        def maybe(): return bool(random.randint(0,1))
        self.status = {"write": maybe(),
                       "read": maybe(),
                       "save": maybe(),
                       }
        self.account_message = {
            "message": "free storage! %d" % random.randint(0,10),
            "fancy": "free pony if you knew how to ask",
            }

    def remote_get_status(self):
        return self.status
    def remote_get_account_message(self):
        return self.account_message

    # these are the non-RIStorageServer methods, some remote, some local

    def _read(self, *paths):
        fn = os.path.join(self.accountdir, *paths)
        try:
            return open(fn).read().strip()
        except EnvironmentError:
            return None
    def _write(self, s, *paths):
        fn = os.path.join(self.accountdir, *paths)
        tmpfn = fn + ".tmp"
        f = open(tmpfn, "w")
        f.write(s+"\n")
        f.close()
        os.rename(tmpfn, fn)

    def set_nickname(self, nickname):
        if len(nickname) > 1000:
            raise ValueError("nickname too long")
        self._write(nickname.encode("utf-8"), "nickname")

    def get_nickname(self):
        n = self._read("nickname")
        if n is not None:
            return n.decode("utf-8")
        return u""

    def remote_get_current_usage(self):
        return self.get_current_usage()

    def get_current_usage(self):
        # read something out of a database, or something. For now, fake it.
        from random import random, randint
        return int(random() * (10**randint(1, 12)))

    def connection_from(self, rx):
        self.connected = True
        self.connected_since = time.time()
        self.connection = rx
        rhost = rx.getPeer()
        from twisted.internet import address
        if isinstance(rhost, address.IPv4Address):
            rhost_s = "%s:%d" % (rhost.host, rhost.port)
        elif "LoopbackAddress" in str(rhost):
            rhost_s = "loopback"
        else:
            rhost_s = str(rhost)
        self._write(rhost_s, "last_connected_from")
        rx.notifyOnDisconnect(self._disconnected)

    def _disconnected(self):
        self.connected = False
        self.connected_since = None
        self.connection = None
        self._write(str(int(time.time())), "last_seen")
        self.disconnected_since = None

    def _send_status(self):
        self.connection.callRemoteOnly("status", self.status)
    def _send_account_message(self):
        self.connection.callRemoteOnly("account_message", self.account_message)

    def set_status(self, write, read, save):
        self.status = { "write": write,
                        "read": read,
                        "save": save,
                        }
        self._send_status()
    def set_account_message(self, message):
        self.account_message = message
        self._send_account_message()

    def get_connection_status(self):
        # starts as: connected=False, connected_since=None,
        #            last_connected_from=None, last_seen=None
        # while connected: connected=True, connected_since=START,
        #                  last_connected_from=HOST, last_seen=IGNOREME
        # after disconnect: connected=False, connected_since=None,
        #                   last_connected_from=HOST, last_seen=STOP

        last_seen = self._read("last_seen")
        if last_seen is not None:
            last_seen = int(last_seen)
        return {"connected": self.connected,
                "connected_since": self.connected_since,
                "last_connected_from": self._read("last_connected_from"),
                "last_seen": last_seen,
                "created": int(self._read("created")),
                }


def size_of_disk_file(filename):
    s = os.stat(filename)
    sharebytes = s.st_size
    try:
        # note that stat(2) says that st_blocks is 512 bytes, and that
        # st_blksize is "optimal file sys I/O ops blocksize", which is
        # independent of the block-size that st_blocks uses.
        diskbytes = s.st_blocks * 512
    except AttributeError:
        # the docs say that st_blocks is only on linux. I also see it on
        # MacOS. But it isn't available on windows.
        diskbytes = sharebytes
    return diskbytes

class AccountingCrawler(ShareCrawler):
    """I manage a SQLite table of which leases are owned by which ownerid, to
    support efficient calculation of total space used per ownerid. The
    sharefiles (and their leaseinfo fields) is the canonical source: the
    database is merely a speedup, generated/corrected periodically by this
    crawler. The crawler both handles the initial DB creation, and fixes the
    DB when changes have been made outside the storage-server's awareness
    (e.g. when the admin deletes a sharefile with /bin/rm).
    """

    # XXX TODO new idea: move all leases into the DB. Do not store leases in
    # shares at all. The crawler will exist solely to discover shares that
    # have been manually added to disk (via 'scp' or some out-of-band means),
    # and will add 30- or 60- day "migration leases" to them, to keep them
    # alive until their original owner does a deep-add-lease and claims them
    # properly. Better migration tools ('tahoe storage export'?) will create
    # export files that include both the share data and the lease data, and
    # then an import tool will both put the share in the right place and
    # update the recipient node's lease DB.
    #
    # I guess the crawler will also be responsible for deleting expired
    # shares, since it will be looking at both share files on disk and leases
    # in the DB.
    #
    # So the DB needs a row per share-on-disk, and a separate table with
    # leases on each bucket. When it sees a share-on-disk that isn't in the
    # first table, it adds the migration-lease. When it sees a share-on-disk
    # that is in the first table but has no leases in the second table (i.e.
    # expired), it deletes both the share and the first-table row. When it
    # sees a row in the first table but no share-on-disk (i.e. manually
    # deleted share), it deletes the row (and any leases).

    slow_start = 7*60 # wait 7 minutes after startup
    minimum_cycle_time = 12*60*60 # not more than twice per day

    def __init__(self, server, statefile, leasedb):
        ShareCrawler.__init__(self, server, statefile)
        self._leasedb = leasedb
        self._expire_time = None

    def process_prefixdir(self, cycle, prefix, prefixdir, buckets, start_slice):
        # assume that we can list every bucketdir in this prefix quickly.
        # Otherwise we have to retain more state between timeslices.

        # we define "shareid" as (SI,shnum)
        disk_shares = set() # shareid
        for storage_index in buckets:
            bucketdir = os.path.join(prefixdir, storage_index)
            for sharefile in os.listdir(bucketdir):
                try:
                    shnum = int(sharefile)
                except ValueError:
                    continue # non-numeric means not a sharefile
                shareid = (storage_index, shnum)
                disk_shares.add(shareid)

        # now check the database for everything in this prefix
        db_shares = self._leasedb.get_shares_for_prefix(prefix)

        # add new shares to the DB
        new_shares = (disk_shares - db_shares)
        for shareid in new_shares:
            storage_index, shnum = shareid
            filename = os.path.join(prefixdir, storage_index, str(shnum))
            size = size_of_disk_file(filename)
            self._leasedb.add_share(prefix, storage_index, shnum, size)

        # remove deleted shares
        deleted_shares = (db_shares - disk_shares)
        self._leasedb.remove_deleted_shares(deleted_shares)

        self._leasedb.commit()


    # these methods are for outside callers to use

    def set_lease_expiration(self, enable, expire_time=None):
        """Arrange to remove all leases that are currently expired, and to
        delete all shares without remaining leases. The actual removals will
        be done later, as the crawler finishes each prefix."""
        self._do_expire = enable
        self._expire_time = expire_time

    def db_is_incomplete(self):
        # don't bother looking at the sqlite database: it's certainly not
        # complete.
        return self.state["last-cycle-finished"] is None

class Accountant(service.MultiService):
    def __init__(self, storage_server, dbfile, statefile):
        service.MultiService.__init__(self)
        self.storage_server = storage_server
        self.leasedb = LeaseDB(dbfile)
        self._active_accounts = weakref.WeakValueDictionary()
        self._accountant_window = None

        crawler = AccountingCrawler(storage_server, statefile, self.leasedb)
        self.accounting_crawler = crawler
        crawler.setServiceParent(self)

    def get_accountant_window(self, tub):
        if not self._accountant_window:
            self._accountant_window = AccountantWindow(self, tub)
        return self._accountant_window

    def get_leasedb(self):
        return self.leasedb

    def set_expiration_policy(self,
                              expiration_enabled=False,
                              expiration_mode="age",
                              expiration_override_lease_duration=None,
                              expiration_cutoff_date=None,
                              expiration_sharetypes=("mutable", "immutable")):
        pass # TODO

    def _read(self, *paths):
        fn = os.path.join(self.accountsdir, *paths)
        return open(fn).read().strip()
    def _write(self, s, *paths):
        fn = os.path.join(self.accountsdir, *paths)
        tmpfn = fn + ".tmp"
        f = open(tmpfn, "w")
        f.write(s+"\n")
        f.close()
        os.rename(tmpfn, fn)

    # methods used by StorageServer

    def get_account(self, pubkey_vs):
        ownernum = self.get_ownernum_by_pubkey(pubkey_vs)
        if pubkey_vs not in self._active_accounts:
            a = Account(ownernum, self.storage_server,
                        os.path.join(self.accountsdir, pubkey_vs))
            self._active_accounts[pubkey_vs] = a
        return self._active_accounts[pubkey_vs] # a is still alive

    def get_anonymous_account(self):
        if not self._anonymous_account:
            a = AnonymousAccount(0, self.storage_server,
                                 os.path.join(self.accountsdir, "anonymous"))
            self._anonymous_account = a
        return self._anonymous_account

    def get_ownernum_by_pubkey(self, pubkey_vs):
        if not re.search(r'^[a-zA-Z0-9+-_]+$', pubkey_vs):
            raise BadAccountName("unacceptable characters in pubkey")
        assert ("." not in pubkey_vs and "/" not in pubkey_vs)
        accountdir = os.path.join(self.accountsdir, pubkey_vs)
        if not os.path.isdir(accountdir):
            if not self.create_if_missing:
                return None
            next_ownernum = int(self._read("next_ownernum"))
            self._write(str(next_ownernum+1), "next_ownernum")
            os.mkdir(accountdir)
            self._write(str(next_ownernum), pubkey_vs, "ownernum")
            self._write(str(int(time.time())), pubkey_vs, "created")
        ownernum = int(self._read(pubkey_vs, "ownernum"))
        return ownernum

    # methods used by admin interfaces
    def get_all_accounts(self):
        for d in os.listdir(self.accountsdir):
            if d.startswith("pub-v0-"):
                yield (d, self.get_account(d, None)) # TODO: None is weird


class AccountantWindow(Referenceable):
    def __init__(self, accountant, tub):
        self.accountant = accountant
        self.tub = tub

    def remote_get_account(self, msg, sig, pubkey_vs):
        print "GETTING ACCOUNT", msg
        vk = keyutil.parse_pubkey(pubkey_vs)
        vk.verify(sig, msg)
        account = self.accountant.get_account(pubkey_vs)
        msg_d = simplejson.loads(msg.decode("utf-8"))
        rxFURL = msg_d["please-give-Account-to-rxFURL"].encode("ascii")
        account.set_nickname(msg_d["nickname"])
        d = self.tub.getReference(rxFURL)
        def _got_rx(rx):
            account.connection_from(rx)
            d = rx.callRemote("account", account)
            d.addCallback(lambda ign: account._send_status())
            d.addCallback(lambda ign: account._send_account_message())
            return d
        d.addCallback(_got_rx)
        d.addErrback(log.err, umid="nFYfcA")
        return d
