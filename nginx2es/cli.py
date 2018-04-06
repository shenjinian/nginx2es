#!/usr/bin/env python

import argparse
import io
import json
import logging
import socket
import sys

import dateutil

from arconfig import LoadConfigAction, GenConfigAction

from elasticsearch import Elasticsearch, ConnectionError

import entrypoints

from .parser import AccessLogParser
from .nginx2es import Nginx2ES
from .watcher import Watcher
from .mapping import DEFAULT_TEMPLATE


def geoip_error(msg):
    sys.stderr.write("can't load geoip database: %s\n" % msg)
    sys.exit(1)


def load_geoip(geoip, explicit):

    try:
        import GeoIP
        try:
            # Description from https://github.com/maxmind/geoip-api-c:
            #
            # * GEOIP_INDEX_CACHE - Cache only the the most frequently accessed
            # index portion of the database, resulting in faster lookups than
            # GEOIP_STANDARD, but less memory usage than GEOIP_MEMORY_CACHE.
            # This is useful for larger databases such as GeoIP Legacy
            # Organization and GeoIP Legacy City. Note: for GeoIP Legacy
            # Country, Region and Netspeed databases, GEOIP_INDEX_CACHE is
            # equivalent to GEOIP_MEMORY_CACHE.
            #
            # * GEOIP_CHECK_CACHE - Check for updated database. If database has
            # been updated, reload file handle and/or memory cache.
            flags = GeoIP.GEOIP_INDEX_CACHE | GeoIP.GEOIP_CHECK_CACHE
            return GeoIP.open(geoip, flags)
        except GeoIP.error as e:
            # if geoip was specified explicitly then the program should exit
            if explicit:
                geoip_error(e)
    except ImportError:
        if explicit:
            geoip_error("geoip module is not installed")
    return None


def check_template(es, name, template, force):
    if force or not es.indices.exists_template(name):
        if template is None:
            template = DEFAULT_TEMPLATE
        else:
            template = json.load(open(template))
        es.indices.put_template(name, DEFAULT_TEMPLATE)


def load_extensions(extensions):

    ret = []

    for ext_name in extensions:
        try:
            ext = entrypoints.get_single(
                "nginx2es.ext", ext_name)
        except entrypoints.NoSuchEntryPoint:
            raise ValueError(
                "%s not found in \"nginx2es.ext\" "
                "entrypoints" % ext_name
            )
        ret.append(ext.load())

    return ret


parser = argparse.ArgumentParser()
parser.add_argument("--config", action=LoadConfigAction)
parser.add_argument("--gen-config", action=GenConfigAction)
parser.add_argument("filename", nargs="?", default="/var/log/nginx/access.json")
parser.add_argument("--chunk-size", type=int, default=500, help="chunk size for bulk requests")
parser.add_argument("--elastic", action="append",
                    help="elasticsearch cluster address")
parser.add_argument("--min-timestamp",
                    help="skip records with timestamp less than specified")
parser.add_argument("--max-timestamp",
                    help="skip records with timestamp more than specified")
parser.add_argument("--force-create-template", action="store_true",
                    help="force create index template")
parser.add_argument("--geoip", help="GeoIP database file path")
parser.add_argument("--hostname", default=socket.gethostname(),
                    help="override hostname to add to documents")
parser.add_argument("--index", default="nginx-%Y.%m.%d",
                    help="index name strftime pattern")
parser.add_argument(
    "--max-delay", default=10., type=int,
    help="maximum time to wait before flush if count of records in buffer is "
         "less than chunk-size")
parser.add_argument(
    "--max-retries", default=3, type=int,
    help="maximum number of times a document will be retried when 429 is "
         "received, set to 0 for no retries on 429")
parser.add_argument("--mode", default="tail",
                    choices=["tail", "from-start", "one-shot"],
                    help="records read mode")
parser.add_argument("--ext", default=[], action="append",
                    help="add post-processing extension")
