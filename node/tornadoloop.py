import sys
import argparse
import tornado.web

from zmq.eventloop import ioloop
ioloop.install()

from crypto2crypto import CryptoTransportLayer
from db_store import Obdb
from market import Market
from ws import WebSocketHandler
import logging
import signal
from threading import Thread
from twisted.internet import reactor
import upnp


class MainHandler(tornado.web.RequestHandler):
    def get(self):
        self.redirect("/html/index.html")


class MarketApplication(tornado.web.Application):
    def __init__(self, market_ip, market_port, market_id=1,
                 bm_user=None, bm_pass=None, bm_port=None, seed_peers=[],
                 seed_mode=0, dev_mode=False, db_path='db/ob.db'):

        db = Obdb(db_path)

        self.transport = CryptoTransportLayer(market_ip,
                                              market_port,
                                              market_id,
                                              db,
                                              bm_user,
                                              bm_pass,
                                              bm_port,
                                              seed_mode,
                                              dev_mode)

        self.market = Market(self.transport, db)

        def post_joined():
            self.transport._dht._refreshNode()
            self.market.republish_contracts()

        peers = seed_peers if seed_mode == 0 else []
        self.transport.join_network(peers)

        Thread(target=reactor.run, args=(False,)).start()

        handlers = [
            (r"/", MainHandler),
            (r"/main", MainHandler),
            (r"/html/(.*)", tornado.web.StaticFileHandler, {'path': './html'}),
            (r"/ws", WebSocketHandler,
                dict(transport=self.transport, market=self.market, db=db))
        ]

        # TODO: Move debug settings to configuration location
        settings = dict(debug=True)
        tornado.web.Application.__init__(self, handlers, **settings)

    def get_transport(self):
        return self.transport

    def setup_upnp_port_mappings(self, http_port, p2p_port):
        upnp.PortMapper.DEBUG = False
        print "Setting up UPnP Port Map Entry..."
        # TODO: Add some setting whether or not to use UPnP
        # if Settings.get(Settings.USE_UPNP_PORT_MAPPINGS):
        self.upnp_mapper = upnp.PortMapper()
        # TODO: Add some setting whether or not to clean all previous port
        # mappings left behind by us
        # if Settings.get(Settings.CLEAN_UPNP_PORT_MAPPINGS_ON_START):
        #    upnp_mapper.cleanMyMappings()

        # for now let's always clean mappings every time.
        self.upnp_mapper.clean_my_mappings()
        result_http_port_mapping = self.upnp_mapper.add_port_mapping(http_port,
                                                                     http_port)
        print ("UPnP HTTP Port Map configuration done (%s -> %s) => %s" %
               (str(http_port), str(http_port), str(result_http_port_mapping)))

        result_tcp_p2p_mapping = self.upnp_mapper.add_port_mapping(p2p_port,
                                                                   p2p_port)
        print ("UPnP TCP P2P Port Map configuration done (%s -> %s) => %s" %
               (str(p2p_port), str(p2p_port), str(result_tcp_p2p_mapping)))

        result_udp_p2p_mapping = self.upnp_mapper.add_port_mapping(p2p_port,
                                                                   p2p_port,
                                                                   'UDP')
        print ("UPnP UDP P2P Port Map configuration done (%s -> %s) => %s" %
               (str(p2p_port), str(p2p_port), str(result_udp_p2p_mapping)))

        return result_http_port_mapping and \
            result_tcp_p2p_mapping and \
            result_udp_p2p_mapping

    def cleanup_upnp_port_mapping(self):
        if self.upnp_mapper is not None:
            print "Cleaning UPnP Port Mapping -> ", \
                self.upnp_mapper.clean_my_mappings()


def start_node(my_market_ip,
               my_market_port,
               log_file,
               market_id,
               bm_user=None,
               bm_pass=None,
               bm_port=None,
               seed_peers=[],
               seed_mode=0,
               dev_mode=False,
               log_level=None,
               database='db/ob.db',
               disable_upnp=False):

    logging.basicConfig(level=int(log_level),
                        format='%(asctime)s - %(name)s -  \
                                %(levelname)s - %(message)s',
                        filename=log_file)

    locallogger = logging.getLogger('[%s] %s' % (market_id, 'root'))

    handler = logging.handlers.RotatingFileHandler(log_file,
                                                   maxBytes=50,
                                                   backupCount=0)
    locallogger.addHandler(handler)

    application = MarketApplication(my_market_ip,
                                    my_market_port,
                                    market_id,
                                    bm_user,
                                    bm_pass,
                                    bm_port,
                                    seed_peers,
                                    seed_mode,
                                    dev_mode,
                                    database)

    error = True
    http_port = 8888
    p2p_port = 12345

    while error and http_port < 8988:
        try:
            application.listen(http_port)
            error = False
        except:
            http_port += 1

    if not disable_upnp:
        application.setup_upnp_port_mappings(http_port, p2p_port)
    else:
        print "Disabling upnp setup"

    locallogger.info("Started OpenBazaar Web App at http://%s:%s" %
                     (my_market_ip, http_port))
    print "Started OpenBazaar Web App at http://%s:%s" % (my_market_ip, http_port)

    # handle shutdown
    def shutdown(x, y):
        locallogger = logging.getLogger('[%s] %s' % (market_id, 'root'))
        locallogger.info("Received TERMINATE, exiting...")

        # application.get_transport().broadcast_goodbye()
        application.cleanup_upnp_port_mapping()
        tornado.ioloop.IOLoop.instance().stop()

        # TODO:
        # we should implement the shutdown of the dht connections, db connection, bitmessage connection
        # maybe this was meant to do all that but nobody ever got around it.
        # application.market.p.kill()
        sys.exit(0)
    try:
        signal.signal(signal.SIGTERM, shutdown)
    except ValueError:
        # not the main thread
        pass

    if not tornado.ioloop.IOLoop.instance():
        ioloop.install()
    else:
        tornado.ioloop.IOLoop.instance().start()

# Run this if executed directly
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("my_market_ip")
    parser.add_argument("-p", "--my_market_port",
                        type=int, default=12345)
    parser.add_argument("-l", "--log_file",
                        default='logs/production.log')
    parser.add_argument("-u", "--market_id",
                        default=1)
    parser.add_argument("-S", "--seed_peers",
                        nargs='*', default=[])
    parser.add_argument("-s", "--seed_mode",
                        default=0)
    parser.add_argument("-d", "--dev_mode",
                        action='store_true')
    parser.add_argument("--database",
                        default='db/ob.db', help="Database filename")
    parser.add_argument("--bmuser",
                        default='username', help="Bitmessage instance user")
    parser.add_argument("--bmpass",
                        default='password', help="Bitmessage instance pass")
    parser.add_argument("--bmport",
                        default='8442', help="Bitmessage instance RPC port")
    parser.add_argument("--log_level",
                        default=10, help="Numeric value for logging level")
    parser.add_argument("--disable_upnp",
                        action='store_true')
    args = parser.parse_args()
    start_node(args.my_market_ip,
               args.my_market_port,
               args.log_file,
               args.market_id,
               args.bmuser,
               args.bmpass,
               args.bmport,
               args.seed_peers,
               args.seed_mode,
               args.dev_mode,
               args.log_level,
               args.database,
               args.disable_upnp)
