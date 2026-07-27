import os
import sys
import platform
import shutil
import subprocess
import threading
import queue
import time
import urllib.request
import zipfile
import tarfile
import socket

def get_local_ip() -> str:
    """Return machine LAN IP."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


class MediaMTXTunnel:
    """
    Wraps a MediaMTX media server process.

    Events posted to self.status_queue (queue.Queue):
        ("connecting", None)
        ("connected",  (host: str, port: int))
        ("error",      message: str)
        ("closed",     None)
    """

    MEDIAMTX_VERSION = "v1.11.3"

    def __init__(self, local_port: int = 9000, rtsp_port: int = 8554, rtmp_port: int = 1935, webrtc_port: int = 8889):
        self.local_port = local_port
        self.rtsp_port = rtsp_port
        self.rtmp_port = rtmp_port
        self.webrtc_port = webrtc_port
        self.status_queue: queue.Queue = queue.Queue()
        
        self._process: subprocess.Popen | None = None
        self._running = False
        self._thread: threading.Thread | None = None

    def _get_executable_name(self) -> str:
        return "mediamtx.exe" if sys.platform == "win32" else "mediamtx"

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

        # 4. Attempt automatic download if not found
        try:
            os.makedirs(bin_dir, exist_ok=True)
            url = self._get_download_url()
            if not url:
                return None
            
            archive_path = os.path.join(bin_dir, "mediamtx_archive")
            urllib.request.urlretrieve(url, archive_path)
            
            if url.endswith(".zip"):
                with zipfile.ZipFile(archive_path, 'r') as zip_ref:
                    zip_ref.extractall(bin_dir)
            elif url.endswith(".tar.gz"):
                with tarfile.open(archive_path, 'r:gz') as tar_ref:
                    tar_ref.extractall(bin_dir)

            if os.path.exists(archive_path):
                os.remove(archive_path)

            if os.path.exists(bin_path):
                if sys.platform != "win32":
                    os.chmod(bin_path, 0o755)
                return bin_path
        except Exception as e:
            print(f"[MediaMTXTunnel] Download failed: {e}")

        return None

    def _get_download_url(self) -> str | None:
        system = sys.platform
        machine = platform.machine().lower()

        arch = "amd64"
        if machine in ("arm64", "aarch64"):
            arch = "arm64"
        elif machine in ("i386", "i686", "x86"):
            arch = "386"

        ver = self.MEDIAMTX_VERSION
        if system == "win32":
            return f"https://github.com/bluenviron/mediamtx/releases/download/{ver}/mediamtx_{ver}_windows_{arch}.zip"
        elif system == "darwin":
            return f"https://github.com/bluenviron/mediamtx/releases/download/{ver}/mediamtx_{ver}_darwin_{arch}.tar.gz"
        elif system.startswith("linux"):
            return f"https://github.com/bluenviron/mediamtx/releases/download/{ver}/mediamtx_{ver}_linux_{arch}.tar.gz"
        
        return None

    def start(self) -> None:
        """Starts the MediaMTX process in a background thread."""
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
                "MediaMTX binary not found. Please install mediamtx or check internet connection."
            ))
            self._running = False
            return

        try:
            # Launch MediaMTX process
            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0  # SW_HIDE

            self._process = subprocess.Popen(
                [binary_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                startupinfo=startupinfo,
            )

            # Give it a second to initialize
            time.sleep(1)

            if self._process.poll() is not None:
                out, _ = self._process.communicate()
                self.status_queue.put(("error", f"MediaMTX failed to start: {out[:200]}"))
                self._running = False
                return

            local_ip = get_local_ip()
            self.status_queue.put(("connected", (local_ip, self.rtsp_port)))

            # Read process output line by line to keep the stdout buffer drained
            for line in iter(self._process.stdout.readline, ''):
                if not self._running:
                    break

            if self._running and self._process.poll() is not None:
                self.status_queue.put(("error", "MediaMTX process exited unexpectedly."))

        except Exception as e:
            if self._running:
                self.status_queue.put(("error", f"Failed to start MediaMTX: {str(e)}"))
        finally:
            self._running = False

    def stop(self) -> None:
        """Terminates the MediaMTX process cleanly."""
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
        """MediaMTX has no time limit (unlike Pinggy free tier). Returns None."""
        return None
