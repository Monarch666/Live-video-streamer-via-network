import time
import queue
from cloudflared_tunnel import CloudflareTunnel

ct = CloudflareTunnel(local_port=9000)
ct.start()
print("Starting Cloudflare Tunnel...")

start = time.time()
while time.time() - start < 30:
    try:
        ev, val = ct.status_queue.get(timeout=1)
        print("EVENT:", ev, val)
        if ev in ("connected", "error"):
            break
    except queue.Empty:
        pass

ct.stop()
print("Cloudflare Tunnel stopped.")
