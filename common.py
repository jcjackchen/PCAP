#!/usr/bin/env python

import time
import threading
from scapy.all import *
import sys
import socket
import json
import Queue
import interfaces

maxhop = 25

# A request that will trigger the great firewall but will NOT cause
# the web server to process the connection.  You probably want it here

triggerfetch = "GET / HTTP/1.1\r\nHost:www.google.com\r\n\r\n"

# A couple useful functions that take scapy packets
def isRST(p):
    return (TCP in p) and (p[IP][TCP].flags & 0x4 != 0)

def isICMP(p):
    return ICMP in p

def isTimeExceeded(p):
    return ICMP in p and p[IP][ICMP].type == 11

# A general python object to handle a lot of this stuff...
#
# Use this to implement the actual functions you need.
class PacketUtils:
    def __init__(self, dst=None):
        # Get one's SRC IP & interface
        i = interfaces.interfaces()
        self.src = i[1][0]
        self.iface = i[0]
        self.netmask = i[1][1]
        self.enet = i[2]
        self.dst = dst
        sys.stderr.write("SIP IP %s, iface %s, netmask %s, enet %s\n" %
                         (self.src, self.iface, self.netmask, self.enet))
        # A queue where received packets go.  If it is full
        # packets are dropped.
        self.packetQueue = Queue.Queue(100000)
        self.dropCount = 0
        self.idcount = 0

        self.ethrdst = ""

        # Get the destination ethernet address with an ARP
        self.arp()
        
        # You can add other stuff in here to, e.g. keep track of
        # outstanding ports, etc.
        
        # Start the packet sniffer
        t = threading.Thread(target=self.run_sniffer)
        t.daemon = True
        t.start()
        time.sleep(.1)

    # generates an ARP request
    def arp(self):
        e = Ether(dst="ff:ff:ff:ff:ff:ff",
                  type=0x0806)
        gateway = ""
        srcs = self.src.split('.')
        netmask = self.netmask.split('.')
        for x in range(4):
            nm = int(netmask[x])
            addr = int(srcs[x])
            if x == 3:
                gateway += "%i" % ((addr & nm) + 1)
            else:
                gateway += ("%i" % (addr & nm)) + "."
        sys.stderr.write("Gateway %s\n" % gateway)
        a = ARP(hwsrc=self.enet,
                pdst=gateway)
        p = srp1([e/a], iface=self.iface, verbose=0)
        self.etherdst = p[Ether].src
        sys.stderr.write("Ethernet destination %s\n" % (self.etherdst))


    # A function to send an individual packet.
    def send_pkt(self, payload=None, ttl=32, flags="",
                 seq=None, ack=None,
                 sport=None, dport=80,ipid=None,
                 dip=None,debug=False):
        if sport == None:
            sport = random.randint(1024, 32000)
        if seq == None:
            seq = random.randint(1, 31313131)
        if ack == None:
            ack = random.randint(1, 31313131)
        if ipid == None:
            ipid = self.idcount
            self.idcount += 1
        t = TCP(sport=sport, dport=dport,
                flags=flags, seq=seq, ack=ack)
        ip = IP(src=self.src,
                dst=self.dst,
                id=ipid,
                ttl=ttl)
        p = ip/t
        if payload:
            p = ip/t/payload
        else:
            pass
        e = Ether(dst=self.etherdst,
                  type=0x0800)
        # Have to send as Ethernet to avoid interface issues
        sendp([e/p], verbose=1, iface=self.iface)
        # Limit to 20 PPS.
        time.sleep(.05)
        # And return the packet for reference
        return p


    # Has an automatic 5 second timeout.
    def get_pkt(self, timeout=5):
        try:
            return self.packetQueue.get(True, timeout)
        except Queue.Empty:
            return None

    # The function that actually does the sniffing
    def sniffer(self, packet):
        try:
            # non-blocking: if it fails, it fails
            self.packetQueue.put(packet, False)
        except Queue.Full:
            if self.dropCount % 1000 == 0:
                sys.stderr.write("*")
                sys.stderr.flush()
            self.dropCount += 1

    def run_sniffer(self):
        sys.stderr.write("Sniffer started\n")
        rule = "src net %s or icmp" % self.dst
        sys.stderr.write("Sniffer rule \"%s\"\n" % rule);
        sniff(prn=self.sniffer,
              filter=rule,
              iface=self.iface,
              store=0)

    # Sends the message to the target in such a way
    # that the target receives the msg without
    # interference by the Great Firewall.
    #
    # ttl is a ttl which triggers the Great Firewall but is before the
    # server itself (from a previous traceroute incantation
    def evade(self, target, msg, ttl):

        # Normal Handshake with the server
        p = self.send_pkt(None,64,"S",None)
        sport = p[TCP].sport
        q = self.get_pkt()

        # Re SYN with the server
        while(q == None or isICMP(q) or q[TCP].ack != p[TCP].seq+1):
            if (q == None):
                p = self.send_pkt(None,64,"S",None)
                sport = p[TCP].sport
                q = self.get_pkt()

        # SYN/ACK sanity check
        seq = q[TCP].seq
        ack = q[TCP].ack
        assert(p[TCP].seq+1 == q[TCP].ack)
        assert(q[TCP].flags == 18)

        # ACK the SYN/ACK
        p = self.send_pkt(None,64,"A",ack,seq+1,sport)

        msgs = msg.split(".") 
        for m in msgs:
            p = self.send_pkt(m+".",64,"PA",ack,seq+1,sport)
            ack += len(m) 

        p = self.send_pkt("\r\n",64,"PA",ack,seq+1,sport)
        q = self.get_pkt()
        pkts = []
        wait = 5
        while (q != None):
            pkts += [q]
            q = self.get_pkt(wait)
                
        loads = ""
        for pkt in pkts:
            if 'Raw' in pkt and pkt[TCP].ack == ack+1:
                loads += pkt[Raw].load
    
        return loads
        
    # Returns "DEAD" if server isn't alive,
    # "LIVE" if teh server is alive,
    # "FIREWALL" if it is behind the Great Firewall
    def ping(self, target):
        # self.send_msg([triggerfetch], dst=target, syn=True)
        sport = random.randint(2000, 32000)
        p = self.send_pkt(None, 64, "S", None, None, sport)
        q = self.get_pkt()
        if (q==None):
            return "DEAD"

        seq = q[TCP].seq
        ack = q[TCP].ack
        assert(p[TCP].seq+1 == q[TCP].ack)
        assert(q[TCP].flags == 18)

        p = self.send_pkt(None,64,"A",ack,seq+1, sport)
        p = self.send_pkt(triggerfetch, 64, "PA",ack,seq+1, sport)

        q = self.get_pkt()
        
        if (q == None):
            return "DEAD"
        elif(isRST(q)):
            return "FIREWALL"
    
        
        while(q != None):
            q = self.get_pkt()
            if (q == None):
                break
            elif(isRST(q)):
                return "FIREWALL"

        return "LIVE"

    # Format is
    # ([], [])
    # The first list is the list of IPs that have a hop
    # or none if none
    # The second list is T/F 
    # if there is a RST back for that particular request
    def traceroute(self, target, hops):
        ips = []
        rst = []

        ttl = 1
        while(ttl<=hops):

            # Normal Handshake with the server
            p = self.send_pkt(None,64,"S",None)
            sport = p[TCP].sport
            q = self.get_pkt()
                
            # Re SYN with the server
            while(q == None or isICMP(q) or q[TCP].ack != p[TCP].seq+1):
                if (q == None):
                    p = self.send_pkt(None,64,"S",None)
                    sport = p[TCP].sport
                q = self.get_pkt()

            # SYN/ACK sanity check    
            seq = q[TCP].seq
            ack = q[TCP].ack
            assert(p[TCP].seq+1 == q[TCP].ack)
            assert(q[TCP].flags == 18)
                    
            # ACK the SYN/ACK
            p = self.send_pkt(None,64,"A",ack,seq+1,sport)

            #Send 3 copies of the packet
            for i in range(3):
                p = self.send_pkt(triggerfetch,ttl,"PA",ack,seq+1,sport)

            #GET all the packets and EMPTY the Queue
            wait = 5
            pkts = []
            while(q != None):
                q = self.get_pkt(wait)
                if q != None:
                    pkts += [q]

                if wait != 0:
                    wait -= 1

            ip = None
            tf = False
            #Examine the received pkts
            for pkt in pkts:
                if isTimeExceeded(pkt) and ip == None:
                    ip = pkt[IP].src
                elif isRST(pkt):
                    tf = True

            ips += [ip]
            rst += [tf]

            #Empty the Queue again    
            while(q!=None):
                q = self.get_pkt(1)
                
            ttl += 1
        return (ips,rst)