parser.add_argument("--template", help="index template filename")
parser.add_argument("--template-name", default="nginx",
                    help="template name to use for index template")
parser.add_argument("--carbon", help="carbon host:port to send http stats")
parser.add_argument("--carbon-interval", default=10, type=int,
                    help="carbon host:port to send http stats")
parser.add_argument("--carbon-delay", default=None, type=int,
                    help="stats delay (defaults to interval)")
parser.add_argument("--carbon-prefix",
                    help="carbon metrics prefix (default: nginx2es.$hostname")
parser.add_argument("--timeout", type=int, default=30,
                    help="elasticsearch request timeout")
parser.add_argument("--sentry", help="sentry dsn")
parser.add_argument("--stdout", action="store_true",
                    help="output to stdout instead of elasticsearch")
parser.add_argument("--log-format",
                    default="%(asctime)s %(levelname)s %(message)s",
                    help="log format")
parser.add_argument("--log-level", default="error", help="log level")


def main():

    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper(), format='%(asctime)s %(levelname)s %(message)s')

    if args.sentry is not None:
        import raven
        import raven.conf
        import raven.handlers.logging
        sentry = raven.Client(args.sentry)
        sentry_handler = raven.handlers.logging.SentryHandler(sentry)
        sentry_handler.setLevel(logging.ERROR)
        raven.conf.setup_logging(sentry_handler)

    es_kwargs = {'timeout': args.timeout}
    if args.elastic:
        es_kwargs['hosts'] = args.elastic
    if args.min_timestamp:
        es_kwargs['min_timestamp'] = dateutil.parser.parse(args.min_timestamp)
    if args.max_timestamp:
        es_kwargs['max_timestamp'] = dateutil.parser.parse(args.max_timestamp)
    es = Elasticsearch(**es_kwargs)

    if args.geoip is None:
        explicit_geoip = False
        args.geoip = "/usr/share/GeoIP/GeoIPCity.dat"
    else:
        explicit_geoip = True
    geoip = load_geoip(args.geoip, explicit_geoip)

    access_log_parser = AccessLogParser(
        args.hostname, geoip=geoip, extensions=load_extensions(args.ext),
    )

    if args.carbon:
        from nginx2es.stat import Stat
        if args.carbon_prefix is None:
            carbon_prefix = 'nginx2es.%s' % args.hostname
        stat_kwargs = {
            'prefix': carbon_prefix,
            'interval': args.carbon_interval,
        }
        if ':' in args.carbon:
            args.carbon, carbon_port = args.carbon.split(':')
            stat_kwargs['port'] = int(carbon_port)
        stat_kwargs['host'] = args.carbon
        if args.carbon_delay:
            stat_kwargs['delay'] = args.carbon_delay
        stat = Stat(**stat_kwargs)
        if args.stdout:
            stat.output = sys.stdout
        else:
            stat.connect()
        stat.start()
    else:
        stat = None

    nginx2es = Nginx2ES(es, access_log_parser, args.index,
                        stat=stat,
                        chunk_size=args.chunk_size,
                        max_retries=args.max_retries,
                        max_delay=args.max_delay)

    if args.stdout:
        run = nginx2es.stdout
    else:
        try:
            check_template(es, args.template_name, args.template, args.force_create_template)
        except ConnectionError as e:
            logging.error("can't connect to elasticsearch")
            sys.exit(1)
        run = nginx2es.run

    if args.filename == '-':
        f = io.TextIOWrapper(sys.stdin.buffer, errors='replace')
    else:
        f = open(args.filename, errors='replace')

    if not f.seekable():
        if '--mode' in sys.argv:
            logging.warning("using --mode argument while reading from stream is incorrect")
        run(f)
    elif args.mode == 'one-shot':
        run(f)
    else:
        f.close()
        from_start = (args.mode == 'from-start')
        run(Watcher(args.filename, from_start))


if __name__ == "__main__":
    main()
