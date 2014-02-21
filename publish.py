from __future__ import division
from __future__ import print_function

import psycopg2
import librato
import time
import json
import sys


def fetch_pg_version(cur):
    cur.execute("SELECT split_part(version(), ' ', 2)")
    res = cur.fetchall()
    return tuple(map(int, res[0][0].split(".")))

# TODO: Add incremental versions VS these global ones
def fetch_index_hits(cur):
    cur.execute("SELECT (sum(idx_blks_hit)) / (1 + sum(idx_blks_hit + idx_blks_read)) AS ratio FROM pg_statio_user_indexes")
    res = cur.fetchall()
    return float(res[0][0])

def fetch_cache_gits(cur):
    cur.execute("SELECT sum(heap_blks_hit) / (1 + sum(heap_blks_hit) + sum(heap_blks_read)) AS ratio FROM pg_statio_user_tables")
    res = cur.fetchall()
    return float(res[0][0])

def fetch_backend_states(cur, version):
    if version < (9,2):
        cur.execute("""select (case
            when current_query = '<IDLE> in transaction' then 'idle_in_transaction'
            when current_query = '<IDLE>' then 'idle'
            when current_query like 'autovacuum:%' then 'autovacuum'
            else 'active'
            end), count(*) from pg_stat_activity group by 1
            """)
    else:
        cur.execute("select state, count(*) from pg_stat_activity group by 1")
    res = cur.fetchall()
    states = []
    for state, count in res:
        if state is None:
            state = 'null'
        state = state.replace(' ', '_')
        states.append((state, int(count)))
    return states

def fetch_waiting_backends(cur):
    cur.execute("select count(*) from pg_stat_activity where waiting")
    res = cur.fetchall()
    return int(res[0][0])

def fetch_backend_times(cur, version):
    if version < (9,2):
        where = "current_query not like '<IDLE>%' and current_query not like '%pg_stat%' and current_query not like 'autovacuum:%'"
    else:
        where = "state != 'idle' and query not like '%pg_stat%'"
    cur.execute("select extract ('epoch' from GREATEST(now() - query_start, '0')) as runtime from pg_stat_activity where %s order by 1" % where)
    res = cur.fetchall()
    times = [row[0] for row in res]
    if times:
        max_time = max(times)
        mean_time = sum(times) / len(times)
        median_time = times[int(len(times) / 2)]
        return [
            ("max_query_time", max_time),
            ("mean_query_time", mean_time),
            ("median_query_time", median_time),
        ]
    else:
        return []

def fetch_seq_scans(cur):
    cur.execute("SELECT sum(seq_scan), sum(idx_scan) FROM pg_stat_user_tables")
    res = cur.fetchall()
    return [
        ("sequential_scans", str(res[0][0])),
        ("index_scans", str(res[0][1]))
    ]

def fetch_db_stats(cur, db, version):
    fields = [
        ("xact_commit", "transactions_committed"),     # Number of transactions in this database that have been committed
        ("xact_rollback", "transactions_rolled_back"), # Number of transactions in this database that have been rolled back
        ("blks_read", "disk_blocks_read"),             # Number of disk blocks read in this database
        ("blks_hit", "disk_blocks_cache_hit"),         # Number of times disk blocks were found already in the buffer cache, so that a read was not necessary (this only includes hits in the PostgreSQL buffer cache, not the operating system's file system cache)
        ("tup_returned", "rows_returned"),             # Number of rows returned by queries in this database
        ("tup_fetched", "rows_fetched"),               # Number of rows fetched by queries in this database
        ("tup_inserted", "rows_inserted"),             # Number of rows inserted by queries in this database
        ("tup_updated", "rows_updated"),               # Number of rows updated by queries in this database
        ("tup_deleted", "rows_deleted"),               # Number of rows deleted by queries in this database
    ]
    if version >= (9,2):
        fields.extend([
            ("temp_bytes", "temp_file_bytes"),         # Total amount of data written to temporary files by queries in this database. All temporary files are counted, regardless of why the temporary file was created, and regardless of the log_temp_files setting.
            ("blk_read_time", "block_read_time"),      # Time spent reading data file blocks by backends in this database, in milliseconds
        ])
    cur.execute("select %s from pg_stat_database where datname = '%s'" % (", ".join(f for f, _ in fields), db))
    res = cur.fetchall()
    row = res[0]
    result = []
    for name, value in zip((name for _, name in fields), row):
        result.append((name, str(long(round(value)))))
    return result

# TODO: Implement
def fetch_index_sizes(cur):
    pass

# TODO: Implement
def fetch_tables_sizes(cur):
    pass

def dsn_for_db(db):
    creds = ("host=%s port=%d dbname=%s user=%s password=%s" %
        (db['host'], db['port'], db['database'], db['user'], db['password']))
    return creds + " connect_timeout=2 application_name=postgres-librato"

def publish_forever(config, librato_client):
    while True:
        q = librato_client.new_queue()
        for db in config['databases']:
            try:
                conn = psycopg2.connect(dsn_for_db(db))
            except psycopg2.OperationalError as e:
                print(repr(e))
                continue
            cur = conn.cursor()
            source = db["source"]

            try:
                version = fetch_pg_version(cur)

                index_hits = fetch_index_hits(cur)
                cache_hits = fetch_cache_gits(cur)
                states = fetch_backend_states(cur, version)
                waiting = fetch_waiting_backends(cur)
                times = fetch_backend_times(cur, version)
                scans = fetch_seq_scans(cur)
                db_stats = fetch_db_stats(cur, db["database"], version)
                index_sizes = fetch_index_sizes(cur)

                #print(".", end="") # TODO: Add CLI flag --feedback

                q.add('postgres.pg_stat.index_hits', index_hits, source=source)
                q.add('postgres.pg_stat.cache_hits', cache_hits, source=source)
                for state, count in states:
                    q.add('postgres.pg_stat.backends_' + state, count, source=source)
                q.add('postgres.pg_stat.backends_waiting', waiting, source=source)
                for metric, secs in times:
                    q.add('postgres.pg_stat.' + metric, secs, source=source)
                for metric, count in scans:
                    q.add('postgres.pg_stat.' + metric, count, type='counter', source=source)
                for metric, count in db_stats:
                    q.add('postgres.pg_stat.' + metric, count, type='counter', source=source)

            except Exception as e:
                print(repr(e))

            cur.close()
            conn.close()

        try:
            q.submit()
        except Exception as e:
            print(repr(e))

        time.sleep(config["interval"])


if __name__ == '__main__':
    config_file = 'config.json'
    if len(sys.argv) > 1:
        config_file = sys.argv[1]

    with open(config_file) as f:
        config = json.load(f)
        librato_client = librato.connect(config["librato"]["user"], config["librato"]["token"])

    publish_forever(config, librato_client)
