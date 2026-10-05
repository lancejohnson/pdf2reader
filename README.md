# pdf2reader

Turn any PDF into a clean, readable ePub — real headings, re-flowed paragraphs, tables as tables,
and the **figures, charts and slides cropped in as images** — and send it to
[Readwise Reader](https://readwise.io/read). Includes a tiny web service so you can do it from the
iPhone Share sheet.

Plain text extraction (`pdftotext` and friends) falls apart on slide decks, scanned pages and
tables drawn as vector graphics. pdf2reader renders each page to an image and has a vision model
rebuild it as Markdown, using the PDF's own text layer only to get spelling and numbers exact.

## How it works

1. Each page is rendered to PNG. Graphic regions (vector-drawing clusters and embedded images) are
   detected and cropped at 200 dpi.
2. A vision model gets the page image, its text layer and the list of figure crops, and writes the
   page as Markdown: running headers/footers dropped, tables as pipe tables, every figure placed
   inline with its text still transcribed underneath (so it stays searchable and highlightable).
3. Pages run in parallel and are cached, so re-runs only redo failures.
4. pandoc builds an ePub (with images and MathML), which is emailed to your Reader address.
   Reader keeps images in emailed ePubs; its save API drops them.
5. Reader also saves the email body as a tiny separate document; pdf2reader deletes that stub.

## Requirements

- Python 3.10+, `pip install -r requirements.txt` (PyMuPDF, google-auth)
- [pandoc](https://pandoc.org)
- A model runner: [`pi`](https://github.com/badlogic/pi-mono) (default; any model it lists,
  e.g. `openai-codex/gpt-6.1-sol`) or [Claude Code](https://claude.com/claude-code) (`--engine claude`)
- For `--readwise`: a Gmail/Workspace account you can get an API access token for, and your
  Readwise access token (to remove the email stub)

## Usage

```sh
pdf2reader paper.pdf                       # -> paper.md + paper_images/
pdf2reader https://example.com/x.pdf --epub
pdf2reader paper.pdf --readwise            # ePub emailed to Reader
pdf2reader paper.pdf --pages 1-5,9 --model openai-codex/gpt-5.5 -j 8
pdf2reader paper.pdf --no-images           # text only
```

## Configuration

`~/.config/pdf2reader/config.json` (see `config.example.json`):

| key | meaning |
|---|---|
| `reader_email` | your Reader address — Reader → Settings → Add to Library → Email |
| `sender` | Gmail address to send from |
| `gmail_token_command` | command that prints a Gmail access token with `gmail.send` scope; `{sender}` is replaced by the sender address |

Readwise token (only used to delete the email stub): `$READWISE_TOKEN`, the file
`~/.config/pdf2reader/readwise-token`, or the macOS Keychain item `readwise-token`.
Get one at <https://readwise.io/access_token>.

## iPhone Share sheet

iOS doesn't let web apps join the Share sheet, so this uses a Shortcut that posts to `server.py`.

1. Run `server.py` on an always-on machine (see `pdf2reader.service` for systemd). It listens on
   `127.0.0.1:8450`; expose it privately, e.g. `tailscale serve --bg --https=8449 http://127.0.0.1:8450`.
   There is no authentication — **don't put it on the public internet.**
2. On the iPhone: Shortcuts → **+** → name it *Send to Reader* → ⓘ → **Show in Share Sheet**
   (PDFs, URLs, Safari web pages, Files).
3. Add **Get Contents of URL**: `https://<your-host>:8449/submit`, Method **POST**, Request Body
   **Form**, field `file` (type File) = *Shortcut Input*.
4. Add **Show Notification** with *Contents of URL*.

Opening the server's address in Safari shows a status page with recent jobs and an upload form;
Share → Add to Home Screen gives it an app icon.

`POST /submit` accepts a raw PDF body, a multipart PDF, or a link to a PDF (form field or text body).

## Automatic: convert every PDF you save to Reader

`server.py` also listens on `127.0.0.1:8451` for Readwise webhooks. Expose just that port publicly
(e.g. `tailscale funnel --bg --set-path /pdf2reader-hook http://127.0.0.1:8451`), then at
<https://readwise.io/webhook> create a webhook for that URL with the event
**reader.non_feed_document.created**. Put the webhook's secret in config as `webhook_secret`.

For each new PDF saved from a link, it downloads the original, emails the ePub, and deletes the
original PDF from Reader. Every event is checked against your library with your own token, so
only your real PDFs are acted on. Uploaded PDF files are skipped: Reader's API won't hand their
bytes back (use the Share-sheet Shortcut for those).

## Other options

- `--readwise-api` — save HTML through the Reader API instead of email (text only; Reader drops
  images). `--replace` deletes an existing copy with the same source first.
- `--keep-stub` — leave Reader's email-body document alone.
- `--engine claude|pi`, `--model`, `--dpi`, `--fig-dpi`, `--fresh`, `--title`, `--author`.

## License

MIT
