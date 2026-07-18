"""Blink Live: single window that loads, then embeds the live video feed."""
import asyncio
import ctypes
from ctypes import wintypes
import json
import logging
import queue
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from aiohttp import ClientSession
from blinkpy.blinkpy import Blink
from blinkpy.auth import Auth
from blinkpy.helpers.util import json_load

HERE = Path(__file__).parent
CREDS = HERE / "credentials.json"
CONFIG = HERE / "config.json"
LOG = HERE / "blink-live.log"
ICON = HERE / "blink.ico"

BG = "#0d1117"
CARD = "#161b22"
FG = "#e6edf3"
MUTED = "#8b949e"
ACCENT = "#3fb950"
ERROR_FG = "#f85149"

WINDOW_SIZE = (960, 540)

logging.basicConfig(
    filename=str(LOG),
    filemode="w",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("blink-live")

# ---------------------------------------------------------------------------
# Win32 helpers used to embed ffplay's video window inside our Tk window.
# ---------------------------------------------------------------------------
user32 = ctypes.windll.user32
shcore = ctypes.windll.shcore

GWL_STYLE = -16
WS_CHILD = 0x40000000
WS_POPUP = 0x80000000
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_SYSMENU = 0x00080000
WS_BORDER = 0x00800000
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020

_ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.EnumWindows.restype = wintypes.BOOL
user32.EnumWindows.argtypes = [_ENUM_PROC, wintypes.LPARAM]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.SetParent.restype = wintypes.HWND
user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
user32.SetWindowPos.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.MoveWindow.restype = wintypes.BOOL
user32.MoveWindow.argtypes = [
    wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.BOOL,
]
user32.GetWindowLongPtrW.restype = ctypes.c_long
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_long
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]


def _set_dpi_aware():
    """Keep our window and the embedded ffplay window on the same DPI footing."""
    try:
        shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def scan_windows_for_pid(pid):
    """One immediate, non-blocking scan for a visible window owned by `pid`."""
    result = {}

    def callback(hwnd, _lparam):
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != pid:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        result["hwnd"] = hwnd
        return False

    user32.EnumWindows(_ENUM_PROC(callback), 0)
    return result.get("hwnd")


def find_window_by_pid(pid, timeout):
    """Poll for a visible top-level window owned by process `pid`."""
    deadline = time.monotonic() + timeout
    while True:
        hwnd = scan_windows_for_pid(pid)
        if hwnd:
            return hwnd
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.1)


def embed_window(child_hwnd, parent_hwnd):
    style = user32.GetWindowLongPtrW(child_hwnd, GWL_STYLE)
    style &= ~(WS_POPUP | WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX |
               WS_MAXIMIZEBOX | WS_SYSMENU | WS_BORDER)
    style |= WS_CHILD
    user32.SetWindowLongPtrW(child_hwnd, GWL_STYLE, style)
    user32.SetParent(child_hwnd, parent_hwnd)
    user32.SetWindowPos(child_hwnd, 0, 0, 0, 0, 0,
                         SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)


def resize_embedded(child_hwnd, width, height):
    if width > 0 and height > 0:
        user32.MoveWindow(child_hwnd, 0, 0, width, height, True)


# ---------------------------------------------------------------------------


