from protocol import proto_store
from urlparse import urlparse
import constants
import datastore
import hashlib
import json
import logging
import os
import routingtable
import time


class DHT(object):
    def __init__(self, transport, market_id, settings, db_connection):

        self._log = logging.getLogger('[%s] %s' % (market_id,
                                                   self.__class__.__name__))
        self._settings = settings
        self._knownNodes = []
        self._searches = []
        self._search_keys = {}
        self._activePeers = []
        self._republishThreads = []
        self._transport = transport
        self._market_id = market_id

        # Routing table
        self._routingTable = routingtable.OptimizedTreeRoutingTable(
            self._settings['guid'], market_id)
        self._dataStore = datastore.SqliteDataStore(db_connection)

    def getActivePeers(self):
        return self._activePeers

    def start(self, seed_peer):
        """ This method executes only when the server is starting up for the
            first time and add the seed peer(s) to known node list and
            active peer list. It then executes a findNode against the network
            for itself to refresh buckets.

        :param seed_peer: (CryptoPeerConnection) for seed peer
        """
        ip = seed_peer._ip
        port = seed_peer._port
        self.add_known_node((ip, port, seed_peer._guid, seed_peer._nickname))

        self._log.debug('Starting Seed Peer: %s' % seed_peer._nickname)
        self.add_peer(self._transport,
                      seed_peer._address,
                      seed_peer._pub,
                      seed_peer._guid,
                      seed_peer._nickname)

        self._iterativeFind(self._settings['guid'], self._knownNodes,
                            'findNode')

    def find_active_peer(self, uri, pubkey=None, guid=None, nickname=None):
        found_peer = False
        for idx, peer in enumerate(self._activePeers):
            if (guid, uri, pubkey, nickname) == (peer._guid, peer._address, peer._pub, peer._nickname):
                found_peer = peer
        return found_peer

    def remove_active_peer(self, uri):
        for idx, peer in enumerate(self._activePeers):
            if uri == peer._address:
                self._activePeers[idx].cleanup_context()
                del self._activePeers[idx]

    def add_seed(self, transport, uri):

        new_peer = self._transport.get_crypto_peer(uri=uri)
        self._log.debug(new_peer)

        def start_handshake_cb():
            self._knownNodes.append((urlparse(uri).hostname,
                                     urlparse(uri).port,
                                     new_peer._guid))
            self._log.debug('Known Nodes: %s' % self._knownNodes)

        new_peer.start_handshake(start_handshake_cb)

    def add_peer(self, transport, uri, pubkey=None, guid=None, nickname=None):
        """ This takes a tuple (pubkey, URI, guid) and adds it to the active
        peers list if it doesn't already reside there.

        :param transport: (CryptoTransportLayer) so we can get a new CryptoPeer
        """

        assert(uri)

        if uri is not None:

            peer_tuple = (uri, pubkey, guid, nickname)

            for idx, peer in enumerate(self._activePeers):

                active_peer_tuple = (peer._pub, peer._address, peer._guid, peer._nickname)

                if active_peer_tuple == peer_tuple:

                    old_peer = self._routingTable.getContact(guid)

                    if old_peer and (old_peer._address != uri or old_peer._pub != pubkey):
                        self._routingTable.removeContact(guid)
                        self._routingTable.addContact(peer)

                    self._log.info('Already in active peer list')
                    return
                else:
                    if peer._guid == guid or peer._address == uri:
                        self._log.debug('Partial Match')
                        # Update peer
                        peer._guid = guid
                        peer._address = uri
                        peer._pub = pubkey
                        peer._nickname = nickname
                        self._activePeers[idx] = peer
                        self._routingTable.removeContact(guid)
                        self._routingTable.addContact(peer)

                        return

            self._log.debug('New Peer')

            new_peer = self._transport.get_crypto_peer(guid, uri, pubkey, nickname)

            def cb():
                self._log.debug('Back from handshake')
                self._routingTable.removeContact(new_peer._guid)
                self._routingTable.addContact(new_peer)
                self._knownNodes.append((urlparse(uri).hostname, urlparse(uri).port, new_peer._guid))
                self._transport.save_peer_to_db(peer_tuple)

            if new_peer.check_port():
                new_peer.start_handshake(handshake_cb=cb)

        else:
            self._log.debug('Missing peer attributes')

    def add_known_node(self, node):
        """ Accept a peer tuple and add it to known nodes list
        :param node: (tuple)
        :return: N/A
        """
        if node not in self._knownNodes:
            self._knownNodes.append(node)

    def get_known_nodes(self):
        """ Get known nodes list and return it
        :return: (list)
        """
        return self._knownNodes

    def on_find_node(self, msg):
        """ When a findNode message is received it will be of several types:
        - findValue: Looking for a specific key-value
        - findNode: Looking for a node with a key

        If you find the key-value pair you send back the value in the foundKey
        field.

        If you find the node then send back the exact node and if you don't
        send back a list of k closest nodes in the foundNodes field.

        :param msg: Incoming message from other node with findNode request
        :return: N/A
        """

        self._log.debug('Received a findNode request: %s' % msg)

        guid = msg['senderGUID']
        key = msg['key']
        findID = msg['findID']
        uri = msg['uri']
        pubkey = msg['pubkey']
        # nick = msg['senderNick']

        assert guid is not None and guid != self._transport.guid
        assert key is not None
        assert findID is not None
        assert uri is not None
        assert pubkey is not None

        new_peer = self._routingTable.getContact(guid)

        if new_peer is not None:

            if msg['findValue'] is True:
                if key in self._dataStore and self._dataStore[key] is not None:

                    # Found key in local data store
                    new_peer.send(
                        {"type": "findNodeResponse",
                         "senderGUID": self._transport.guid,
                         "uri": self._transport._uri,
                         "pubkey": self._transport.pubkey,
                         "foundKey": self._dataStore[key],
                         "senderNick": self._transport._nickname,
                         "findID": findID})
                else:
                    self._log.info('Did not find a key: %s' % key)
                    contacts = self.close_nodes(key, guid)
                    self._log.info('Sending found nodes to: %s' % guid)

                    new_peer.send(
                        {"type": "findNodeResponse",
                         "senderGUID": self._transport.guid,
                         "senderNick": self._transport._nickname,
                         "uri": self._transport._uri,
                         "pubkey": self._transport.pubkey,
                         "foundNodes": contacts,
                         "findID": findID})

            else:
                # Search for contact in routing table
                foundContact = self._routingTable.getContact(key)

                if foundContact:
                    self._log.info('Found the node')
                    foundNode = (foundContact._guid,
                                 foundContact._address,
                                 foundContact._pub)
                    new_peer.send(
                        {"type": "findNodeResponse",
                         "senderGUID": self._transport.guid,
                         "senderNick": self._transport._nickname,
                         "uri": self._transport._uri,
                         "pubkey": self._transport.pubkey,
                         "foundNode": foundNode,
                         "findID": findID})
                else:

                    contacts = self.close_nodes(key, guid)
                    self._log.info('Sending found nodes to: %s' % guid)

                    new_peer.send(
                        {"type": "findNodeResponse",
                         "senderGUID": self._transport.guid,
                         "senderNick": self._transport._nickname,
                         "uri": self._transport._uri,
                         "pubkey": self._transport.pubkey,
                         "foundNodes": contacts,
                         "findID": findID})

            if new_peer is None or new_peer._address != uri:
                new_peer._address = uri
                self._routingTable.removeContact(new_peer._guid)
                self._routingTable.addContact(new_peer)

    def close_nodes(self, key, guid):
        contacts = self._routingTable.findCloseNodes(key, constants.k, guid)
        contactTriples = []
        for contact in contacts:
            contactTriples.append((contact._guid, contact._address, contact._pub, contact._nickname))

        return self.dedupe(contactTriples)

    def on_findNodeResponse(self, transport, msg):

        # self._log.info('Received a findNode Response: %s' % msg)

        # Update pubkey if necessary - happens for seed server
        # localPeer = next((peer for peer in self._activePeers if peer._guid == msg['senderGUID']), None)

        # Update existing peer's pubkey if active peer
        for idx, peer in enumerate(self._activePeers):
            if peer._guid == msg['senderGUID']:
                peer._nickname = msg['senderNick']
                peer._pub = msg['pubkey']
                self._activePeers[idx] = peer

        # If key was found by this node then
        if 'foundKey' in msg.keys():
            self._log.debug('Found the key-value pair. Executing callback.')

            for idx, s in enumerate(self._searches):
                if s._findID == msg['findID']:
                    s._callback(msg['foundKey'])
                    del self._searches[idx]
                    # self._searches[msg['findID']]._callback(msg['foundKey'])

                    # Remove active search

        else:

            if 'foundNode' in msg.keys():

                foundNode = msg['foundNode']
                self._log.debug('Found the node you were looking for: %s' % foundNode)

                # Add foundNode to active peers list and routing table
                if foundNode[2] != self._transport._guid:
                    self._log.debug('Found a tuple %s' % foundNode)
                    if len(foundNode) == 3:
                        foundNode.append('')
                    self.add_peer(self._transport, foundNode[1], foundNode[2], foundNode[0], foundNode[3])

                for idx, search in enumerate(self._searches):
                    if search._findID == msg['findID']:

                        # Execute callback
                        if search._callback is not None:
                            search._callback((foundNode[2], foundNode[1], foundNode[0], foundNode[3]))

                        # Clear search
                        del self._searches[idx]

            else:

                # Add any close nodes found to the shortlist
                # self.extendShortlist(transport, msg['findID'], msg['foundNodes'])

                foundSearch = False
                search = ""
                findID = msg['findID']
                for s in self._searches:
                    if s._findID == findID:
                        search = s
                        foundSearch = True

                if not foundSearch:
                    self._log.info('No search found')
                    return
                else:

                    # Get current shortlist length
                    shortlist_length = len(search._shortlist)

                    # Extends shortlist if necessary
                    for node in msg['foundNodes']:
                        self._log.info('FOUND NODE: %s' % node)
                        if node[0] != self._transport._guid and node[2] != self._transport.pubkey \
                                and node[1] != self._transport._uri:
                            self._log.info('Found it %s %s' % (node[0], self._transport.guid))
                            self.extendShortlist(transport, msg['findID'], [node])

                    # Remove active probe to this node for this findID
                    search_ip = urlparse(msg['uri']).hostname
                    search_port = urlparse(msg['uri']).port
                    search_guid = msg['senderGUID']
                    search_tuple = (search_ip, search_port, search_guid)
                    for idx, probe in enumerate(search._active_probes):
                        if probe == search_tuple:
                            del search._active_probes[idx]
                    self._log.debug('Find Node Response - Active Probes After: %s' % search._active_probes)

                    # Add this to already contacted list
                    if search_tuple not in search._already_contacted:
                        search._already_contacted.append(search_tuple)
                    self._log.debug('Already Contacted: %s' % search._already_contacted)

                    # If we added more to shortlist then keep searching
                    if len(search._shortlist) > shortlist_length:
                        self._log.info('Lets keep searching')
                        self._searchIteration(search)
                    else:
                        self._log.info('Shortlist is empty')
                        if search._callback is not None:
                            search._callback(search._shortlist)

    def _refreshNode(self):
        """ Periodically called to perform k-bucket refreshes and data
        replication/republishing as necessary """
        self._refreshRoutingTable()
        self._republishData()

    def _refreshRoutingTable(self):
        self._log.info('Started Refreshing Routing Table')

        # Get Random ID from every k-bucket
        nodeIDs = self._routingTable.getRefreshList(0, False)

        def searchForNextNodeID():
            if len(nodeIDs) > 0:
                searchID = nodeIDs.pop()
                self.iterativeFindNode(searchID)
                searchForNextNodeID()
            else:
                # If this is reached, we have finished refreshing the routing table
                return

        # Start the refreshing cycle
        searchForNextNodeID()

    def _republishData(self, *args):
        self._threadedRepublishData()

    def _threadedRepublishData(self, *args):
        """ Republishes and expires any stored data (i.e. stored
        C{(key, value pairs)} that need to be republished/expired

        This method should run in a deferred thread
        """
        self._log.debug('Republishing Data')
        expiredKeys = []

        for key in self._dataStore.keys():

            # Filter internal variables stored in the data store
            if key == 'nodeState':
                continue

            now = int(time.time())
            key = key.encode('hex')
            originalPublisherID = self._dataStore.originalPublisherID(key)
            age = now - self._dataStore.originalPublishTime(key) + 500000

            if originalPublisherID == self._settings['guid']:
                # This node is the original publisher; it has to republish
                # the data before it expires (24 hours in basic Kademlia)
                if age >= constants.dataExpireTimeout:
                    self.iterativeStore(self._transport, key, self._dataStore[key])

            else:
                # This node needs to replicate the data at set intervals,
                # until it expires, without changing the metadata associated with it
                # First, check if the data has expired
                if age >= constants.dataExpireTimeout:
                    # This key/value pair has expired (and it has not been republished by the original publishing node
                    # - remove it
                    expiredKeys.append(key)
                elif now - self._dataStore.lastPublished(key) >= constants.replicateInterval:
                    self.iterativeStore(self._transport, key, self._dataStore[key], originalPublisherID, age)

        for key in expiredKeys:
            del self._dataStore[key]

    def extendShortlist(self, transport, findID, foundNodes):

        self._log.debug('foundNodes: %s' % foundNodes)

        foundSearch = False
        for s in self._searches:
            if s._findID == findID:
                search = s
                foundSearch = True

        if not foundSearch:
            self._log.error('There was no search found for this ID')
            return

        self._log.debug('Short list before: %s' % search._shortlist)

        for node in foundNodes:

            node_guid, node_uri, node_pubkey, node_nick = node
            node_ip = urlparse(node_uri).hostname
            node_port = urlparse(node_uri).port

            # Add to shortlist
            if (node_ip, node_port, node_guid, node_nick) not in search._shortlist:
                search.add_to_shortlist([(node_ip, node_port, node_guid, node_nick)])

            # Skip ourselves if returned
            if node_guid == self._settings['guid']:
                continue

            for peer in self._activePeers:
                if node_guid == peer._guid:
                    # Already an active peer or it's myself
                    continue

            if node_guid != self._settings['guid']:
                self._log.debug('Adding new peer to active peers list: %s' % node)
                self.add_peer(self._transport, node_uri, node_pubkey, node_guid, node_nick)

        self._log.debug('Short list after: %s' % search._shortlist)

    def find_listings(self, transport, key, listingFilter=None, callback=None):
        """ Send a get product listings call to the node in question and then cache those listings locally
        TODO: Ideally we would want to send an array of listing IDs that we have locally and then the node would
        send back the missing or updated listings. This would save on queries for listings we already have.
        """

        peer = self._routingTable.getContact(key)

        if peer:
            if peer.check_port():
                peer.send({'type': 'query_listings', 'key': key})
                return

        # Check cache in DHT if peer not available
        listing_index_key = hashlib.sha1('contracts-%s' % key).hexdigest()
        hashvalue = hashlib.new('ripemd160')
        hashvalue.update(listing_index_key)
        listing_index_key = hashvalue.hexdigest()

        self._log.info('Finding contracts for store: %s' % listing_index_key)

        self.iterativeFindValue(listing_index_key, callback)

        # Find appropriate storage nodes and save key value
        # self.iterativeFindNode(key, lambda msg, key=key, value=value, originalPublisherID=originalPublisherID, age=age: self.storeKeyValue(msg, key, value, originalPublisherID, age))

    def find_listings_by_keyword(self, transport, keyword, listingFilter=None, callback=None):

        hashvalue = hashlib.new('ripemd160')
        keyword_key = 'keyword-%s' % keyword
        hashvalue.update(keyword_key.encode('utf-8'))
        listing_index_key = hashvalue.hexdigest()

        self._log.info('Finding contracts for keyword: %s' % keyword)

        self.iterativeFindValue(listing_index_key, callback)

        # Find appropriate storage nodes and save key value
        # self.iterativeFindNode(key, lambda msg, key=key, value=value, originalPublisherID=originalPublisherID, age=age: self.storeKeyValue(msg, key, value, originalPublisherID, age))

    def iterativeStore(self, transport, key, value_to_store=None, originalPublisherID=None, age=0):
        """ The Kademlia store operation

        Call this to store/republish data in the DHT.

        @param key: The hashtable key of the data
        @type key: str
        @param value: The actual data (the value associated with C{key})
        @type value: str
        @param originalPublisherID: The node ID of the node that is the
                                    B{original} publisher of the data
        @type originalPublisherID: str
        @param age: The relative age of the data (time in seconds since it was
                    originally published). Note that the original publish time
                    isn't actually given, to compensate for clock skew between
                    different nodes.
        @type age: int
        """
        if originalPublisherID is None:
            originalPublisherID = self._transport._guid

        # Find appropriate storage nodes and save key value
        if value_to_store:
            self.iterativeFindNode(key, lambda msg, findKey=key, value=value_to_store,
                                               originalPublisherID=originalPublisherID,
                                               age=age: self.storeKeyValue(msg, findKey, value, originalPublisherID,
                                                                           age))

    def storeKeyValue(self, nodes, key, value, originalPublisherID, age):

        self._log.debug('Store Key Value: (%s, %s %s)' % (nodes, key, type(value)))

        try:

            value_json = json.loads(value)

            # Add Notary GUID to index
            if 'notary_index_add' in value_json:
                existing_index = self._dataStore[key]
                if existing_index is not None:
                    if not value_json['notary_index_add'] in existing_index['notaries']:
                        existing_index['notaries'].append(value_json['notary_index_add'])
                    value = existing_index
                else:
                    value = {'notaries': [value_json['notary_index_add']]}
                self._log.info('Notaries: %s' % existing_index)

            if 'notary_index_remove' in value_json:
                existing_index = self._dataStore[key]
                if existing_index is not None:
                    if value_json['notary_index_remove'] in existing_index['notaries']:
                        existing_index['notaries'].remove(value_json['notary_index_remove'])
                        value = existing_index
                    else:
                        return
                else:
                    return

            # Add listing to keyword index
            if 'keyword_index_add' in value_json:

                existing_index = self._dataStore[key]

                if existing_index is not None:

                    if not value_json['keyword_index_add'] in existing_index['listings']:
                        existing_index['listings'].append(value_json['keyword_index_add'])

                    value = existing_index

                else:

                    value = {'listings': [value_json['keyword_index_add']]}

                self._log.info('keyword %s' % existing_index)

            if 'keyword_index_remove' in value_json:

                existing_index = self._dataStore[key]

                if existing_index is not None:

                    if value_json['keyword_index_remove'] in existing_index['listings']:
                        existing_index['listings'].remove(value_json['keyword_index_remove'])
                        value = existing_index
                    else:
                        return

                else:
                    # Not in keyword index anyways
                    return

        except Exception as e:
            self._log.debug('Value is not a JSON array: %s' % e)

        now = int(time.time())
        originallyPublished = now - age

        # Store it in your own node
        self._dataStore.setItem(key, value, now, originallyPublished, originalPublisherID, market_id=self._market_id)

        for node in nodes:

            uri = 'tcp://%s:%s' % (node[0], node[1])
            guid = node[2]

            peer = self._routingTable.getContact(guid)

            if guid == self._transport.guid:
                break

            if not peer:
                peer = self._transport.get_crypto_peer(guid, uri)
                peer.start_handshake()

            peer.send(proto_store(key, value, originalPublisherID, age))

    def _on_storeValue(self, msg):

        self._log.debug('Received Store Command')

        key = msg['key']
        value = msg['value']
        originalPublisherID = msg['originalPublisherID']
        age = msg['age']

        now = int(time.time())
        originallyPublished = now - age

        if value:
            self._dataStore.setItem(key, value, now, originallyPublished, originalPublisherID, self._market_id)
        else:
            self._log.info('No value to store')

    def store(self, key, value, originalPublisherID=None, age=0, **kwargs):
        """ Store the received data in this node's local hash table

        @param key: The hashtable key of the data
        @type key: str
        @param value: The actual data (the value associated with C{key})
        @type value: str
        @param originalPublisherID: The node ID of the node that is the
                                    B{original} publisher of the data
        @type originalPublisherID: str
        @param age: The relative age of the data (time in seconds since it was
                    originally published). Note that the original publish time
                    isn't actually given, to compensate for clock skew between
                    different nodes.
        @type age: int

        @rtype: str

        @todo: Since the data (value) may be large, passing it around as a buffer
               (which is the case currently) might not be a good idea... will have
               to fix this (perhaps use a stream from the Protocol class?)
        """
        # Get the sender's ID (if any)
        if '_rpcNodeID' in kwargs:
            rpcSenderID = kwargs['_rpcNodeID']
        else:
            rpcSenderID = None

        if originalPublisherID is None:
            if rpcSenderID is not None:
                originalPublisherID = rpcSenderID
            else:
                raise TypeError(
                    'No publisher specifed, and RPC caller ID not available. Data requires an original publisher.')

        now = int(time.time())
        originallyPublished = now - age
        self._dataStore.setItem(key, value, now, originallyPublished, originalPublisherID, market_id=self._market_id)
        return 'OK'

    def iterativeFindNode(self, key, callback=None):
        """ The basic Kademlia node lookup operation

        Call this to find a remote node in the P2P overlay network.

        @param key: the 160-bit key (i.e. the node or value ID) to search for
        @type key: str

        @return: This immediately returns a deferred object, which will return
                 a list of k "closest" contacts (C{kademlia.contact.Contact}
                 objects) to the specified key as soon as the operation is
                 finished.
        @rtype: twisted.internet.defer.Deferred
        """
        self._log.info('Looking for node at: %s' % key)
        self._iterativeFind(key, callback=callback)

    def _iterativeFind(self, key, startupShortlist=None, call='findNode', callback=None):
        """
        - Create a new DHTSearch object and add the key and call back to it
        - Add the search to our search queue (self._searches)
        - Find out if we're looking for a value or for a node
        -

        """
        # Create a new search object
        new_search = DHTSearch(self._market_id, key, call, callback=callback)
        self._searches.append(new_search)

        # Determine if we're looking for a node or a key
        findValue = True if call != 'findNode' else False

        # If search is for your key abandon search
        if not findValue and key == self._settings['guid']:
            return 'You are looking for yourself'

        # If looking for a node check in your active peers list first to prevent unnecessary searching
        if not findValue:
            self._log.info('Looking for node in your active connections list')
            for node in self._activePeers:
                if node._guid == key:
                    return [node]

        if startupShortlist == [] or startupShortlist is None:

            # Retrieve closest nodes adn add them to the shortlist for the search
            closeNodes = self._routingTable.findCloseNodes(key, constants.alpha, self._settings['guid'])
            shortlist = []

            for closeNode in closeNodes:
                shortlist.append((closeNode._ip, closeNode._port, closeNode._guid))

            if len(shortlist) > 0:
                new_search.add_to_shortlist(shortlist)

            # Refresh the k-bucket for this key
            if key != self._settings['guid']:
                self._routingTable.touchKBucket(key)

            # Abandon the search if the shortlist has no nodes
            if len(new_search._shortlist) == 0:
                self._log.info('Out of nodes to search, stopping search')
                if callback is not None:
                    callback([])
                else:
                    return []

        else:
            # On startup of the server the shortlist is pulled from the DB
            # TODO: Right now this is just hardcoded to be seed URIs but should pull from db
            new_search._shortlist = startupShortlist

        self._searchIteration(new_search, findValue=findValue)

    def _searchIteration(self, new_search, findValue=False):

        # Update slow nodes count
        new_search._slowNodeCount[0] = len(new_search._active_probes)

        # Sort shortlist from closest to farthest
        # self._activePeers.sort(lambda firstContact, secondContact, targetKey=key: cmp(self._routingTable.distance(firstContact._guid, targetKey), self._routingTable.distance(secondContact._guid, targetKey)))
        # new_search._shortlist.sort(lambda firstContact, secondContact, targetKey=key: cmp(self._routingTable.distance(firstContact._guid, targetKey), self._routingTable.distance(secondContact._guid, targetKey)))

        self._activePeers.sort(lambda firstNode, secondNode, targetKey=new_search._key: cmp(
            self._routingTable.distance(firstNode._guid, targetKey),
            self._routingTable.distance(secondNode._guid, targetKey)))

        # while len(self._pendingIterationCalls):
        # del self._pendingIterationCalls[0]

        # TODO: Put this in the callback
        # if new_search._key in new_search._find_value_result:
        # return new_search._find_value_result
        # elif len(new_search._shortlist) and findValue is False:
        #
        # # If you have more k amount of nodes in your shortlist then stop
        # # or ...
        # if (len(new_search._shortlist) >= constants.k) or (
        # new_search._shortlist[0] == new_search._previous_closest_node and len(
        # new_search._active_probes) ==
        # new_search._slowNodeCount[0]):
        # if new_search._callback is not None:
        # new_search._callback(new_search._shortlist)
        # return

        # Update closest node
        if len(self._activePeers):
            closestPeer = self._activePeers[0]
            closestPeer_ip = urlparse(closestPeer._address).hostname
            closestPeer_port = urlparse(closestPeer._address).port
            new_search._previous_closest_node = (closestPeer_ip, closestPeer_port, closestPeer._guid)
            self._log.debug('Previous Closest Node %s' % (new_search._previous_closest_node,))

        # Sort short list again
        if len(new_search._shortlist) > 1:
            self._log.info('Short List: %s' % new_search._shortlist)

            # Remove dupes
            new_search._shortlist = self.dedupe(new_search._shortlist)

            new_search._shortlist.sort(lambda firstNode, secondNode, targetKey=new_search._key: cmp(
                self._routingTable.distance(firstNode[2], targetKey),
                self._routingTable.distance(secondNode[2], targetKey)))

            new_search._prevShortlistLength = len(new_search._shortlist)

        # See if search was cancelled
        if not self.activeSearchExists(new_search._findID):
            self._log.info('Active search does not exist')
            return

        # Send findNodes out to all nodes in the shortlist
        for node in new_search._shortlist:
            if node not in new_search._already_contacted:
                if node[2] != self._transport._guid:

                    new_search._active_probes.append(node)
                    new_search._already_contacted.append(node)
                    contact = self._routingTable.getContact(node[2])

                    if contact:

                        msg = {"type": "findNode",
                               "uri": contact._transport._uri,
                               "senderGUID": self._transport._guid,
                               "key": new_search._key,
                               "findValue": findValue,
                               "senderNick": self._transport._nickname,
                               "findID": new_search._findID,
                               "pubkey": contact._transport.pubkey}
                        self._log.debug('Sending findNode to: %s %s' % (contact._address, msg))

                        contact.send(msg)
                        new_search._contactedNow += 1

                    else:
                        self._log.error('No contact was found for this guid: %s' % node[2])

            if new_search._contactedNow == constants.alpha:
                break

    def activeSearchExists(self, findID):

        activeSearchExists = False
        for search in self._searches:
            if findID == search._findID:
                return True
        if not activeSearchExists:
            return False

    def iterativeFindValue(self, key, callback=None):

        self._log.debug('[Iterative Find Value]')
        self._iterativeFind(key, call='findValue', callback=callback)

    @staticmethod
    def dedupe(lst):
        seen = set()
        result = []
        for item in lst:
            fs = frozenset(item)
            if fs not in seen:
                result.append(item)
                seen.add(fs)
        return result


