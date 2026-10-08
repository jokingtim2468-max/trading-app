"""RPyC classic server run by *Windows* Python inside Wine.

The bridge container connects with mt5linux and drives the official MetaTrader5 package
remotely. Classic mode can execute arbitrary code, which is why docker-compose keeps this
port on an internal network that only the bridge joins (never published to the host).
"""
from rpyc.core.service import ClassicService
from rpyc.utils.server import ThreadedServer

if __name__ == "__main__":
    ThreadedServer(ClassicService, hostname="0.0.0.0", port=18812, reuse_addr=True,
                   protocol_config={"allow_public_attrs": True, "sync_request_timeout": 300}).start()
