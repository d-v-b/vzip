"""A range server for measurements: serves the files of a directory by single
ranges (and, with multipart=1, several ranges as multipart/byteranges, like
Apache). Each response waits `latency` ms (time to first byte), then sends the
status, headers and body in one stream paced at `rate` MB/s per connection.
Usage: rangeserver.py <dir> <port> <latency ms> <multipart 0|1> [<rate MB/s>]"""
import os, socket, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

root, port, latency, multipart = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]) / 1000, sys.argv[4] == "1"
rate = float(sys.argv[5]) * 1e6 if len(sys.argv) > 5 else 0
import heapq

_due: list = []
_due_lock = threading.Lock()


def _timer():
    """One thread keeps the deadlines by the clock and wakes their waiters: the OS
    stretches this process's timed sleeps (timer coalescing: 26 ms became 170 ms),
    but not a wake-up from another thread."""
    while True:
        now = time.perf_counter()
        with _due_lock:
            while _due and _due[0][0] <= now:
                heapq.heappop(_due)[2].set()
        time.sleep(0)


def wait(d):
    if d <= 0:
        return
    e = threading.Event()
    with _due_lock:
        heapq.heappush(_due, (time.perf_counter() + d, id(e), e))
    e.wait()


threading.Thread(target=_timer, daemon=True).start()


stats = {"requests": 0, "bytes": 0, "ranges": 0}
lock = threading.Lock()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def setup(self):
        super().setup()
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def reply(self, status, headers, body):
        head = f"HTTP/1.1 {status} X\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        data = (head + f"Content-Length: {len(body)}\r\n\r\n").encode() + body
        wait(latency)
        if not rate:
            self.connection.sendall(data)
            return
        t0, step = time.perf_counter(), 1 << 16
        for k in range(0, len(data), step):
            self.connection.sendall(data[k:k + step])
            ahead = min(len(data), k + step) / rate - (time.perf_counter() - t0)
            if ahead > 0:
                wait(ahead)

    def do_GET(self):
        if self.path == "/stats":
            return self.reply(200, {}, repr(stats).encode())
        path = os.path.join(root, self.path.lstrip("/"))
        size = os.path.getsize(path)
        spans = []
        for r in self.headers.get("Range", "").removeprefix("bytes=").split(","):
            a, b = r.split("-")
            spans.append((int(a), min(size, int(b) + 1)))
        if len(spans) > 1 and not multipart:
            return self.reply(403, {}, b"single ranges only")
        with lock:
            stats["requests"] += 1
            stats["ranges"] += len(spans)
            stats["bytes"] += sum(b - a for a, b in spans)
        fd = os.open(path, os.O_RDONLY)
        try:
            if len(spans) == 1:
                a, b = spans[0]
                self.reply(206, {"Content-Range": f"bytes {a}-{b - 1}/{size}"}, os.pread(fd, b - a, a))
            else:
                body = b"".join(b"\r\n--B\r\nContent-Range: bytes %d-%d/%d\r\n\r\n" % (a, b - 1, size)
                                + os.pread(fd, b - a, a) for a, b in spans) + b"\r\n--B--\r\n"
                self.reply(206, {"Content-Type": "multipart/byteranges; boundary=B"}, body)
        finally:
            os.close(fd)


ThreadingHTTPServer.daemon_threads = True
ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
