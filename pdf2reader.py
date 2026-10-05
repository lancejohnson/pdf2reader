#!/usr/bin/env python3
"""pdf2reader — turn any PDF into clean Markdown/ePub (figures included) with a vision model, and send it to Readwise Reader.

Each page is rendered to PNG and sent to Claude (via the `claude` CLI, so it uses
your subscription — no API key) together with the page's raw text layer. The
model rebuilds the page as clean Markdown: headings, paragraphs re-flowed,
tables as pipe tables, charts/slides/figures described, running headers/footers
and page numbers dropped. Pages run in parallel; results are cached per page so
re-runs only redo failures.

Usage: pdf2reader input.pdf|URL [-o out.md] [--model sonnet] [--dpi 170] [-j 6] [--pages 1-5,9]
             [--epub] [--readwise] [--title T] [--author A]

Figures (vector drawings and embedded images) are cropped at --fig-dpi (200) into <name>_images/
next to the .md and placed inline; --no-images turns this off.
--epub      also write an .epub next to the .md (pandoc)
--readwise  email the ePub (images included) to your Readwise Reader address through the Gmail API.
            Set reader_email, sender and gmail_token_command in ~/.config/pdf2reader/config.json
            (see config.example.json); --reader-email / --sender override them
--readwise-api  old method (HTML via the API; Reader drops images). --replace applies to this one:
--replace   with --readwise-api, delete an existing Reader copy of the same source first (its highlights go too)
            The API method uses a token from $READWISE_TOKEN or
            macOS Keychain item "readwise-token" (get one at https://readwise.io/access_token):
            security add-generic-password -a "$USER" -s readwise-token -w <TOKEN>
"""
import argparse, time, base64, shutil, json, urllib.parse, concurrent.futures as cf, hashlib, os, pathlib, re, subprocess, sys, tempfile, urllib.request

import fitz  # PyMuPDF

PROMPT = """You are converting page {n} of {total} of a PDF into clean Markdown.

The page image is {img} (attached, or read it with your Read tool).
Below is the PDF's embedded text layer for this page (may be empty, garbled, out of order, or missing
text that lives inside images/vector slides — the IMAGE is the source of truth, the text layer only
helps with exact spelling and numbers):

<text_layer>
{text}
</text_layer>

Rules:
- Output ONLY the Markdown for this page. No preamble, no code fences around the whole thing.
- Drop running headers/footers, page numbers, repeated logos and letterhead boilerplate.
- Headings: use #/##/### that reflect the document's real hierarchy (document title = #).
- Re-flow paragraphs into normal prose (no hard line breaks mid-sentence). Join hyphenated line-break words.
- If a paragraph clearly continues from the previous page or onto the next, just output the fragment as-is
  (it will be concatenated); do not add notes about it.
- Tables (including tables inside slides or images): transcribe as GitHub pipe tables, every number exact.
- Unlabeled illustrations / cartoons / decorative art: ONE short italic line describing it (no bold label,
  no "Figure:" prefix). If the art contains a formula or text, put it right after that line.
- Figures / slides / charts that carry a printed label: write "**Figure N: Title**" (only use numbers printed
  on the page — never number an unlabeled figure) then transcribe all text in it (bullets as bullets,
  tables as tables). For charts give axis labels, series, and key values you can read. For pure
  illustrations, one short italic line describing it.
- Math: use LaTeX ($...$ inline, $$...$$ display).
- Footnotes: keep as [^n] markers with definitions at the end of the page.
- Never invent content. If something is illegible write [illegible].
{figures}"""

FIG_RULES = """
Figure crops: these regions of this page were detected as graphics and saved as images
(positions are % of page width/height: left, top, right, bottom):
{regions}
- These regions were already filtered by size, so treat EVERY one as a figure the reader must see —
  including slides that only contain bullet text, and illustrations/cartoons. Put each image on its own line exactly where it appears in the reading order:  ![short caption](FIG_k)
  Put it right after the figure's printed label line (e.g. "**Figure 5: ...**") if there is one.
- After the image, STILL transcribe the figure's text, tables and data as instructed above, so the
  content stays searchable. For an illustration, the italic description line can be the caption instead.
- Leave a region out only if it is plainly a logo, letterhead or page border. Every other FIG id must appear.
- Use each FIG id exactly once. Never invent FIG ids.
"""

MIN_FRAC, MAX_FRAC = 0.015, 0.85  # figure area as a fraction of the page


def figure_regions(page):
    """Graphic regions on a page: vector-drawing clusters plus embedded images, merged."""
    pr = page.rect
    area = pr.width * pr.height
    rects = []
    try:
        rects += list(page.cluster_drawings(x_tolerance=8, y_tolerance=8))
    except Exception:
        pass
    for x in page.get_images(full=True):
        try:
            rects += page.get_image_rects(x[0])
        except Exception:
            pass
    rects = [fitz.Rect(r) & pr for r in rects]
    rects = [r for r in rects if r.width >= 60 and r.height >= 40
             and MIN_FRAC <= r.width * r.height / area <= MAX_FRAC]
    merged = True
    while merged:  # union overlapping boxes
        merged = False
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                if rects[i].intersects(rects[j]):
                    rects[i] |= rects.pop(j)
                    merged = True
                    break
            if merged:
                break
    rects = [r for r in rects if r.width * r.height / area <= MAX_FRAC]
    return sorted(rects, key=lambda r: (round(r.y0 / 20), r.x0))


def fetch(src: str, work: pathlib.Path) -> pathlib.Path:
    if re.match(r"https?://", src):
        dst = work / "input.pdf"
        req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
        dst.write_bytes(urllib.request.urlopen(req, timeout=60).read())
        return dst
    return pathlib.Path(src).expanduser().resolve()


def parse_pages(spec, total):
    if not spec:
        return list(range(1, total + 1))
    out = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out += range(int(a), int(b or a) + 1)
    return [p for p in out if 1 <= p <= total]


def llm(prompt, model, engine, img=None, timeout=600):
    """Run one model call. engine 'claude' = Claude Code CLI (Mac subscription);
    engine 'pi' = pi CLI (any model it lists, e.g. openai-codex/gpt-6.1-sol)."""
    if engine == "claude":
        cmd = ["claude", "-p", "--model", model] + (["--allowedTools", "Read"] if img else [])
        return subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
    cmd = ["pi", "-p", "--no-session", "-nc", "-ne", "-ns", "-np", "--no-tools", "--model", model]
    cmd += [f"@{img}"] if img else []
    return subprocess.run(cmd + ["--", prompt], capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)


def convert_page(doc_path, n, total, args, cache):
    out = cache / f"page-{n:04d}.md"
    if out.exists() and out.stat().st_size > 0:
        return n, out.read_text()
    doc = fitz.open(doc_path)
    page = doc[n - 1]
    img = cache / f"page-{n:04d}.png"
    page.get_pixmap(dpi=args.dpi).save(img)
    text = page.get_text("text").strip()[:12000]
    figures = ""
    if args.images:
        regs = figure_regions(page)
        pr = page.rect
        lines = []
        for k, r in enumerate(regs, 1):
            fig = cache / f"fig-{n:04d}-{k}.png"
            if not fig.exists():
                page.get_pixmap(dpi=args.fig_dpi, clip=r + (-4, -4, 4, 4)).save(fig)
            lines.append(f"  FIG_{k}: {100*r.x0/pr.width:.0f}%, {100*r.y0/pr.height:.0f}%, "
                         f"{100*r.x1/pr.width:.0f}%, {100*r.y1/pr.height:.0f}%")
        if lines:
            figures = FIG_RULES.format(regions="\n".join(lines))
    prompt = PROMPT.format(n=n, total=total, img=img, text=text or "(empty)", figures=figures)
    for attempt in range(3):
        r = llm(prompt, args.model, args.engine, img=img)
        md = r.stdout.strip()
        if r.returncode == 0 and md:
            md = re.sub(r"^```(?:markdown)?\n|\n```$", "", md)
            out.write_text(md)
            return n, md
    raise RuntimeError(f"page {n} failed: {r.stderr[-500:]}")


def meta_from(first_page_md, model, engine):
    """Ask the model for title/author/date from the first page."""
    prompt = ("From this first page of a document, return ONLY compact JSON with keys "
              '"title", "author", "date" (YYYY-MM-DD or "" if unknown). Authors comma-separated.\n\n'
              + first_page_md[:6000])
    r = llm(prompt, "haiku" if engine == "claude" else model, engine, timeout=180)
    m = re.search(r"\{.*\}", r.stdout, re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except Exception:
        return {}


def readwise_token():
    tok = os.environ.get("READWISE_TOKEN")
    tf = CONFIG.parent / "readwise-token"  # optional plain-text token file (chmod 600)
    if not tok and tf.exists():
        tok = tf.read_text().strip()
    if not tok and sys.platform == "darwin":
        r = subprocess.run(["security", "find-generic-password", "-s", "readwise-token", "-w"],
                           capture_output=True, text=True)
        tok = r.stdout.strip()
    if not tok:
        sys.exit("No Readwise token. Get one at https://readwise.io/access_token then run:\n"
                 '  security add-generic-password -a "$USER" -s readwise-token -w <TOKEN>')
    return tok


def to_readwise(md_path, meta, src, digest, replace=False):
    # Reader can't render MathML/TeX, so turn math into readable Unicode text first.
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from tex2text import convert
    ast = json.loads(subprocess.run(["pandoc", str(md_path), "-f", "markdown-implicit_figures", "-t", "json"],
                                    capture_output=True, text=True, check=True).stdout)

    def walk(x):
        if isinstance(x, list):
            return [walk(i) for i in x]
        if isinstance(x, dict):
            if x.get("t") == "Image":
                attr, alt, (target, title) = x["c"]
                f = md_path.parent / target
                if f.exists() and not re.match(r"https?:|data:", target):
                    target = "data:image/png;base64," + base64.b64encode(f.read_bytes()).decode()
                return {"t": "Image", "c": [attr, walk(alt), [target, title]]}
            if x.get("t") == "Math":
                kind, tex = x["c"]
                txt = convert(tex)
                if kind["t"] == "DisplayMath":
                    return {"t": "Strong", "c": [{"t": "Str", "c": txt}]}
                return {"t": "Str", "c": txt}
            return {k: walk(v) for k, v in x.items()}
        return x

    html = subprocess.run(["pandoc", "-f", "json", "-t", "html"], input=json.dumps(walk(ast)),
                          capture_output=True, text=True, check=True).stdout
    url = src if re.match(r"https?://", src) else f"https://pdf2reader.local/{digest}"
    payload = {"url": url, "html": html, "should_clean_html": False, "category": "article",
               "title": meta.get("title") or md_path.stem, "tags": ["pdf2reader"], "saved_using": "pdf2reader"}
    if meta.get("author"):
        payload["author"] = meta["author"]
    if re.match(r"\d{4}-\d{2}-\d{2}$", meta.get("date") or ""):
        payload["published_date"] = meta["date"]
    hdr = {"Authorization": f"Token {readwise_token()}", "Content-Type": "application/json"}

    def save():
        req = urllib.request.Request("https://readwise.io/api/v3/save/", data=json.dumps(payload).encode(), headers=hdr)
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")

    status, res = save()
    if status == 200:  # Reader already has this URL and does NOT overwrite it
        if not replace:
            print(f"Readwise Reader: already saved, left unchanged -> {res.get('url', '')}\n"
                  "  use --replace to delete that copy (and its highlights) and save this version", file=sys.stderr)
            return
        req = urllib.request.Request(f"https://readwise.io/api/v3/delete/{res['id']}/", method="DELETE", headers=hdr)
        urllib.request.urlopen(req, timeout=60).close()
        status, res = save()
        print(f"Readwise Reader: replaced -> {res.get('url', '')}", file=sys.stderr)
    else:
        print(f"Readwise Reader: saved -> {res.get('url', '')}", file=sys.stderr)


CONFIG = pathlib.Path.home() / ".config" / "pdf2reader" / "config.json"


def config():
    return json.loads(CONFIG.read_text()) if CONFIG.exists() else {}


def reader_email(cli_value):
    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    if cli_value:  # remember it for next time
        cfg["reader_email"] = cli_value
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(cfg, indent=2))
    addr = cli_value or os.environ.get("READER_EMAIL") or cfg.get("reader_email")
    if not addr:
        sys.exit("No Reader email address. Find it in Reader: Settings -> Add to Library -> Email\n"
                 "(looks like something@library.readwise.io), then run once with --reader-email ADDRESS")
    return addr


def email_epub(epub, to, title, sender):
    """Send the ePub to Readwise Reader through the Gmail API (Reader keeps ePub images; its save API drops them).
    The access token comes from config "gmail_token_command": a command that prints a Gmail access token
    with gmail.send scope; "{sender}" in it is replaced by the sending address (e.g. a service-account
    impersonation helper, or `gcloud auth print-access-token`)."""
    import shlex
    from email.message import EmailMessage
    cmd = config().get("gmail_token_command")
    if not sender or not cmd:
        sys.exit("To email Reader, set \"sender\" and \"gmail_token_command\" in "
                 f"{CONFIG} (see config.example.json).")
    r = subprocess.run(shlex.split(os.path.expanduser(cmd.replace("{sender}", sender))),
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not r.stdout.strip():
        sys.exit(f"Could not get a Gmail token for {sender}: {r.stderr.strip()[-300:]}")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, title
    msg.set_content("Sent by pdf2reader")
    msg.add_attachment(epub.read_bytes(), maintype="application", subtype="epub+zip", filename=epub.name)
    body = json.dumps({"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}).encode()
    req = urllib.request.Request("https://gmail.googleapis.com/gmail/v1/users/me/messages/send", data=body,
                                 headers={"Authorization": f"Bearer {r.stdout.strip()}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        res = json.loads(resp.read())
    sent_at = time.time()
    print(f"Readwise Reader: emailed {epub.name} from {sender} to {to} (Gmail id {res.get('id')}); "
          "shows up in Reader within a few minutes", file=sys.stderr)


def delete_email_stub(subject, wait=360):
    """Reader saves the email itself as a 3-word 'email' document next to the ePub; remove it."""
    try:
        tok = readwise_token()
    except SystemExit:
        print("  (no Readwise token here; email stub left in Reader)", file=sys.stderr)
        return
    hdr = {"Authorization": f"Token {tok}"}
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(20)
        url = f"https://readwise.io/api/v3/list/?category=email&updatedAfter={since}"
        try:
            res = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=30).read())
        except Exception:
            continue
        stubs = [d for d in res.get("results", []) if (d.get("title") or "") == subject]
        if stubs:
            for d in stubs:
                urllib.request.urlopen(urllib.request.Request(
                    f"https://readwise.io/api/v3/delete/{d['id']}/", method="DELETE", headers=hdr), timeout=30).close()
            print(f"Readwise Reader: removed the email stub ({len(stubs)})", file=sys.stderr)
            return
    print("  (email stub not seen within 6 min; left in Reader)", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("-o", "--out")
    ap.add_argument("--engine", choices=["claude", "pi"],
                    default="pi" if shutil.which("pi") else "claude")
    ap.add_argument("--model", help="default: sonnet (claude engine) or openai-codex/gpt-6.1-sol (pi engine)")
    ap.add_argument("--dpi", type=int, default=170)
    ap.add_argument("-j", "--jobs", type=int, default=6)
    ap.add_argument("--pages")
    ap.add_argument("--epub", action="store_true")
    ap.add_argument("--readwise", action="store_true", help="email the ePub (with images) to your Reader address")
    ap.add_argument("--reader-email", help="your Reader address (…@library.readwise.io); remembered after first use")
    ap.add_argument("--keep-stub", action="store_true", help="don't delete Reader's email-body document")
    ap.add_argument("--sender", default=config().get("sender"), help="Gmail address to send from (config: sender)")
    ap.add_argument("--readwise-api", action="store_true",
                    help="old method: save HTML through the Reader API (text only — Reader drops the images)")
    ap.add_argument("--replace", action="store_true", help="with --readwise: overwrite an existing Reader copy (loses its highlights)")
    ap.add_argument("--title")
    ap.add_argument("--author")
    ap.add_argument("--no-images", dest="images", action="store_false", help="text only, no figure crops")
    ap.add_argument("--fig-dpi", type=int, default=200)
    ap.add_argument("--fresh", action="store_true", help="ignore page cache")
    args = ap.parse_args()
    args.model = args.model or ("sonnet" if args.engine == "claude" else "openai-codex/gpt-6.1-sol")

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="pdf2reader-"))
    pdf = fetch(args.src, tmp)
    digest = hashlib.sha1(pdf.read_bytes()).hexdigest()[:12]
    cache = pathlib.Path.home() / ".cache" / "pdf2reader" / (f"{digest}-{args.model.replace('/', '_')}-{args.dpi}" + ("-img2" if args.images else ""))
    cache.mkdir(parents=True, exist_ok=True)
    if args.fresh:
        for f in cache.glob("page-*.md"):
            f.unlink()

    total = len(fitz.open(pdf))
    pages = parse_pages(args.pages, total)
    name = re.sub(r"\.pdf$", "", pathlib.Path(re.sub(r"\?.*", "", args.src)).name, flags=re.I)
    out = pathlib.Path(args.out or f"{name}.md").expanduser()
    print(f"{total} pages -> {out}  (model={args.model}, dpi={args.dpi}, jobs={args.jobs}, cache={cache})", file=sys.stderr)

    results, failed = {}, []
    with cf.ThreadPoolExecutor(args.jobs) as ex:
        futs = {ex.submit(convert_page, pdf, n, total, args, cache): n for n in pages}
        for f in cf.as_completed(futs):
            n = futs[f]
            try:
                results[n] = f.result()[1]
                print(f"  page {n} ok ({len(results)}/{len(pages)})", file=sys.stderr)
            except Exception as e:
                failed.append(n)
                print(f"  page {n} FAILED: {e}", file=sys.stderr)

    # drop running-title headings repeated on later pages
    seen_h1 = None
    for n in sorted(results):
        lines = []
        for ln in results[n].splitlines():
            if ln.startswith("# "):
                if seen_h1 is None:
                    seen_h1 = ln.strip()
                elif ln.strip() == seen_h1:
                    continue
            lines.append(ln)
        results[n] = "\n".join(lines).strip()
    # swap FIG_k placeholders for real image files next to the .md
    assets = out.parent / f"{out.stem}_images"
    if args.images and assets.exists():
        shutil.rmtree(assets)
    n_img = 0
    for n in sorted(results):
        def place(m, n=n):
            nonlocal n_img
            src = cache / f"fig-{n:04d}-{m.group(2)}.png"
            if not src.exists():
                return ""
            assets.mkdir(exist_ok=True)
            dst = assets / f"p{n}-{m.group(2)}.png"
            shutil.copy(src, dst)
            n_img += 1
            return f"![{m.group(1)}]({assets.name}/{dst.name})"
        # fallback: figures the model skipped go under the matching "**Figure N" label line,
        # or at the end of the page if the labels don't line up with the detected regions
        n_regions = len(list(cache.glob(f"fig-{n:04d}-*.png")))
        used = {int(k) for k in re.findall(r"\]\(FIG_(\d+)\)", results[n])}
        missing = [k for k in range(1, n_regions + 1) if k not in used]
        if missing:
            lines = results[n].split("\n")
            labels = [i for i, ln in enumerate(lines) if re.match(r"\*\*Figure\b", ln)]
            tail = []
            for k in reversed(missing):
                tag = f"![Figure](FIG_{k})"
                if len(labels) == n_regions:
                    lines.insert(labels[k - 1] + 1, "\n" + tag + "\n")
                else:
                    tail.insert(0, tag)
            results[n] = "\n".join(lines + ([""] + tail if tail else []))
        results[n] = re.sub(r"!\[([^\]]*)\]\(FIG_(\d+)\)", place, results[n])
        results[n] = re.sub(r"\bFIG_\d+\b", "", results[n])
    if args.images:
        print(f"placed {n_img} images in {assets}", file=sys.stderr)
    body = "\n\n".join(f"<!-- page {n} -->\n{results[n]}" for n in sorted(results))
    out.write_text(body + "\n")
    print(f"wrote {out} ({len(body):,} chars)" + (f"; failed pages: {failed} — re-run to retry" if failed else ""), file=sys.stderr)
    if failed:
        sys.exit(1)

    if args.readwise:
        args.epub = True
        to = reader_email(args.reader_email)
    if args.epub or args.readwise or args.readwise_api:
        meta = meta_from(fitz.open(pdf)[0].get_text() + "\n\n" + results.get(min(results), ""), args.model, args.engine)
        if args.title: meta["title"] = args.title
        if args.author: meta["author"] = args.author
        print(f"metadata: {meta}", file=sys.stderr)
    if args.epub:
        epub = out.with_suffix(".epub")
        cmd = ["pandoc", str(out), "-f", "markdown-implicit_figures", "-o", str(epub),
               "--resource-path", str(out.parent), "--mathml", "--toc", "--toc-depth=2",
               "--split-level=2", "-M", f"title={meta.get('title') or out.stem}"]
        if meta.get("author"): cmd += ["-M", f"author={meta['author']}"]
        if meta.get("date"): cmd += ["-M", f"date={meta['date']}"]
        subprocess.run(cmd, check=True)
        print(f"wrote {epub}", file=sys.stderr)
    if args.readwise:
        email_epub(epub, to, meta.get("title") or out.stem, args.sender)
        if not args.keep_stub:
            delete_email_stub(meta.get("title") or out.stem)
    if args.readwise_api:
        to_readwise(out, meta, args.src, digest, args.replace)


if __name__ == "__main__":
    main()