class App:
    def __init__(self, camera_name):
        self.camera_name = camera_name
        self.q = queue.Queue()
        self.child_hwnd = None
        self.video_frame = None
        self.player = None
        self.loop = None
        self.task = None
        self.worker_thread = None
        self.closing = False

        self.root = tk.Tk()
        self.root.title("Blink Live")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)

        if ICON.exists():
            try:
                self.root.iconbitmap(str(ICON))
            except Exception:
                log.warning("Could not set window icon", exc_info=True)

        self._set_geometry(*WINDOW_SIZE)
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(500, lambda: self.root.attributes("-topmost", False))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close_request)

        self.loading_frame = tk.Frame(self.root, bg=BG)
        self.loading_frame.pack(fill="both", expand=True)

        # Centered both ways within loading_frame (pack(expand=True), no fill).
        content = tk.Frame(self.loading_frame, bg=BG)
        content.pack(expand=True)

        self.title_label = tk.Label(
            content, text=camera_name, bg=BG, fg=FG, font=("Segoe UI", 18, "bold"),
        )
        self.title_label.pack(pady=(0, 6))

        self.subtitle_label = tk.Label(
            content, text="Blink live view", bg=BG, fg=MUTED, font=("Segoe UI", 10),
        )
        self.subtitle_label.pack()

        self.status_label = tk.Label(
            content, text="Starting…", bg=BG, fg=FG, font=("Segoe UI", 10),
            wraplength=380, justify="center",
        )
        self.status_label.pack(pady=(28, 18))

        style = ttk.Style()
        try:
            style.theme_use("default")
        except tk.TclError:
            pass
        style.configure(
            "blink.Horizontal.TProgressbar",
            background=ACCENT, troughcolor=CARD, borderwidth=0, thickness=6,
        )
        self.progress = ttk.Progressbar(
            content, mode="indeterminate", length=340,
            style="blink.Horizontal.TProgressbar",
        )
        self.progress.pack()
        self.progress.start(12)

        self.close_btn = tk.Button(
            content, text="Close", command=self._safe_destroy,
            bg=CARD, fg=FG, activebackground=CARD, activeforeground=FG,
            relief="flat", padx=24, pady=6, font=("Segoe UI", 10),
            cursor="hand2", borderwidth=0,
        )

        self.root.after(80, self._poll_queue)

    def _set_geometry(self, w, h):
        self.root.geometry(f"{w}x{h}")
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    # -- called from the worker thread --
    def set_status(self, text):
        self.q.put(("status", text))

    def video_ready(self, child_hwnd, player):
        self.child_hwnd = child_hwnd
        self.player = player
        self.q.put(("video", None))

    def signal_error(self, message):
        self.q.put(("error", message))

    def signal_closed(self):
        self.q.put(("closed", None))

    # -- runs on the Tk main thread --
    def _poll_queue(self):
        try:
            while True:
                event, payload = self.q.get_nowait()
                if event == "status":
                    self.status_label.config(text=payload)
                elif event == "video":
                    self._show_video()
                elif event == "error":
                    self._show_error(payload)
                    return
                elif event == "closed":
                    if not self.closing:
                        self._safe_destroy()
                    return
        except queue.Empty:
            pass
        except tk.TclError:
            return
        try:
            self.root.after(80, self._poll_queue)
        except tk.TclError:
            pass

    def _show_video(self):
        self.loading_frame.pack_forget()

        self.root.title(f"Blink - {self.camera_name}")
        self.root.resizable(True, True)
        self.root.minsize(320, 240)

        self.video_frame = tk.Frame(self.root, bg="black")
        self.video_frame.pack(fill="both", expand=True)
        self.video_frame.bind("<Configure>", self._on_frame_resize)

        self.root.update_idletasks()
        self._embed_and_watch()

    def _embed_and_watch(self):
        """Keep ffplay's window embedded; some drivers detach/recreate it mid-stream."""
        if self.closing:
            return
        if self.child_hwnd is None or not user32.IsWindow(self.child_hwnd):
            if self.player is not None and self.player.returncode is None:
                new_hwnd = scan_windows_for_pid(self.player.pid)
                if new_hwnd:
                    log.info("Re-acquired ffplay window handle: %s", new_hwnd)
                    self.child_hwnd = new_hwnd
        if self.child_hwnd is not None and user32.IsWindow(self.child_hwnd):
            parent_hwnd = self.video_frame.winfo_id()
            if user32.GetParent(self.child_hwnd) != parent_hwnd:
                log.info("ffplay window was detached; re-embedding")
                embed_window(self.child_hwnd, parent_hwnd)
                self._on_frame_resize()
        try:
            self.root.after(300, self._embed_and_watch)
        except tk.TclError:
            pass

    def _on_frame_resize(self, _event=None):
        if self.child_hwnd:
            resize_embedded(
                self.child_hwnd, self.video_frame.winfo_width(), self.video_frame.winfo_height(),
            )

    def _show_error(self, message):
        if self.video_frame is not None:
            self.video_frame.pack_forget()
            self.root.resizable(False, False)
            self.root.title("Blink Live")
            self.loading_frame.pack(fill="both", expand=True)

        self.progress.stop()
        self.progress.pack_forget()
        self.title_label.config(text="Couldn't open live view", fg=ERROR_FG)
        self.subtitle_label.config(text=self.camera_name)
        self.status_label.config(
            text=f"{message}\n\nSee blink-live.log for details.", fg=FG,
        )
        self.close_btn.pack(pady=(4, 0))

    def _on_close_request(self):
        if self.closing:
            return
        self.closing = True
        self.status_label.config(text="Closing…")
        self._wait_for_worker_then_destroy(0)

    def _wait_for_worker_then_destroy(self, attempts):
        if self.worker_thread is None or not self.worker_thread.is_alive():
            self._safe_destroy()
            return
        if self.player is not None and self.player.returncode is None:
            try:
                self.player.terminate()
            except Exception:
                pass
        elif self.loop is not None and self.task is not None and not self.task.done():
            self.loop.call_soon_threadsafe(self.task.cancel)
        if attempts > 100:  # ~5s safety cap
            self._safe_destroy()
            return
        self.root.after(50, self._wait_for_worker_then_destroy, attempts + 1)

    def _safe_destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self):
        self.root.mainloop()