class DHTSearch(object):
    def __init__(self, market_id, key, call="findNode", callback=None):
        self._key = key  # Key to search for
        self._call = call  # Either findNode or findValue depending on search
        self._callback = callback  # Callback for when search finishes
        self._shortlist = []  # List of nodes that are being searched against
        self._active_probes = []  #
        self._already_contacted = []  # Nodes are added to this list when they've been sent a findXXX action
        self._previous_closest_node = None  # This is updated to be the closest node found during search
        self._find_value_result = {}  # If a findValue search is found this is the value
        self._pendingIterationCalls = []  #
        self._slowNodeCount = [0]  #
        self._contactedNow = 0  # Counter for how many nodes have been contacted
        self._dhtCallbacks = []  # Callback list
        self._prevShortlistLength = 0

        self._log = logging.getLogger('[%s] %s' % (market_id,
                                                   self.__class__.__name__))

        # Create a unique ID (SHA1) for this iterativeFind request to support parallel searches
        self._findID = hashlib.sha1(os.urandom(128)).hexdigest()

    def add_to_shortlist(self, additions):

        self._log.debug('Additions: %s' % additions)
        for item in additions:
            if item not in self._shortlist:
                self._shortlist.append(item)

        self._log.debug('Updated short list: %s' % self._shortlist)
