#!/usr/bin/python2
# -*- encoding: utf-8 -*-

import time
from datetime import datetime
import argparse
import sys
import os
from scapy.all import *
import sqlite3
import netaddr
import base64
from radiotap import radiotap_parse
from lru import LRU

NAME = 'probemon'
DESCRIPTION = "a command line tool for logging 802.11 probe request frames"
VERSION = '0.2'

mac_cache = LRU(128)
ssid_cache = LRU(128)
vendor_cache = LRU(128)

# read config variable from config.txt file
with open('config.txt') as f:
    exec('\n'.join(f.readlines()))

def insert_into_db(fields, db):
    date, mac, vendor, ssid, rssi = fields
    conn = sqlite3.connect(db)
    c = conn.cursor()

    try:
        vendor_id = vendor_cache[vendor]
    except KeyError:
        c.execute('select id from vendor where name=?', (vendor,))
        row = c.fetchone()
        if row is None:
            c.execute('insert into vendor (name) values(?)', (vendor,))
            c.execute('select id from vendor where name=?', (vendor,))
            row = c.fetchone()
        vendor_id = row[0]
        vendor_cache[vendor] = vendor_id

    try:
        mac_id = mac_cache[mac]
    except KeyError:
        c.execute('select id from mac where address=?', (mac,))
        row = c.fetchone()
        if row is None:
            c.execute('insert into mac (address,vendor) values(?, ?)', (mac, vendor_id))
            c.execute('select id from mac where address=?', (mac,))
            row = c.fetchone()
        mac_id = row[0]
        mac_cache[mac] = mac_id

    try:
        ssid_id = ssid_cache[ssid]
    except KeyError:
        c.execute('select id from ssid where name=?', (ssid,))
        row = c.fetchone()
        if row is None:
            c.execute('insert into ssid (name) values(?)', (ssid,))
            c.execute('select id from ssid where name=?', (ssid,))
            row = c.fetchone()
        ssid_id = row[0]
        ssid_cache[ssid] = ssid_id

    c.execute('insert into probemon values(?, ?, ?, ?)', (date, mac_id, ssid_id, rssi))

    try:
        conn.commit()
    except sqlite3.OperationalError as e:
        # db is locked ? Retry again
        time.sleep(10)
        conn.commit()
    conn.close()

def build_packet_cb(db, stdout, ignored):
    def packet_callback(packet):
        now = time.time()
        # look up vendor from OUI value in MAC address
        try:
            parsed_mac = netaddr.EUI(packet.addr2)
            vendor = parsed_mac.oui.registration().org
        except netaddr.core.NotRegisteredError, e:
            vendor = u'UNKNOWN'
        except IndexError:
            vendor = u'UNKNOWN'

        # parse radiotap headers to get RSSI value
        offset, headers = radiotap_parse(str(packet))
        rssi = headers['dbm_antsignal']

        try:
            ssid = packet.info.decode('utf-8')
        except UnicodeDecodeError:
            # encode the SSID in base64 because it will fail
            # to be inserted into the db otherwise
            ssid = u'b64_%s' % base64.b64encode(packet.info)
        fields = [now, packet.addr2, vendor, ssid, rssi]

        if packet.addr2 not in ignored:
            insert_into_db(fields, db)

            if stdout:
                # convert time to iso
                fields[0] = str(datetime.fromtimestamp(now))[:-3].replace(' ','T')
                print '\t'.join(str(i) for i in fields)

    return packet_callback

def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('-c', '--channel', default=1, type=int, help="the channel to listen on")
    parser.add_argument('-d', '--db', default='probemon.db', help="database file name to use")
    parser.add_argument('-i', '--interface', required=True, help="the capture interface to use")
    parser.add_argument('-I', '--ignore', action='append', help="mac address to ignore")
    parser.add_argument('-s', '--stdout', action='store_true', default=False, help="also log probe request to stdout")
    args = parser.parse_args()

    global IGNORED
    if args.ignore is not None:
        IGNORED = args.ignore

    conn = sqlite3.connect(args.db)
    c = conn.cursor()
    # create tables if they do not exist
    sql = 'create table if not exists vendor(id integer not null primary key, name text)'
    c.execute(sql)
    sql = '''create table if not exists mac(id integer not null primary key, address text,
        vendor integer,
        foreign key(vendor) references vendor(id)
        )'''
    c.execute(sql)
    sql = 'create table if not exists ssid(id integer not null primary key, name text)'
    c.execute(sql)
    sql = '''create table if not exists probemon(date float,
        mac integer,
        ssid integer,
        rssi integer,
        foreign key(mac) references mac(id),
        foreign key(ssid) references ssid(id)
        )'''
    c.execute(sql)
    conn.commit()
    conn.close()

    # sniff on specified channel
    os.system('iwconfig %s channel %d' % (args.interface, args.channel))

    print ":: Started listening to probe requests on channel %d on interface %s" % (args.channel, args.interface)
    sniff(iface=args.interface, prn=build_packet_cb(args.db, args.stdout, IGNORED),
        store=0, lfilter=lambda x:x.haslayer(Dot11ProbeReq))

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass

# vim: set et ts=4 sw=4:
