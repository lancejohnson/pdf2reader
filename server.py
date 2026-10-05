#!/usr/bin/env python3
"""pdf2reader web service — send PDFs to Readwise Reader from the iPhone Share sheet.

POST /submit   a PDF (raw body or multipart field), or a link to a PDF (text body or form field "url").
               Replies with one line of plain text (shown by the iOS Shortcut) and queues the job.
GET  /         status page + upload form (add to Home Screen).
GET  /jobs.json
Webhook: a second listener on 127.0.0.1:8451 takes Readwise's reader.non_feed_document.created events;
for PDFs saved from a link it queues a job and deletes the original PDF from Reader once the ePub is sent.

Jobs run one at a time:  pdf2reader <pdf> --readwise  (ePub with images, emailed to Reader).
Listen on 127.0.0.1 only; exposed to the tailnet with `tailscale serve`.
"""
import email.parser, email.policy, html, json, os, pathlib, re, subprocess, sys, threading, time, urllib.parse, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
PY = str(HERE / ".venv" / "bin" / "python")
DATA = pathlib.Path.home() / ".local" / "share" / "pdf2reader" / "jobs"
DATA.mkdir(parents=True, exist_ok=True)
PORT = int(os.environ.get("PDFMD_PORT", "8450"))
MAX_BYTES = 200 * 1024 * 1024
LOCK = threading.Lock()
WAKE = threading.Event()


def job_list():
    jobs = []
    for d in DATA.iterdir():
        f = d / "job.json"
        if f.exists():
            try:
                jobs.append(json.loads(f.read_text()))
            except Exception:
                pass
    return sorted(jobs, key=lambda j: j["created"], reverse=True)


def save(job):
    with LOCK:
        (DATA / job["id"] / "job.json").write_text(json.dumps(job, indent=1))


def slug(name):
    name = re.sub(r"\.pdf$", "", urllib.parse.unquote(name), flags=re.I)
    return re.sub(r"[^A-Za-z0-9._ -]+", "", name).strip()[:80] or "document"


def fetch_pdf(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read(MAX_BYTES + 1)
        name = pathlib.Path(urllib.parse.urlparse(r.geturl()).path).name
    if not data.startswith(b"%PDF"):
        raise ValueError("That link isn't a PDF.")
    return data, name


def new_job(pdf, name, source, reader_id=None):
    jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    d = DATA / jid
    d.mkdir()
    (d / "input.pdf").write_bytes(pdf)
    job = {"id": jid, "name": slug(name), "source": source, "status": "queued",
           "created": time.time(), "message": "", "reader_id": reader_id}
    save(job)
    WAKE.set()
    return job


def worker():
    for j in job_list():  # jobs interrupted by a restart go back in the queue
        if j["status"] == "running":
            j["status"] = "queued"
            save(j)
    while True:
        queued = [j for j in job_list() if j["status"] == "queued"]
        if not queued:
            WAKE.wait(30)
            WAKE.clear()
            continue
        job = queued[-1]  # oldest first
        d = DATA / job["id"]
        job.update(status="running", started=time.time())
        save(job)
        cmd = [PY, str(HERE / "pdf2reader.py"), str(d / "input.pdf"), "-o", str(d / f"{job['name']}.md"), "--readwise"]
        with open(d / "log.txt", "w") as log:
            p = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=d)
        tail = (d / "log.txt").read_text().strip().splitlines()[-3:]
        job.update(status="done" if p.returncode == 0 else "failed", finished=time.time(),
                   message=" / ".join(tail)[-400:])
        if p.returncode == 0 and job.get("reader_id"):  # webhook job: remove the original PDF from Reader
            try:
                reader_api(f"delete/{job['reader_id']}/", method="DELETE")
                job["message"] += " / original PDF deleted from Reader"
            except Exception as e:
                job["message"] += f" / could not delete original PDF: {e}"
        save(job)


# ---------- Readwise webhook (reader.non_feed_document.created) ----------
CONFIG = pathlib.Path.home() / ".config" / "pdf2reader"
HOOK_LOCK = threading.Lock()
HOOK_SEEN = set()
HOOK_PORT = int(os.environ.get("PDF2READER_HOOK_PORT", "8451"))


def readwise_token():
    tok = os.environ.get("READWISE_TOKEN") or ""
    f = CONFIG / "readwise-token"
    return tok or (f.read_text().strip() if f.exists() else "")


def reader_api(path, method="GET"):
    req = urllib.request.Request(f"https://readwise.io/api/v3/{path}", method=method,
                                 headers={"Authorization": f"Token {readwise_token()}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
    return json.loads(body) if body else {}


def handle_hook(payload):
    """Act only on PDFs that really exist in the library (looked up with our own token), so a forged
    request can at most convert one of Lance's own PDFs. Our ePubs are category epub -> no loop."""
    cfg = json.loads((CONFIG / "config.json").read_text()) if (CONFIG / "config.json").exists() else {}
    if cfg.get("webhook_secret") and payload.get("secret") != cfg["webhook_secret"]:
        return "bad secret"
    doc_id = str(payload.get("id") or "")
    if payload.get("category") != "pdf" or not re.fullmatch(r"[0-9a-z]{20,40}", doc_id):
        return "ignored (not a pdf)"
    with HOOK_LOCK:  # Readwise can deliver the same event twice at once
        if doc_id in HOOK_SEEN or any(j.get("reader_id") == doc_id for j in job_list()):
            return "already handled"
        HOOK_SEEN.add(doc_id)
    docs = reader_api(f"list/?id={doc_id}").get("results", [])
    if not docs or docs[0].get("category") != "pdf":
        return "ignored (not in library)"
    doc = docs[0]
    src = doc.get("source_url") or ""
    title = doc.get("title") or "document"
    if not src.startswith("http") or "readwise.io/reader/document_raw_content" in src:
        jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        (DATA / jid).mkdir()
        save({"id": jid, "name": slug(title), "source": src, "status": "skipped", "created": time.time(),
              "reader_id": doc_id, "message": "Uploaded file: Reader won't hand it back. "
              "Use the Send to Reader Shortcut for this one."})
        return "skipped (uploaded file)"
    pdf, name = fetch_pdf(src)
    new_job(pdf, title or name, src, reader_id=doc_id)
    return "queued"


class Hook(BaseHTTPRequestHandler):
    """Separate listener: the only thing exposed publicly (tailscale funnel)."""
    def _ok(self, text="ok"):
        b = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, fmt, *a):
        pass

    def do_GET(self):
        self._ok()

    def do_POST(self):
        n = min(int(self.headers.get("Content-Length") or 0), 1_000_000)
        raw = self.rfile.read(n)
        self._ok()  # always 200 fast (Readwise's "Test Endpoint" needs it); work happens after
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            return
        def run():
            try:
                res = handle_hook(payload)
            except Exception as e:
                res = f"error: {e}"
            sys.stderr.write(f"hook {payload.get('id')} {payload.get('category')}: {res}\n")
        threading.Thread(target=run, daemon=True).start()


PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=apple-mobile-web-app-capable content=yes>
<meta name=apple-mobile-web-app-title content="PDF to Reader">
<link rel=manifest href=/manifest.json><link rel=apple-touch-icon href=/icon.png>
<title>PDF to Reader</title>
<style>
body{font:16px -apple-system,system-ui,sans-serif;margin:0;padding:max(16px,env(safe-area-inset-top)) 16px 40px;background:#f6f6f4;color:#222}
h1{font-size:22px;margin:4px 0 14px} form{background:#fff;border-radius:14px;padding:14px;margin-bottom:18px;box-shadow:0 1px 3px #0001}
input[type=url]{width:100%;box-sizing:border-box;font-size:16px;padding:10px;border:1px solid #ccc;border-radius:10px;margin:8px 0}
button{font-size:16px;padding:10px 16px;border:0;border-radius:10px;background:#2b6cb0;color:#fff}
.job{background:#fff;border-radius:12px;padding:12px 14px;margin:8px 0;box-shadow:0 1px 3px #0001}
.n{font-weight:600}.s{font-size:13px;color:#666;margin-top:3px}.m{font-size:12px;color:#888;margin-top:4px;word-break:break-word}
.skipped{color:#975a16}.done{color:#2f855a}.failed{color:#c53030}.running{color:#b7791f}.queued{color:#666}
</style></head><body>
<h1>PDF &rarr; Reader</h1>
<form method=post action=/submit enctype=multipart/form-data>
<input type=file name=file accept="application/pdf"><input type=url name=url placeholder="or paste a link to a PDF">
<button>Send to Reader</button></form>
__JOBS__
<script>setTimeout(()=>location.reload(),15000)</script></body></html>"""

ICON_SVG = b"""<svg xmlns='http://www.w3.org/2000/svg' width='180' height='180'><rect width='180' height='180' fill='#2b6cb0'/>
<rect x='48' y='30' width='84' height='112' rx='8' fill='#fff'/><rect x='62' y='56' width='56' height='8' fill='#2b6cb0'/>
<rect x='62' y='74' width='56' height='8' fill='#90cdf4'/><rect x='62' y='92' width='40' height='8' fill='#90cdf4'/>
<path d='M90 150 l-16 -16 h10 v-12 h12 v12 h10z' fill='#fff'/></svg>"""


def icon_png():
    f = DATA.parent / "icon.png"
    if not f.exists():
        import fitz
        doc = fitz.open(stream=ICON_SVG, filetype="svg")
        doc[0].get_pixmap().save(f)
    return f.read_bytes()


def ago(t):
    s = int(time.time() - t)
    return f"{s}s ago" if s < 60 else f"{s // 60} min ago" if s < 3600 else time.strftime("%b %-d %H:%M", time.localtime(t))


class H(BaseHTTPRequestHandler):
    def reply(self, code, body, ctype="text/plain; charset=utf-8"):
        body = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *a):
        sys.stderr.write("%s %s\n" % (self.command, self.path))

    def do_GET(self):
        if self.path == "/jobs.json":
            return self.reply(200, json.dumps(job_list()), "application/json")
        if self.path == "/manifest.json":
            return self.reply(200, json.dumps({"name": "PDF to Reader", "short_name": "PDF→Reader", "start_url": "./",
                              "display": "standalone", "background_color": "#f6f6f4", "theme_color": "#2b6cb0",
                              "icons": [{"src": "icon.png", "sizes": "180x180", "type": "image/png"}]}),
                              "application/manifest+json")
        if self.path == "/icon.png":
            return self.reply(200, icon_png(), "image/png")
        rows = []
        for j in job_list()[:30]:
            label = {"queued": "Waiting", "running": "Converting…", "done": "In Reader", "failed": "Failed",
                     "skipped": "Skipped"}[j["status"]]
            msg = f"<div class=m>{html.escape(j['message'])}</div>" if j["status"] in ("failed", "skipped") else ""
            rows.append(f"<div class=job><div class=n>{html.escape(j['name'])}</div>"
                        f"<div class=s><span class={j['status']}>{label}</span> · {ago(j['created'])}</div>{msg}</div>")
        self.reply(200, PAGE.replace("__JOBS__", "".join(rows) or "<p class=s>No documents yet.</p>"),
                   "text/html; charset=utf-8")

    def do_POST(self):
        if self.path.rstrip("/") not in ("/submit", ""):
            return self.reply(404, "not found")
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BYTES:
            return self.reply(413, "That file is too big (200 MB max).")
        body = self.rfile.read(n)
        ctype = self.headers.get("Content-Type", "")
        pdf, name, url = None, "", ""
        if ctype.startswith("multipart/form-data"):
            msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(
                b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body)
            for part in msg.iter_parts():
                data = part.get_payload(decode=True) or b""
                if data.startswith(b"%PDF"):
                    pdf, name = data, part.get_filename() or "document.pdf"
                elif data.strip():
                    url = url or data.decode("utf-8", "replace")
        elif body.startswith(b"%PDF"):
            pdf = body
            name = self.headers.get("X-Filename", "") or "document.pdf"
        else:
            url = urllib.parse.parse_qs(body.decode("utf-8", "replace")).get("url", [body.decode("utf-8", "replace")])[0]
        m = re.search(r"https?://\S+", url or "")
        source = m.group(0) if m else "upload"
        try:
            if pdf is None:
                if not m:
                    return self.reply(400, "Send a PDF file or a link to a PDF.")
                pdf, name = fetch_pdf(source)
        except Exception as e:
            return self.reply(400, f"Couldn't get that PDF: {e}")
        job = new_job(pdf, name, source)
        if "text/html" in self.headers.get("Accept", ""):  # browser form -> back to status page
            self.send_response(303)
            self.send_header("Location", "./")
            self.send_header("Content-Length", "0")
            return self.end_headers()
        self.reply(200, f"Sending “{job['name']}” to Reader — it'll show up in a few minutes.")


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", HOOK_PORT), Hook).serve_forever, daemon=True).start()
    print(f"pdf2reader server on 127.0.0.1:{PORT}, jobs in {DATA}", file=sys.stderr)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
