﻿import sys
import time
import math
import json
import random
import controller.framework.fxlib as fxlib
from controller.framework.ControllerModule import ControllerModule


class BaseTopologyManager(ControllerModule):

    def __init__(self, CFxHandle, paramDict):

        super(BaseTopologyManager, self).__init__()
        self.CFxHandle = CFxHandle
        self.CMConfig = paramDict
        self.ipop_state = None
        self.interval_counter = 0
        self.cv_interval = 5
        self.use_visualizer = False

        self.uid = ""
        self.ip4 = ""
        self.local_state = {}

        # peers (linked nodes)
        self.peers = {}

        # links:
        #   self.links["successor"] = { uid: None }
        #   self.links["chord"]     = { uid: {"log_uid": log_uid, "ttl": ttl} }
        #   self.links["on_demand"] = { uid: {"ttl": ttl, "rate": rate} }
        #   self.links["inbound"]   = { uid: None }

        self.links = {
            "successor": {}, "chord": {}, "on_demand": {}, "inbound": {}
        }

        self.log_chords = []

        # discovered nodes
        #   self.discovered_nodes is the list of nodes used by the successors policy
        #   self.discovered_nodes_srv is the list of nodes obtained from peer_state
        #       notifications
        self.discovered_nodes = []
        self.discovered_nodes_srv = []

        # p2p overlay state
        self.p2p_state = "started"

        # address mapping
        self.uid_ip4_table = {}
        self.ip4_uid_table = {}

        # populate uid_ip4_table and ip4_uid_table with all UID and IPv4
        # mappings within the /16 subnet
        parts = self.CMConfig["ip4"].split(".")
        ip_prefix = parts[0] + "." + parts[1] + "."
        for i in range(0, 255):
            for j in range(0, 255):
                ip4 = ip_prefix + str(i) + "." + str(j)
                uid = fxlib.gen_uid(ip4)
                self.uid_ip4_table[uid] = ip4
                self.ip4_uid_table[ip4] = uid

        if 'use_central_visualizer' in self.CMConfig:
            self.use_visualizer = self.CMConfig["use_central_visualizer"]
        if "interval_central_visualizer" in self.CMConfig:
            self.cv_interval = self.CMConfig["interval_central_visualizer"]


    def initialize(self):
        self.registerCBT('Logger', 'info', "BaseTopologyManager Loaded")

    # self.CFxHandle.createCBT(...) and self.CFxHandle.submitCBT(...) mask
    def registerCBT(self, _recipient, _action, _data=''):
        cbt = self.CFxHandle.createCBT(
            initiator='BaseTopologyManager',
            recipient=_recipient,
            action=_action,
            data=_data
        )
        self.CFxHandle.submitCBT(cbt)

    ############################################################################
    # send message functions                                                   #
    ############################################################################

    # send message (through XMPP service)
    #   - msg_type = message type attribute
    #   - uid      = UID of the destination node
    #   - msg      = message
    def send_msg_srv(self, msg_type, uid, msg):
        cbtdata = {"method": msg_type, "overlay_id": 1, "uid": uid, "data": msg}
        self.registerCBT('TincanSender', 'DO_SEND_MSG', cbtdata)

    # send message (through ICC)
    #   - uid = UID of the destination peer (a tincan link must exist)
    #   - msg = message
    def send_msg_icc(self, uid, msg):
        if uid in self.peers:
            if "ip6" in self.peers[uid]:
                cbtdata = {
                    "icc_type": "control",
                    "src_uid": self.uid,
                    "dst_uid": uid,
                    "msg": msg
                }
                self.registerCBT('TincanSender', 'DO_SEND_ICC_MSG', cbtdata)

    ############################################################################
    # connectivity functions                                                   #
    ############################################################################

    # request connection
    #   send a connection request
    #   - con_type = {successor, chord, on_demand}
    #   - uid      = UID of the target node
    def request_connection(self, con_type, uid):

        # send connection request
        data = {
            "fpr": self.local_state["_fpr"],
            "con_type": con_type
        }

        self.send_msg_srv("con_req", uid, json.dumps(data))

        log = "sent CON_REQ (" + con_type + "): " + str(uid[0:3])
        self.registerCBT('Logger', 'info', log)

    # respond connection
    #   create connection and return a connection acknowledgement and response
    #   - uid  = UID of the target node
    #   - data = information necessary to establish a link
    def respond_connection(self, con_type, uid, data):
        self.create_connection(uid, data)

        # send con_ack message
        data = {
            "fpr": self.local_state["_fpr"],
            "con_type": con_type
        }

        self.send_msg_srv("con_ack", uid, json.dumps(data))

    # create connection
    #   establish a tincan link
    #   - uid  = UID of the target node
    #   - data = information necessary to establish a link
    def create_connection(self, uid, data):

        # FIXME check_collision was removed
        fpr_len = len(self.local_state["_fpr"])
        fpr = data[:fpr_len]
        nid = 1
        sec = self.CMConfig["sec"]
        cas = data[fpr_len + 1:]
        ip4 = self.uid_ip4_table[uid]

        con_dict = {'uid': uid, 'fpr': data, 'nid': nid, 'sec': sec, 'cas': cas}
        self.registerCBT('LinkManager', 'CREATE_LINK', con_dict)

        cbtdata = {"uid": uid, "ip4": ip4}
        self.registerCBT('TincanSender', 'DO_SET_REMOTE_IP', cbtdata)

    def linked(self, uid):
        if uid in self.peers:
#            if self.peers[uid]["con_status"] == "online":
            if "fpr" in self.peers[uid]:
                return True
        return False

    # remove connection
    #   remove a link by peer UID
    #   - uid = UID of the peer
    def remove_connection(self, uid):
        if uid in self.peers:
            self.registerCBT('LinkManager', 'TRIM_LINK', uid)
            del self.peers[uid]

            for con_type in ["successor", "chord", "on_demand", "inbound"]:
                if uid in self.links[con_type].keys():
                    del self.links[con_type][uid]

            log = "removed connection: " + str(uid[0:3])
            self.registerCBT('Logger', 'info', log)

    # clean connections
    #   remove peers with expired time-to-live attributes
    def clean_connections(self):
        # time-to-live attribute indicative of an offline link
        for uid in self.peers.keys():
            if time.time() > self.peers[uid]["ttl"]:
                self.remove_connection(uid)

        # periodically call policy for link removal
        self.clean_chord()
        self.clean_on_demand()

    ############################################################################
    # add/remove link functions                                                #
    ############################################################################

    # add outbound link
    def add_outbound_link(self, con_type, uid, attributes):

        # add peer to link type
        self.links[con_type][uid] = attributes

        # peer is not in the peers list
        if uid not in self.peers.keys():

            # add peer to peers list
            self.peers[uid] = {
                "uid": uid,
                "ttl": time.time() + self.CMConfig["ttl_link_initial"],
                "con_status": "unknown"
            }

            # request connection
            self.request_connection(con_type, uid)

        else:

            # forward mark_inbound message
            new_msg = {
                "msg_type": "mark_inbound",
                "src_uid": self.uid,
                "dst_uid": uid
            }

            self.send_msg_icc(uid, new_msg)

    # add inbound link
    def add_inbound_link(self, con_type, uid, fpr):

        # add peer in inbound links
        self.links["inbound"][uid] = None

        # peer is not in the peers list
        if uid not in self.peers.keys():

            self.peers[uid] = {
                "uid": uid,
                "ttl": time.time() + self.CMConfig["ttl_link_initial"],
                "con_status": "unknown"
            }

            self.respond_connection(con_type, uid, fpr)

    # remove link
    def remove_link(self, con_type, uid):

        # remove peer from link type
        if uid in self.links[con_type].keys():
            del self.links[con_type][uid]

        # this peer does not have any outbound links
        if uid not in (self.links["successor"].keys() + \
                       self.links["chord"].keys() + \
                       self.links["on_demand"].keys()):

            # this peer does not have any inbound links
            if uid not in self.links["inbound"].keys():

                # remove connection
                self.remove_connection(uid)

            # this peer has inbound links
            else:

                # forward unmark_inbound message
                new_msg = {
                    "msg_type": "unmark_inbound",
                    "src_uid": self.uid,
                    "dst_uid": uid
                }

                self.send_msg_icc(uid, new_msg)

    ############################################################################
    # packet forwarding policy                                                 #
    ############################################################################

    # closer function
    #   tests if uid is successively closer to uid_B than uid_A
    def closer(self, uid_A, uid, uid_B):
        if (uid_A < uid_B) and ((uid_A < uid) and (uid <= uid_B)):
            return True  #0---A===B---N
        elif (uid_A > uid_B) and ((uid_A < uid) or (uid <= uid_B)):
            return True  #0===B---A===N
        return False

    # forward packet
    #   forward a packet across ICC
    #   - fwd_type = {
    #       exact   = intended specifically to the destination node,
    #       closest = intended to the node closest to the designated node
    #     }
    #   - dst_uid  = UID of the destination or designated node
    #   - msg      = message in transit
    #   returns true if this packet is intended for the calling node
    def forward_msg(self, fwd_type, dst_uid, msg):

        # find peer that is successively closest to and less-than-or-equal-to
        # the designated UID
        nxt_uid = self.uid
        for peer in self.peers.keys():
            if self.linked(peer):
                if self.closer(nxt_uid, peer, dst_uid):
                    nxt_uid = peer

        # packet is intended specifically to the destination node
        if fwd_type == "exact":

            # this is the destination uid
            if self.uid == dst_uid:
                return True

            # this is the closest node but not the destination; drop packet
            elif self.uid == nxt_uid:
                return False

        # packet is intended to the node closest to the designated node
        elif fwd_type == "closest":

            # this is the destination uid or the node closest to it
            if self.uid == nxt_uid:
                return True

        # there is a closer node; forward packet to the next node
        self.send_msg_icc(nxt_uid, msg)
        return False

    ############################################################################
    # successors policy                                                        #
    ############################################################################
    # [1] A discovers nodes in the network
    #     A requests to link to the closest successive node B as A's successor
    # [2] B accepts A's link request, with A as B's inbound link
    #     B responds to link to A
    # [3] A and B are connected
    # [*] the link is terminated when A discovers and links to closer successive
    #     nodes, or the link disconnects
    # [*] A periodically advertises its peer list to its peers to help them
    #     discover nodes

    def add_successors(self):

        # sort nodes into rotary, unique list with respect to this UID
        nodes = sorted(set(self.links["successor"].keys() + self.discovered_nodes))
        if self.uid in nodes:
            nodes.remove(self.uid)
        if max([self.uid] + nodes) != self.uid:
            while nodes[0] < self.uid:
                nodes.append(nodes.pop(0))

        # link to the closest <num_successors> nodes (if not already linked)
        for node in nodes[0:min(len(nodes), self.CMConfig["num_successors"])]:
            if node not in self.links["successor"].keys():
                self.add_outbound_link("successor", node, None)

        # reset list of discovered nodes
        del self.discovered_nodes[:]

    def remove_successors(self):

        # sort nodes into rotary, unique list with respect to this UID
        successors = sorted(self.links["successor"].keys())
        if max([self.uid] + successors) != self.uid:
            while successors[0] < self.uid:
                successors.append(successors.pop(0))

        # remove all linked successors not within the closest <num_successors> linked nodes
        # remove all unlinked successors not within the closest <num_successors> nodes
        num_linked_successors = 0
        for successor in successors:
            if self.linked(successor):
                num_linked_successors += 1

                # remove excess successors
                if num_linked_successors > self.CMConfig["num_successors"]:
                    self.remove_link("successor", successor)

    def advertise(self):
        # create list of linked peers
        peer_list = []
        for peer in self.peers.keys():
            if self.linked(peer):
                peer_list.append(peer)

        # send peer list advertisement to all peers
        new_msg = {
            "msg_type": "advertise",
            "src_uid": self.uid,
            "peer_list": peer_list
        }

        for peer in self.peers.keys():
            if self.linked(peer):
                self.send_msg_icc(peer, new_msg)

    ############################################################################
    # chords policy                                                            #
    ############################################################################
    # [1] A forwards a headless find_chord message approximated by a designated UID
    # [2] B discovers that it is the closest node to the designated UID
    #     B responds with a found_chord message to A
    # [3] A requests to link to B as A's chord
    # [4] B accepts A's link request, with A as B's inbound link
    #     B responds to link to A
    # [5] A and B are connected
    # [*] the link is terminated when the chord time-to-live attribute expires and
    #     a better chord was found or the link disconnects

    def find_chords(self):

        # find chords closest to the approximate logarithmic nodes 
        if len(self.log_chords) == 0:
            for i in reversed(range(self.CMConfig["num_chords"])):
                # log_uid = [self_uid + 2**(n-1-i)] % 2**n
                log_num = (int(self.uid, 16) + int(math.pow(2, 160-1-i))) % int(math.pow(2, 160))
                log_uid = "{0:040x}".format(log_num)
                self.log_chords.append(log_uid)

        # determine list of designated UIDs
        log_chords = self.log_chords
        for chord in self.links["chord"].values():
            if chord["log_uid"] in log_chords:
                log_chords.remove(chord["log_uid"])

        # forward find_chord messages to the nodes closest to the designated UID
        for log_uid in log_chords:

            # forward find_chord message
            new_msg = {
                "msg_type": "find_chord",
                "src_uid": self.uid,
                "dst_uid": log_uid,
                "log_uid": log_uid
            }

            self.forward_msg("closest", log_uid, new_msg)

    def add_chord(self, uid, log_uid):

        # if a chord associated with log_uid already exists, check if the found
        # chord is the same chord:
        # if they are the same then the chord is already the best one available
        # otherwise, remove the chord and link to the found chord
        for chord in self.links["chord"].keys():
            if self.links["chord"][chord]["log_uid"] == log_uid:
                if chord == uid:
                    return
                else:
                    self.remove_link("chord", chord)

        # add chord link
        attributes = {
            "log_uid": log_uid,
            "ttl": time.time() + self.CMConfig["ttl_chord"]
        }

        self.add_outbound_link("chord", uid, attributes)

    def clean_chord(self):

        if not self.links["chord"].keys():
            return

        # find chord with the oldest time-to-live attribute
        uid = min(self.links["chord"].keys(), key=lambda u: (self.links["chord"][u]["ttl"]))

        # time-to-live attribute has expired: determine if a better chord exists
        if time.time() > self.links["chord"][uid]["ttl"]:

            # forward find_chord message
            new_msg = {
                "msg_type": "find_chord",
                "src_uid": self.uid,
                "dst_uid": self.links["chord"][uid]["log_uid"],
                "log_uid": self.links["chord"][uid]["log_uid"]
            }

            self.forward_msg("closest", self.links["chord"][uid]["log_uid"], new_msg)

            # extend time-to-live attribute
            self.links["chord"][uid]["ttl"] = time.time() + self.CMConfig["ttl_chord"]

    ############################################################################
    # on-demand links policy                                                   #
    ############################################################################
    # [1] A is forwarding packets to B
    #     A immediately requests to link to B, with B as A's on-demand link
    # [2] B accepts A's link request, with A as B's inbound link
    # [3] A and B are connected
    #     B responds to link to A
    # [*] the link is terminated when the transfer rate is below some threshold
    #     until the on-demand time-to-live attribute expires or the link
    #     disconnections

    def add_on_demand(self, uid):

        if len(self.links["on_demand"].keys()) < self.CMConfig["num_on_demand"]:

            if uid not in self.links["on_demand"].keys():

                # add on-demand link
                attributes = {
                    "ttl": time.time() + self.CMConfig["ttl_on_demand"],
                    "rate": 0
                }
                self.add_outbound_link("on_demand", uid, attributes)

    def clean_on_demand(self):
        for uid in self.links["on_demand"].keys():

            # rate exceeds threshold: increase time-to-live attribute
            if self.links["on_demand"][uid]["rate"] >= self.CMConfig["threshold_on_demand"]:
                self.links["on_demand"][uid]["ttl"] = time.time() + self.CMConfig["ttl_on_demand"]

            # rate is below theshold and the time-to-live attribute expired: remove link
            elif time.time() > self.links["on_demand"][uid]["ttl"]:
                self.remove_link("on_demand", uid)

    ############################################################################
    # inbound links policy                                                     #
    ############################################################################

    def add_inbound(self, con_type, uid, fpr):

        if uid not in self.links["inbound"].keys():

            if con_type == "successor":
                self.add_inbound_link(con_type, uid, fpr)

            elif con_type in ["chord", "on_demand"]:
                if len(self.links["inbound"].keys()) < self.CMConfig["num_inbound"]:
                    self.add_inbound_link(con_type, uid, fpr)

    ############################################################################
    # service notifications                                                    #
    ############################################################################

    def processCBT(self, cbt):

        # tincan control messages
        if(cbt.action == "TINCAN_MSG"):
            msg = cbt.data
            msg_type = msg.get("type", None)

            # update local state
            if msg_type == "local_state":
                self.local_state = msg
                self.uid = msg["_uid"]
                self.ip4 = msg["_ip4"]

            # update peer list
            elif msg_type == "peer_state":
                self.discovered_nodes_srv.append(msg["uid"])

                if msg["uid"] in self.peers:
                    # preserve ttl and con_status attributes
                    ttl = self.peers[msg["uid"]]["ttl"]
                    con_status = self.peers[msg["uid"]]["con_status"]

                    # update ttl attribute
                    if "online" == msg["status"]:
                        ttl = time.time() + self.CMConfig["ttl_link_pulse"]

                    # update peer state
                    self.peers[msg["uid"]] = msg
                    self.peers[msg["uid"]]["ttl"] = ttl
                    self.peers[msg["uid"]]["con_status"] = con_status

                    if msg["uid"] in self.links["on_demand"].keys():
                        if "stats" in msg:
                            self.links["on_demand"][msg["uid"]]["rate"] = msg["stats"][0]["sent_bytes_second"]

            elif msg_type == "con_stat":

                if msg["uid"] in self.peers:
                    self.peers[msg["uid"]]["con_status"] = msg["data"]

            # handle connection request
            elif msg_type == "con_req":

                msg["data"] = json.loads(msg["data"])

                log = "recv con_req (" + msg["data"]["con_type"] + "): " + str(msg["uid"][0:3])
                self.registerCBT('Logger', 'info', log)

                self.add_inbound(msg["data"]["con_type"], msg["uid"],
                                    msg["data"]["fpr"])

            # handle connection acknowledgement
            elif msg_type == "con_ack":
                msg["data"] = json.loads(msg["data"])

                log = "recv con_ack (" + msg["data"]["con_type"] + "): " + str(msg["uid"][0:3])
                self.registerCBT('Logger', 'info', log)

                self.create_connection(msg["uid"], msg["data"]["fpr"])

            # handle connection response
            elif msg_type == "con_resp":
                self.create_connection(msg["uid"], msg["data"])

                log = "recv con_resp: " + str(msg["uid"][0:3])
                self.registerCBT('Logger', 'info', log)

            # handle ping message
            elif msg_type == "ping":

                # add source node to the list of discovered nodes
                self.discovered_nodes.append(msg["uid"])

                # reply with a ping response message
                self.send_msg_srv("ping_resp", msg["uid"], self.uid)

                log = "recv ping: " + str(msg["uid"][0:3])
                self.registerCBT('Logger', 'info', log)

            # handle ping response
            elif msg_type == "ping_resp":

                # add source node to the list of discovered nodes
                self.discovered_nodes.append(msg["uid"])

                log = "recv ping_resp: " + str(msg["uid"][0:3])
                self.registerCBT('Logger', 'info', log)

        # handle and forward tincan data packets
        elif(cbt.action == "TINCAN_PACKET"):

            data = cbt.data

            # ignore packets when not connected to the overlay and ipv6 packets
            if self.p2p_state != "connected" or data[54:56] == "\x86\xdd":
                return

            # extract packet
            pkt = data.encode("hex")

            # extract the source uid and destination uid
            # XXX src_uid and dst_uid should be obtained from the header, but
            # sometimes the dst_uid is the null uid
            # FIXME sometimes an irrelevant ip4 address obtained (i.e. 65.242.74.60)
            src_ip4 = '.'.join(str(int(i, 16)) for i in [pkt[132:140][i:i+2] for i in range(0, 8, 2)])
            dst_ip4 = '.'.join(str(int(i, 16)) for i in [pkt[140:148][i:i+2] for i in range(0, 8, 2)])

            try:
                src_uid = self.ip4_uid_table[src_ip4]
                dst_uid = self.ip4_uid_table[dst_ip4]
            except KeyError: # FIXME
                return

            # send forwarded message
            new_msg = {
                "msg_type": "forward",
                "src_uid": src_uid,
                "dst_uid": dst_uid,
                "packet": pkt
            }

            self.forward_msg("exact", dst_uid, new_msg)

            log = "sent tincan_packet (exact): " + str(dst_uid[0:3])
            self.registerCBT('Logger', 'info', log)

            # add on-demand link
            self.add_on_demand(dst_uid)

        # inter-controller communication (ICC) messages
        elif(cbt.action == "ICC_MSG"):
            msg = cbt.data
            msg_type = msg.get("msg_type", None)

            # advertisement of nearby nodes
            if msg_type == "advertise":
                self.discovered_nodes = list(set(self.discovered_nodes + msg["peer_list"]))

                log = "recv advertisement: " + str(msg["src_uid"][0:3])
                self.registerCBT('Logger', 'info', log)

            # handle forward packet
            elif msg_type == "forward":

                if self.forward_msg("exact", msg["dst_uid"], msg):
                    self.registerCBT('TincanSender', 'DO_INSERT_DATA_PACKET', msg["packet"])

                    log = "recv tincan_packet: " + str(msg["src_uid"][0:3])
                    self.registerCBT('Logger', 'info', log)

            # handle mark inbound
            elif msg_type == "mark_inbound":

                log = "recv mark_inbound: " + str(msg["src_uid"][0:3])
                self.registerCBT('Logger', 'info', log)

                if msg["src_uid"] not in self.links["inbound"].keys():
                    self.add_inbound_link("inbound", msg["src_uid"], None)

            # handle unmark inbound
            elif msg_type == "unmark_inbound":

                log = "recv unmark_inbound: " + str(msg["src_uid"][0:3])
                self.registerCBT('Logger', 'info', log)

                if msg["src_uid"] not in self.links["inbound"].keys():
                    self.remove_link("inbound", msg["src_uid"])

            # handle find chord
            elif msg_type == "find_chord":

                if self.forward_msg("closest", msg["dst_uid"], msg):

                    # forward found_chord message
                    new_msg = {
                        "msg_type": "found_chord",
                        "src_uid": self.uid,
                        "dst_uid": msg["src_uid"],
                        "log_uid": msg["log_uid"]
                    }

                    self.forward_msg("exact", msg["src_uid"], new_msg)

            # handle found chord
            elif msg_type == "found_chord":

                if self.forward_msg("closest", msg["dst_uid"], msg):

                    self.add_chord(msg["src_uid"], msg["log_uid"])

    ############################################################################
    # manage topology                                                          #
    ############################################################################

    def ping(self):

        # send up to <num_pings> ping messages to random nodes to test if the
        # node is available
        rand_list = random.sample(range(0, len(self.discovered_nodes_srv)), 
                min(len(self.discovered_nodes_srv), self.CMConfig["num_pings"]))

        for i in rand_list:
            self.send_msg_srv("ping", self.discovered_nodes_srv[i], self.uid)

        # reset list of discovered nodes (from XMPP)
        del self.discovered_nodes_srv[:]

    def manage_topology(self):

        # obtain local state
        if self.p2p_state == "started":
            if not self.local_state:
                self.registerCBT('Logger', 'info', "p2p state: started")
                return
            else:
                self.p2p_state = "searching"
                log = "identified local state: " + str(self.local_state["_uid"][0:3])
                self.registerCBT('Logger', 'info', log)

        # discover nodes (from XMPP)
        if self.p2p_state == "searching":
            if not self.discovered_nodes_srv:
                self.registerCBT('Logger', 'info', "p2p state: searching")
                return
            else:
                self.p2p_state = "connecting"
                self.discovered_nodes = self.discovered_nodes_srv

        # connecting to the peer-to-peer network
        if self.p2p_state == "connecting":

            # if there are no discovered nodes, ping nodes
            if not self.peers and not self.discovered_nodes:
                self.ping()
                return

            log = "discovered nodes: " + str(self.discovered_nodes)
            self.registerCBT('Logger', 'info', log)

            # trim offline connections
            self.clean_connections()

            # attempt to bootstrap
            self.add_successors()

            # wait until connected
            for peer in self.peers.keys():
                if self.linked(peer):
                    self.p2p_state = "connected"
                    break

        # connecting or connected to the IPOP peer-to-peer network; manage local topology
        if self.p2p_state == "connected":

            # trim offline connections
            self.clean_connections()

            # manage successors
            self.add_successors()
            self.remove_successors()

            # manage chords
            self.find_chords()

            # create advertisements
            self.advertise()

            if not self.peers:
                self.p2p_state = "connecting"
                self.registerCBT('Logger', 'info', "p2p state: DISCONNECTED")
            else:
                self.registerCBT('Logger', 'info', "p2p state: CONNECTED")

    def timer_method(self):

        self.interval_counter += 1

        # every <interval_management> seconds
        if self.interval_counter % self.CMConfig["interval_management"] == 0:

            # manage topology
            self.manage_topology()

            # update local state and update the list of discovered nodes (from XMPP)
            self.registerCBT('TincanSender', 'DO_GET_STATE', '')

        # every <interval_ping> seconds
        if self.interval_counter % self.CMConfig["interval_ping"] == 0:

            # ping to repair potential network partitions
            self.ping()

        # every <interval_central_visualizer> seconds
        if self.use_visualizer and self.interval_counter % self.cv_interval == 0:
            # send information to central visualizer
            if self.p2p_state != "started":
                self.visual_debugger()

    def terminate(self):
        pass

    # visual debugger
    #   send information to the central visualizer
    def visual_debugger(self):

        # list only connected links
        new_msg = {
            "uid": self.uid,
            "successor": [],
            "chord": [],
            "on_demand": [],
            "inbound": []
        }

        for con_type in ["successor", "chord", "on_demand", "inbound"]:
            for peer in self.links[con_type].keys():
                if self.linked(peer):
                    new_msg[con_type].append(peer)

        self.registerCBT('CentralVisualizer', 'SEND_INFO', new_msg)

