import time
import queue
from mediamtx_tunnel import MediaMTXTunnel

mt = MediaMTXTunnel(local_port=9000)
mt.start()
print("MediaMTX starting...")

start = time.time()
while time.time() - start < 30:
    try:
        ev, val = mt.status_queue.get(timeout=1)
        print("EVENT:", ev, val)
        if ev in ("connected", "error"):
            break
    except queue.Empty:
        pass
mt.stop()
print("MediaMTX stopped.")
