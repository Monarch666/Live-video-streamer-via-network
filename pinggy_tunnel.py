import queue
import re
import threading
import time
import pexpect

class PinggyTunnel:
    """
    Wraps a Pinggy free anonymous TCP tunnel (ssh -R reverse forward).

    Events posted to self.status_queue (queue.Queue), consumed by the UI
    thread exactly like SenderBackend.status_queue:
        ("connecting", None)
        ("connected",  (host: str, port: int))
        ("error",      message: str)
        ("expired",    None)          # 60-minute mark reached, or process died
        ("closed",     None)          # user-initiated stop
    """
    def __init__(self, local_port: int):
        self.local_port = local_port
        self.status_queue = queue.Queue()
        self._connected_at = None
        self._child = None
        self._running = False
        self._thread = None
        self._expiry_timer = None

    def start(self) -> None:
        """Starts the tunnel in a background thread."""
        if self._running:
            return
        
        self._running = True
        self.status_queue.put(("connecting", None))
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        cmd = (
            f"ssh -p 443 -R0:localhost:{self.local_port} "
            f"-o StrictHostKeyChecking=accept-new "
            f"-o ServerAliveInterval=25 -o ServerAliveCountMax=3 "
            f"-t free+tcp@a.pinggy.io"
        )
        
        try:
            # We use pexpect.spawn for unix-like systems.
            import sys
            if sys.platform == "win32":
                try:
                    from pexpect.popen_spawn import PopenSpawn
                    child = PopenSpawn(cmd, encoding='utf-8')
                except ImportError:
                    self.status_queue.put(("error", "pexpect on Windows requires popen_spawn. Please check environment."))
                    self._running = False
                    return
            else:
                child = pexpect.spawn(cmd, encoding='utf-8', timeout=15)
                
            self._child = child

            idx = child.expect(["password:", pexpect.EOF, pexpect.TIMEOUT], timeout=15)
            if idx == 0:
                child.sendline("")
            elif idx == 1:
                self._handle_error_output(child.before)
                return
            elif idx == 2:
                self.status_queue.put(("error", "Timeout waiting for ssh password prompt."))
                return

            # Now wait for the tunnel URL
            tunnel_found = False
            start_wait = time.time()
            buffer = ""
            
            while time.time() - start_wait < 45:
                try:
                    chunk = child.read_nonblocking(size=1024, timeout=1)
                    if isinstance(chunk, bytes):
                        chunk = chunk.decode('utf-8', errors='ignore')
                    buffer += chunk
                    
                    # Remove ANSI escape sequences (colors/formatting) that might break the regex
                    clean_buf = re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', buffer)
                    # Remove newlines in case the PTY wrapped the long URL
                    clean_buf = clean_buf.replace('\r', '').replace('\n', '')
                    
                    match = re.search(r'tcp://([a-zA-Z0-9.\-]+):(\d+)', clean_buf)
                    if match:
                        host, port = match.groups()
                        self._connected_at = time.time()
                        self.status_queue.put(("connected", (host, int(port))))
                        tunnel_found = True
                        break
                    
                    if "Unsupported argument" in clean_buf or "Connection closed" in clean_buf:
                        self.status_queue.put(("error", "SSH error: Connection rejected by Pinggy"))
                        return
                        
                except pexpect.TIMEOUT:
                    pass
                except pexpect.EOF:
                    self._handle_error_output(buffer)
                    return

            if not tunnel_found:
                self.status_queue.put(("error", "Pinggy is taking too long to return an address (timed out after 45s). Try restarting the internet link."))
                return

            # Keep reading to check if it exits
            while self._running:
                try:
                    child.expect(pexpect.EOF, timeout=1)
                    # If we reach here, EOF was reached
                    if self._running:
                        self.status_queue.put(("expired", None))
                        self._running = False
                    break
                except pexpect.TIMEOUT:
                    # Timeout is expected since we're just blocking waiting for EOF
                    
                    # Manual 60-minute expiry check
                    if self._connected_at and time.time() - self._connected_at >= 3600:
                        self.status_queue.put(("expired", None))
                        self.stop()
                        break
                        
        except pexpect.ExceptionPexpect as e:
            if self._running:
                self.status_queue.put(("error", f"SSH process failed: {str(e)}"))
        except FileNotFoundError:
            self.status_queue.put(("error", "ssh not found — install OpenSSH client"))
        except Exception as e:
            if self._running:
                self.status_queue.put(("error", f"Unexpected error: {str(e)}"))
        finally:
            self._running = False

    def _handle_error_output(self, output) -> None:
        if isinstance(output, bytes):
            output = output.decode('utf-8', errors='ignore')
        if not output:
            output = "Connection closed unexpectedly."
        # Pinggy often prints banners, we just grab the last few lines or generic message
        lines = [line.strip() for line in output.split('\n') if line.strip()]
        if lines:
            self.status_queue.put(("error", f"SSH EOF: {lines[-1]}"))
        else:
            self.status_queue.put(("error", "SSH process exited unexpectedly."))
            
    def stop(self) -> None:
        """Kills the ssh process cleanly."""
        if not self._running and self._child is None:
            return
            
        self._running = False
        
        if self._child:
            try:
                if self._child.isalive():
                    self._child.terminate(force=True)
            except Exception:
                pass
            self._child = None
            
        self.status_queue.put(("closed", None))

    def seconds_remaining(self) -> int:
        """Returns seconds remaining until the 60-minute expiry, or None if not connected."""
        if not self._connected_at:
            return None
        elapsed = int(time.time() - self._connected_at)
        remaining = 3600 - elapsed
        return max(0, remaining)
