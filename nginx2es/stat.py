from collections import defaultdict, deque
from time import time
import logging
import re
import socket
import threading

import numpy as np
import pandas as pd


uuid_regex = re.compile(
    '[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}'
)

id_regex = re.compile(
    '^[0-9]+$'
)


class Stat(threading.Thread):

    columns = [
        # dimensions
        'host', 'request_path_1', 'request_path_2',
        'upstream_cache_status', 'status',
        # metrics
        'request_time', 'upstream_response_time', 'bytes_sent',
    ]
    columns_set = set(columns)

    quantiles = [.50, .75, .90, .99]

    def __init__(self, prefix, host, port=2003, use_udp=False, interval=10,
                 delay=5.):
        """Create new Stat instance

        Stat() groups the HTTP requests by `interval` seconds and calculates an
        HTTP traffic statistics. See metrics() method source code for the list
        of metrics.

        Statistics is sent by plaintext carbon protocol via TCP or UDP (UDP
        mode present, but it is not tested, actually).

        Stat calculation is delayed by `delay` seconds after the:
        - end of data interval
        - last data for this interval was seen
        """

        super(Stat, self).__init__()

        self.prefix = prefix
        self.host = host
        self.port = port
        self.use_udp = use_udp
        self.delay = delay

        self.daemon = True
        self.eof = threading.Event()
        self.interval = interval
        self.lock = threading.Lock()
        self.buffers = defaultdict(list)
        self.last_seen = {}
        self.output = None
        self.last_sent = deque()

    def connect(self):
        if self.output is not None:
            self.output.close()
        if self.use_udp:
            socktype = socket.SOCK_DGRAM
        else:
            socktype = socket.SOCK_STREAM
        addrinfo = socket.getaddrinfo(self.host, self.port, socket.AF_UNSPEC, socktype)
        for af, socktype, proto, canonname, sa in addrinfo:
            try:
                s = socket.socket(af, socktype, proto)
            except socket.error as msg:
                s = None
                continue
            try:
                s.connect(sa)
            except socket.error as msg:
                s.close()
                s = None
                continue
            break
        if s is None:
            raise Exception("Can't connect to carbon!")
        self.output = s.makefile('w')

    def hit(self, row):
        if row['status'] == 0:
            # ignore non-http connections
            return
        ts = self.timestamp(row['@timestamp'])
        d = {k: v for k, v in row.items() if k in self.columns_set}
        with self.lock:
            self.last_seen[ts] = time()
            self.buffers[ts].append(d)

    def timestamp(self, dt):
        ts = dt.timestamp()
        return int(ts - ts % self.interval)

    def run(self):
        while not self.eof.wait(1.):
            self.process(self.get_ready_buffers())
        self.process(self.buffers)

    def get_ready_buffers(self):
        ready = {}
        current_time = time()
        with self.lock:
            for ts, last_seen in list(self.last_seen.items()):
                if ts + self.interval + self.delay > current_time:
                    # delay seconds didn't pass after the data interval end
                    continue
                elif last_seen + self.delay > current_time:
                    # last data for timestamp was delivered less than
                    # self.delay seconds ago
                    continue
                else:
                    del self.last_seen[ts]
                    ready[ts] = self.buffers.pop(ts)
        return ready

    def process(self, buffers):

        for ts, rows in buffers.items():

            if ts in self.last_sent:
                logging.error("the partial statistics for %s was sent, ignoring "
                              "%s remaining records, "
                              "stat delay interval should be increased",
                              ts, len(rows))
                continue
            self.last_sent.append(ts)
            if len(self.last_sent) > 100:
                self.last_sent.popleft()

            try:
                try:
                    self.send_metrics(self.metrics(rows), ts)
                except socket.error:
                    # retry on network error
                    self.connect()
                    self.send_metrics(self.metrics(rows), ts)
            except (KeyboardInterrupt, BrokenPipeError):
                self.eof.set()
                return
            except:
                logging.error("can't send metrics", exc_info=True)

    def send_metrics(self, metrics, timestamp):
        for name, value in metrics:
            if isinstance(value, float):
                value = '%.3f' % value
            metric_string = "%s %s %s\n" % (name, value, timestamp)
            self.output.write(metric_string)
        self.output.flush()

    def log10_bins(self, series):
        # TODO: add docstring!
        # Values starting from -30, which corresponds to arguments starting from 0.001.
        # The -31 value corresponds to the arguments which are less than 0.001.
        pow10 = (np.log10(series.replace(0, np.nan)) * 10.).fillna(-31).astype(np.int)
        return (10. ** (pow10 / 10) * 1000).map(lambda x: '%d' % x)

    def metrics(self, rows):

        if not rows:
            return

        df = pd.DataFrame.from_records(rows, columns=self.columns)
        df['request_path_1'].fillna('#', inplace=True)
        df['request_path_1'].replace(uuid_regex, '<uuid>', inplace=True)
        df['request_path_1'].replace(id_regex, '<id>', inplace=True)
        df['request_path_2'].fillna('#', inplace=True)
        df['request_path_2'].replace(uuid_regex, '<uuid>', inplace=True)
        df['request_path_2'].replace(id_regex, '<id>', inplace=True)
        df['upstream_cache_status'].fillna('NONE', inplace=True)

        # upstream_response_time is a list (nginx could ask several upstreams
        # per single request if the first upstream fails), but I believe it
        # doesn't worth powder and shot to deliver all these times to carbon
        # (just the last - should be enought), the backend logs should be
        # delivered separately instead
        df['upstream_response_time'] = df.upstream_response_time.map(
            lambda x: x[-1] if isinstance(x, list) else x)

        # request time sum / count
        df['request_time_interval'] = self.log10_bins(df.request_time)
        g = df.groupby([
                'host', 'request_path_1', 'request_path_2', 'status',
                'upstream_cache_status', 'request_time_interval'
        ]).request_time
        for dims, value in g.sum().items():
            yield self.metric_name('request_time', 'sum', dims), value
        for dims, value in g.count().items():
            yield self.metric_name('request_time', 'count', dims), value

        # upstream response time sum / count
        df['upstream_response_time_interval'] = self.log10_bins(
            df[pd.notnull(df.upstream_response_time)].upstream_response_time
        )
        g = df[pd.notnull(df.upstream_response_time)].groupby([
                'host', 'request_path_1', 'request_path_2', 'status',
                'upstream_response_time_interval'
        ]).upstream_response_time
        for dims, value in g.sum().items():
            yield self.metric_name('upstream_response_time', 'sum', dims), value
        for dims, value in g.count().items():
            yield self.metric_name('upstream_response_time', 'count', dims), value

        # sent bytes
        for dims, value in df.groupby([
                'host', 'request_path_1', 'request_path_2', 'status',
        ]).bytes_sent.sum().items():
            yield self.metric_name('bytes_sent', dims), value

    def metric_name(self, *args):
        parts = self.prefix.split('.')
        for i in args:
            if isinstance(i, (list, tuple)):
                parts.extend(i)
            else:
                parts.append(i)
        return '.'.join(str(i).replace('.', '_') for i in parts)
