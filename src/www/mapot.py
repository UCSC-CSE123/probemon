from flask import Flask, request, make_response, jsonify, g, render_template
from flask_caching import Cache
from datetime import datetime, timedelta
import sqlite3
import time
import sys
from pathlib import Path
import tempfile
import atexit

sys.path.insert(0, '..')
from stats import is_local_bit_set, build_sql_query, median
import config
config.MERGED = tuple(m[:8] for m in config.MERGED)

CWD = Path(__file__).resolve().parent
DATABASE = Path.joinpath(CWD, 'probemon.db')

# temp dir for flask cache files
TMPDIR = tempfile.TemporaryDirectory(prefix='mapot-cache-').name

# cleanup temp cache dir on exit
def cleanup():
    for f in Path(TMPDIR).glob('*'):
        f.unlink()
    Path(TMPDIR).rmdir()

atexit.register(cleanup)

class InvalidUsage(Exception):
    status_code = 400

    def __init__(self, message, status_code=None, payload=None):
        Exception.__init__(self)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        self.payload = payload

    def to_dict(self):
        rv = dict(self.payload or ())
        rv['message'] = self.message
        return rv

def create_app():
    app = Flask(__name__)

    cache = Cache(config={'CACHE_TYPE': 'filesystem', 'CACHE_DIR': TMPDIR})
    cache.init_app(app)

    def get_db():
        db = getattr(g, '_database', None)
        if db is None:
            db = g._database = sqlite3.connect(f'file:{DATABASE}?mode=ro', uri=True)
        return db

    @app.teardown_appcontext
    def close_connection(exception):
        db = getattr(g, '_database', None)
        if db is not None:
            db.close()

    @app.route('/')
    @app.route('/index.html')
    def index():
        return render_template('index.html.j2')

    @app.route('/api/stats/days')
    @cache.cached(timeout=21600) # 6 hours
    def days():
        cur = get_db().cursor()
        # to store temp table and indices in memory
        sql = 'pragma temp_store = 2;'
        cur.execute(sql)
        try:
            sql = 'select date from probemon'
            sql_args = ()
            cur.execute(sql, sql_args)
        except sqlite3.OperationalError as e:
            return jsonify({'status': 'error', 'message': 'sqlite3 db is not accessible'}), 500

        days = set()
        for row in cur.fetchall():
            t = time.strftime('%Y-%m-%d', time.localtime(row[0]))
            days.add(t)
        days = sorted(list(days))
        missing = []
        last = datetime.strptime(days[-1], '%Y-%m-%d')
        day = datetime.strptime(days[0], '%Y-%m-%d')
        while day != last:
            d = day.strftime('%Y-%m-%d')
            if d not in days:
                missing.append(d)
            day += timedelta(days=1)
        data = {'first': days[0], 'last': days[-1], 'missing': missing}
        return jsonify(data)

    @app.route('/api/stats/timestamp')
    @cache.cached(timeout=60)
    def timestamp():
        # latest modification time of the db
        ts = Path(DATABASE).stat().st_mtime
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))
        return jsonify({'timestamp': timestamp})

    @app.route('/api/stats')
    @cache.cached(timeout=60, query_string=True)
    def stats():
        after = request.args.get('after')
        if after is not None:
            try:
                after = time.mktime(time.strptime(after, '%Y-%m-%dT%H:%M:%S'))
            except ValueError as v:
                raise InvalidUsage('Invalid after parameter')
        before = request.args.get('before')
        if before is not None:
            try:
                before = time.mktime(time.strptime(before, '%Y-%m-%dT%H:%M:%S'))
            except ValueError as v:
                raise InvalidUsage('Invalid before parameter')
        macs = request.args.getlist('macs')
        rssi, zero, day = None, False, False

        cur = get_db().cursor()
        # to store temp table and indices in memory
        sql = 'pragma temp_store = 2;'
        cur.execute(sql)

        sql, sql_args = build_sql_query(after, before, macs, rssi, zero, day)
        try:
            cur.execute(sql, sql_args)
        except sqlite3.OperationalError as e:
            return jsonify({'status': 'error', 'message': 'sqlite3 db is not accessible'}), 500

        # gather stats about each mac, same code as in stats.py
        # TODO: just import that
        macs = {}
        for row in cur.fetchall():
            mac = row[1]
            if is_local_bit_set(mac):
                # create virtual mac for LAA mac address
                mac = 'LAA'
            if mac not in macs:
                macs[mac] = {'vendor': row[2], 'ssid': [], 'rssi': [], 'last': row[0], 'first':row[0]}
            d = macs[mac]
            if row[3] != '' and row[3] not in d['ssid']:
                d['ssid'].append(row[3])
            if row[0] > d['last']:
                d['last'] = row[0]
            if row[0] < d['first']:
                d['first'] = row[0]
            if row[4] != 0:
                d['rssi'].append(row[4])

        # sort on frequency of appearence of a mac
        tmp = [(k,len(v['rssi'])) for k,v in macs.items()]
        tmp = [m for m,_ in reversed(sorted(tmp, key=lambda k:k[1]))]

        data = []
        # dump our stats
        for m in tmp:
            v = macs[m]
            first = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(v['first']))
            last = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(v['last']))
            t = {'mac': m, 'vendor': v['vendor'], 'ssids': sorted(v['ssid']), 'first': first, 'last': last}
            rssi = v['rssi']
            if rssi != []:
                t.update({'rssi': {'count': len(rssi), 'min': min(rssi), 'max': max(rssi),
                 'avg': sum(rssi)/len(rssi), 'median': int(median(rssi))}})
            data.append(t)

        return jsonify(data)

    @app.route('/api/probes')
    @cache.cached(timeout=60, query_string=True)
    def probes():
        after = request.args.get('after')
        if after is not None:
            try:
                after = time.mktime(time.strptime(after, '%Y-%m-%dT%H:%M:%S'))
            except ValueError as v:
                raise InvalidUsage('Invalid after parameter')
        before = request.args.get('before')
        if before is not None:
            try:
                before = time.mktime(time.strptime(before, '%Y-%m-%dT%H:%M:%S'))
            except ValueError as v:
                raise InvalidUsage('Invalid before parameter')
        rssi = request.args.get('rssi')
        if rssi is not None:
            try:
                rssi = int(rssi)
            except ValueError as v:
                raise InvalidUsage('Invalid rssi value')

        macs = request.args.get('macs')
        zero = request.args.get('zero')
        today = request.args.get('today')

        cur = get_db().cursor()
        # to store temp table and indices in memory
        sql = 'pragma temp_store = 2;'
        cur.execute(sql)

        sql, sql_args = build_sql_query(after, before, macs, rssi, zero, today)
        try:
            cur.execute(sql, sql_args)
        except sqlite3.OperationalError as e:
            return jsonify({'status': 'error', 'message': 'sqlite3 db is not accessible'}), 500

        vendor = {}
        ts = {}
        # extract data from db
        for t, mac, vs, ssid, rssi in cur.fetchall():
            #t = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(t))
            d = (t, int(rssi), ssid)
            if is_local_bit_set(mac):
                mac = 'LAA'
            if mac not in ts.keys():
                ts[mac] = [d]
                vendor[mac] = vs
            else:
                ts[mac].append(d)

        data = []
        # recollection
        for m in ts.keys():
            if m == 'LAA' or m.startswith(config.MERGED):
                continue # will deal with them later
            known = m in config.KNOWNMAC
            ssids = list(set(f[2] for f in ts[m]))
            t = {'mac':m, 'known': known, 'vendor': vendor[m], 'ssids': ssids,
                'probereq': [{'ts': int(f[0]*1000), 'rssi':f[1], 'ssid': ssids.index(f[2])} for f in ts[m]]}
            if len(t['probereq']) > 3:
                data.append(t)
        data.sort(key=lambda x:len(x['probereq']), reverse=True)
        # LAA
        if 'LAA' in ts.keys():
            ssids = list(set(f[2] for f in ts['LAA']))
            t = {'mac':'LAA', 'vendor': u'UNKNOWN', 'ssids': ssids,
                'probereq': [{'ts': int(f[0]*1000), 'rssi':f[1], 'ssid': ssids.index(f[2])} for f in ts['LAA']]}
            data.append(t)
        # MERGED
        for m in config.MERGED:
            mm = [ma for ma in ts.keys() if ma.startswith(m)]
            p = []
            for n in mm:
                p.extend(ts[n])
            ssids = list(set(f[2] for f in p))
            if len(p) == 0:
                continue
            p.sort(key=lambda x: x[0])
            t = {'mac':m, 'vendor': u'UNKNOWN', 'ssids': ssids,
                'probereq': [{'ts': int(f[0]*1000), 'rssi':f[1], 'ssid': ssids.index(f[2])} for f in p]}
            data.append(t)

        resp = make_response(jsonify(data))
        if not today:
            resp.headers['Cache-Control'] = 'max-age=21600'
        return resp

        @app.errorhandler(InvalidUsage)
        def handle_invalid_usage(error):
            response = jsonify(error.to_dict())
            response.status_code = error.status_code
            return response

    @app.route('/robots.txt')
    def robot():
        return app.send_static_file('robots.txt')

    @app.errorhandler(404)
    def error_404(e):
        return render_template('error.html.j2', error=e), 404
    @app.errorhandler(500)
    def error_500(e):
        return render_template('error.html.j2', error=e), 500

    return app

if __name__ == '__main__':
    app = create_app()
    app.run(host='0.0.0.0', port=5556, threaded=True, debug=True)
else:
    app = create_app()