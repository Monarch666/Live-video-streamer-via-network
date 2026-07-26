import os
import sys
import platform
import shutil
import subprocess
import threading
import queue
import time
import re
import urllib.request
import tarfile

class CloudflareTunnel:
    """
    Wraps a cloudflared quick tunnel process.

    Events posted to self.status_queue (queue.Queue):
        ("connecting", None)
        ("connected",  (public_url: str, port: int))
        ("error",      message: str)
        ("closed",     None)
    """

    def __init__(self, local_port: int = 9000, protocol: str = "http"):
        self.local_port = local_port
        self.protocol = protocol
        self.status_queue: queue.Queue = queue.Queue()
        
        self.public_url: str | None = None
        self._process: subprocess.Popen | None = None
        self._running = False
        self._thread: threading.Thread | None = None

    def _get_executable_name(self) -> str:
        return "cloudflared.exe" if sys.platform == "win32" else "cloudflared"

    def _find_or_download_binary(self) -> str | None:
        exec_name = self._get_executable_name()
        
        # 1. Check current directory
        if os.path.exists(exec_name):
            return os.path.abspath(exec_name)

        # 2. Check bin/ directory
        bin_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")
        bin_path = os.path.join(bin_dir, exec_name)
        if os.path.exists(bin_path):
            return bin_path

        # 3. Check system PATH
        path_exec = shutil.which(exec_name)
        if path_exec:
            return path_exec

        # 4. Download cloudflared binary automatically
        try:
            os.makedirs(bin_dir, exist_ok=True)
            url = self._get_download_url()
            if not url:
                return None

            print(f"[CloudflareTunnel] Downloading cloudflared from {url}...")
            
            if url.endswith(".tgz") or url.endswith(".tar.gz"):
                archive_path = os.path.join(bin_dir, "cloudflared.tgz")
                urllib.request.urlretrieve(url, archive_path)
                with tarfile.open(archive_path, 'r:gz') as tar_ref:
                    tar_ref.extractall(bin_dir)
                if os.path.exists(archive_path):
                    os.remove(archive_path)
            else:
                # Direct executable download (exe or linux binary)
                urllib.request.urlretrieve(url, bin_path)

            if os.path.exists(bin_path):
                if sys.platform != "win32":
                    os.chmod(bin_path, 0o755)
                return bin_path
        except Exception as e:
            print(f"[CloudflareTunnel] Download failed: {e}")

        return None

    def _get_download_url(self) -> str | None:
        system = sys.platform
        machine = platform.machine().lower()

        arch = "amd64"
        if machine in ("arm64", "aarch64"):
            arch = "arm64"
        elif machine in ("i386", "i686", "x86"):
            arch = "386"

        base = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
        if system == "win32":
            return f"{base}cloudflared-windows-{arch}.exe"
        elif system == "darwin":
            return f"{base}cloudflared-darwin-{arch}.tgz"
        elif system.startswith("linux"):
            return f"{base}cloudflared-linux-{arch}"

        return None

    def start(self) -> None:
        """Starts cloudflared tunnel process in a background thread."""
        if self._running:
            return

        self._running = True
        self.status_queue.put(("connecting", None))
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        binary_path = self._find_or_download_binary()
        if not binary_path:
            self.status_queue.put((
                "error",
                "cloudflared binary not found and auto-download failed. Please check internet connection."
            ))
            self._running = False
            return

        try:
            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0  # SW_HIDE

            target_url = f"{self.protocol}://127.0.0.1:{self.local_port}"
            cmd = [binary_path, "tunnel", "--url", target_url]

            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                startupinfo=startupinfo
            )

            url_found = False
            url_pattern = re.compile(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com')

            # Read process output line by line to locate the trycloudflare.com URL
            for line in iter(self._process.stdout.readline, ''):
                if not self._running:
                    break
                match = url_pattern.search(line)
                if match and not url_found:
                    self.public_url = match.group(0)
                    url_found = True
                    self.status_queue.put(("connected", (self.public_url, self.local_port)))
                    break

            if not url_found and self._running:
                if self._process.poll() is not None:
                    out, _ = self._process.communicate()
                    self.status_queue.put(("error", f"cloudflared failed: {out[:200]}"))
                else:
                    self.status_queue.put(("error", "Could not obtain Cloudflare Tunnel URL."))
                self._running = False
                return

            # Keep thread alive while process runs
            while self._running and self._process.poll() is None:
                time.sleep(0.5)

            if self._running and self._process.poll() is not None:
                self.status_queue.put(("error", "Cloudflare Tunnel process terminated."))

        except Exception as e:
            if self._running:
                self.status_queue.put(("error", f"Cloudflare Tunnel error: {str(e)}"))
        finally:
            self._running = False

    def stop(self) -> None:
        """Terminates cloudflared process cleanly."""
        if not self._running and self._process is None:
            return

        self._running = False
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=2)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None

        self.status_queue.put(("closed", None))

    def seconds_remaining(self) -> None:
        """Cloudflare Tunnels have NO time limit. Returns None."""
        return None