async def do_stream(app):
    session = ClientSession()
    try:
        app.set_status("Loading saved session…")
        blink = Blink(session=session)
        saved = await json_load(str(CREDS))
        if not saved:
            raise RuntimeError(
                "credentials.json is empty or unreadable. Run First Run again."
            )
        blink.auth = Auth(saved, no_prompt=True, session=session)

        app.set_status("Signing in to Blink…")
        await blink.start()
        if not blink.available:
            raise RuntimeError(
                "Sign-in failed. Delete credentials.json and run First Run again."
            )
        await blink.save(str(CREDS))

        camera_name = app.camera_name
        if camera_name not in blink.cameras:
            available = ", ".join(blink.cameras.keys()) or "(none found)"
            raise RuntimeError(
                f"Camera '{camera_name}' not on this account.\nAvailable: {available}"
            )
        camera = blink.cameras[camera_name]

        app.set_status(f"Requesting live view from {camera_name}…")
        stream = await camera.init_livestream()
        await stream.start()
        log.info("Local stream URL: %s", stream.url)

        app.set_status("Opening player…")
        player = await asyncio.create_subprocess_exec(
            "ffplay",
            "-loglevel", "warning",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-autoexit",
            "-noborder",
            "-x", str(WINDOW_SIZE[0]), "-y", str(WINDOW_SIZE[1]),
            "-window_title", f"Blink - {camera_name}",
            stream.url,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        feed_task = asyncio.create_task(stream.feed())

        app.set_status("Waiting for video…")
        hwnd = await asyncio.get_event_loop().run_in_executor(
            None, find_window_by_pid, player.pid, 25.0
        )
        if hwnd is None:
            feed_task.cancel()
            if player.returncode is None:
                try:
                    player.terminate()
                except ProcessLookupError:
                    pass
            raise RuntimeError("ffplay window never appeared.")

        app.video_ready(hwnd, player)

        await player.wait()
        log.info("ffplay exited with return code %s", player.returncode)
        stream.stop()
        try:
            await asyncio.wait_for(feed_task, timeout=5)
        except asyncio.TimeoutError:
            log.warning("Timed out waiting for stream feed task to finish")
        except Exception:
            log.exception("Stream feed task ended with an error")
    finally:
        await session.close()


def worker(app):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app.loop = loop
    task = loop.create_task(do_stream(app))
    app.task = task
    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        pass
    except FileNotFoundError as e:
        log.exception("Missing file/exe")
        msg = str(e)
        if "ffplay" in msg.lower():
            app.signal_error("ffplay not found on PATH. Install ffmpeg and reopen.")
        else:
            app.signal_error(msg)
        loop.close()
        return
    except Exception as e:
        log.exception("Stream error")
        msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        app.signal_error(msg)
        loop.close()
        return
    loop.close()
    app.signal_closed()


def show_setup_needed():
    root = tk.Tk()
    root.title("Blink Live")
    root.configure(bg=BG)
    root.resizable(False, False)
    root.geometry("440x200")
    if ICON.exists():
        try:
            root.iconbitmap(str(ICON))
        except Exception:
            pass

    tk.Label(root, text="Setup needed", bg=BG, fg=FG,
             font=("Segoe UI", 16, "bold")).pack(pady=(38, 8))
    tk.Label(root,
             text="Run 'First Run.bat' once to sign in and pick a camera.",
             bg=BG, fg=MUTED, font=("Segoe UI", 10),
             wraplength=380, justify="center").pack()
    tk.Button(root, text="OK", command=root.destroy, bg=CARD, fg=FG,
              activebackground=CARD, activeforeground=FG, relief="flat",
              padx=30, pady=6, font=("Segoe UI", 10),
              cursor="hand2", borderwidth=0).pack(pady=28)
    root.mainloop()


def main():
    _set_dpi_aware()

    if not CREDS.exists() or not CONFIG.exists():
        show_setup_needed()
        return

    camera_name = json.loads(CONFIG.read_text(encoding="utf-8"))["camera"]
    app = App(camera_name)
    thread = threading.Thread(target=worker, args=(app,), daemon=False)
    app.worker_thread = thread
    thread.start()
    app.run()
    thread.join(timeout=5)
    if thread.is_alive():
        log.warning("Worker thread still running after window closed")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Fatal")
        try:
            from tkinter import messagebox
            messagebox.showerror(
                "Blink Live",
                "Fatal error. See blink-live.log in the blink-live folder.",
            )
        except Exception:
            pass
        raise
