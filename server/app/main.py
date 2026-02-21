from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown import markdown
from starlette.requests import Request

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("KOBO_DB_PATH", str(BASE_DIR.parent / "server.db")))


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Kobo Highlights", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
  content_id TEXT PRIMARY KEY,
  title TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chapters (
  content_id TEXT PRIMARY KEY,
  book_content_id TEXT NOT NULL,
  title TEXT NOT NULL,
  FOREIGN KEY(book_content_id) REFERENCES books(content_id)
);
CREATE TABLE IF NOT EXISTS bookmarks (
  bookmark_id TEXT PRIMARY KEY,
  book_content_id TEXT NOT NULL,
  chapter_content_id TEXT NOT NULL,
  text TEXT NOT NULL,
  annotation TEXT,
  date_created TEXT,
  date_modified TEXT,
  chapter_progress REAL,
  FOREIGN KEY(book_content_id) REFERENCES books(content_id),
  FOREIGN KEY(chapter_content_id) REFERENCES chapters(content_id)
);
CREATE INDEX IF NOT EXISTS idx_bookmarks_book ON bookmarks(book_content_id);
CREATE INDEX IF NOT EXISTS idx_bookmarks_chapter ON bookmarks(chapter_content_id);
"""


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_db() as db:
        db.executescript(SCHEMA)
        ensure_schema(db)
        backfill_slugs(db)


def ensure_schema(db: sqlite3.Connection) -> None:
    if not column_exists(db, "books", "slug"):
        db.execute("ALTER TABLE books ADD COLUMN slug TEXT")
    if not column_exists(db, "chapters", "slug"):
        db.execute("ALTER TABLE chapters ADD COLUMN slug TEXT")
    if not column_exists(db, "chapters", "book_slug"):
        db.execute("ALTER TABLE chapters ADD COLUMN book_slug TEXT")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_books_slug ON books(slug)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_chapters_slug ON chapters(slug)")


def column_exists(db: sqlite3.Connection, table: str, column: str) -> bool:
    rows = db.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def backfill_slugs(db: sqlite3.Connection) -> None:
    books = db.execute(
        "SELECT content_id, title, slug FROM books WHERE slug IS NULL"
    ).fetchall()
    for book in books:
        db.execute(
            "UPDATE books SET slug = ? WHERE content_id = ?",
            (make_slug(book["title"], book["content_id"]), book["content_id"]),
        )

    chapters = db.execute(
        """
        SELECT c.content_id, c.title, c.slug, c.book_content_id,
               b.slug AS book_slug
        FROM chapters c
        JOIN books b ON b.content_id = c.book_content_id
        WHERE c.slug IS NULL OR c.book_slug IS NULL
        """
    ).fetchall()
    for chapter in chapters:
        db.execute(
            """
            UPDATE chapters SET slug = ?, book_slug = ?
            WHERE content_id = ?
            """,
            (
                make_slug(chapter["title"], chapter["content_id"]),
                chapter["book_slug"],
                chapter["content_id"],
            ),
        )


def markdownify(text: str) -> str:
    text = re.sub(r"^\s*(.*)", r"> \1\n> ", text, flags=re.MULTILINE)
    text = re.sub(r"(^>\s*$\n?){2,}", ">\n", text, flags=re.MULTILINE)
    text = re.sub(r"^>\s*$(^$){2,}", "", text, flags=re.MULTILINE)
    return text


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "item"


def short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]


def make_slug(title: str, content_id: str) -> str:
    return f"{slugify(title)}-{short_hash(content_id)}"


def import_kobo_db(path: str) -> int:
    try:
        source = sqlite3.connect(path)
        source.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid sqlite file: {exc}"
        ) from exc

    query = """
    SELECT
        b.BookmarkID as bookmark_id,
        b.VolumeID as book_id,
        b.ContentID as chapter_id,
        c.Title as title,
        COALESCE(c_chapter_titled.Title, c_chapter.Title, b.ContentID) as chapter,
        b.Text as text,
        b.Annotation as annotation,
        b.DateCreated as date_created,
        b.DateModified as date_modified,
        b.ChapterProgress as chapter_progress
    FROM
        Bookmark b
        INNER JOIN content c ON b.VolumeID = c.ContentID
        LEFT JOIN content c_chapter_titled ON c_chapter_titled.ContentID = b.ContentID || '-1'
        LEFT JOIN content c_chapter ON c_chapter.ContentID = b.ContentID
    WHERE
        b.Text IS NOT NULL AND b.Text != ''
    """

    rows = source.execute(query).fetchall()
    if not rows:
        return 0

    with get_db() as db:
        for row in rows:
            book_slug = make_slug(row["title"], row["book_id"])
            chapter_slug = make_slug(row["chapter"], row["chapter_id"])
            db.execute(
                """
                INSERT INTO books (content_id, title, slug)
                VALUES (?, ?, ?)
                ON CONFLICT(content_id) DO UPDATE SET
                    title=excluded.title,
                    slug=excluded.slug
                """,
                (row["book_id"], row["title"], book_slug),
            )
            db.execute(
                """
                INSERT INTO chapters (content_id, book_content_id, title, slug, book_slug)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(content_id) DO UPDATE SET
                    book_content_id=excluded.book_content_id,
                    title=excluded.title,
                    slug=excluded.slug,
                    book_slug=excluded.book_slug
                """,
                (
                    row["chapter_id"],
                    row["book_id"],
                    row["chapter"],
                    chapter_slug,
                    book_slug,
                ),
            )
            db.execute(
                """
                INSERT INTO bookmarks (
                    bookmark_id,
                    book_content_id,
                    chapter_content_id,
                    text,
                    annotation,
                    date_created,
                    date_modified,
                    chapter_progress
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bookmark_id) DO UPDATE SET
                    book_content_id=excluded.book_content_id,
                    chapter_content_id=excluded.chapter_content_id,
                    text=excluded.text,
                    annotation=excluded.annotation,
                    date_created=excluded.date_created,
                    date_modified=excluded.date_modified,
                    chapter_progress=excluded.chapter_progress
                """,
                (
                    row["bookmark_id"],
                    row["book_id"],
                    row["chapter_id"],
                    row["text"],
                    row["annotation"],
                    row["date_created"],
                    row["date_modified"],
                    row["chapter_progress"],
                ),
            )
    return len(rows)


def fetch_book(db: sqlite3.Connection, book_slug: str) -> sqlite3.Row:
    book = db.execute(
        "SELECT content_id, title, slug FROM books WHERE slug = ?", (book_slug,)
    ).fetchone()
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    return book


def fetch_chapter(
    db: sqlite3.Connection, book_slug: str, chapter_slug: str
) -> sqlite3.Row:
    chapter = db.execute(
        """
        SELECT content_id, book_content_id, title, slug, book_slug
        FROM chapters
        WHERE slug = ? AND book_slug = ?
        """,
        (chapter_slug, book_slug),
    ).fetchone()
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")
    return chapter


def fetch_chapters(db: sqlite3.Connection, book_id: str) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT c.content_id, c.title, c.slug,
               MIN(b.chapter_progress) AS first_progress,
               COUNT(*) AS highlight_count
        FROM chapters c
        JOIN bookmarks b ON b.chapter_content_id = c.content_id
        WHERE c.book_content_id = ?
        GROUP BY c.content_id, c.title
        ORDER BY first_progress ASC, c.title ASC
        """,
        (book_id,),
    ).fetchall()


def fetch_bookmarks(
    db: sqlite3.Connection, book_id: str, chapter_id: str
) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT text, annotation, date_created, date_modified, chapter_progress
        FROM bookmarks
        WHERE book_content_id = ? AND chapter_content_id = ?
        ORDER BY chapter_progress ASC, date_created ASC
        """,
        (book_id, chapter_id),
    ).fetchall()


def build_markdown(
    db: sqlite3.Connection, book_slug: str, chapter_slug: str | None = None
) -> str:
    book = fetch_book(db, book_slug)
    lines: list[str] = [f"# {book['title']}", ""]

    chapters = (
        [fetch_chapter(db, book_slug, chapter_slug)]
        if chapter_slug
        else fetch_chapters(db, book["content_id"])
    )

    for chapter in chapters:
        lines.append(f"## {chapter['title']}")
        lines.append("")
        for row in fetch_bookmarks(db, book["content_id"], chapter["content_id"]):
            lines.append(markdownify(row["text"]))
            lines.append("")
            if row["annotation"]:
                lines.append(row["annotation"])
                lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def fetch_highlights(
    db: sqlite3.Connection, query_text: str | None
) -> list[sqlite3.Row]:
    if query_text:
        like = f"%{query_text}%"
        return db.execute(
            """
                 SELECT b.book_content_id, b.chapter_content_id,
                     bk.slug AS book_slug, ch.slug AS chapter_slug,
                   b.text, b.annotation, b.date_created, b.date_modified,
                   bk.title AS book_title, ch.title AS chapter_title
            FROM bookmarks b
            JOIN books bk ON bk.content_id = b.book_content_id
            JOIN chapters ch ON ch.content_id = b.chapter_content_id
            WHERE bk.title LIKE ? OR ch.title LIKE ? OR b.text LIKE ? OR b.annotation LIKE ?
            ORDER BY COALESCE(b.date_modified, b.date_created) DESC
            LIMIT 50
            """,
            (like, like, like, like),
        ).fetchall()

    return db.execute(
        """
         SELECT b.book_content_id, b.chapter_content_id,
             bk.slug AS book_slug, ch.slug AS chapter_slug,
               b.text, b.annotation, b.date_created, b.date_modified,
               bk.title AS book_title, ch.title AS chapter_title
        FROM bookmarks b
        JOIN books bk ON bk.content_id = b.book_content_id
        JOIN chapters ch ON ch.content_id = b.chapter_content_id
        ORDER BY COALESCE(b.date_modified, b.date_created) DESC
        LIMIT 50
        """
    ).fetchall()


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/books")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/books", response_class=HTMLResponse)
def list_books(request: Request) -> HTMLResponse:
    with get_db() as db:
        books = db.execute(
            """
            SELECT b.content_id, b.title, b.slug, COUNT(m.bookmark_id) AS highlight_count
            FROM books b
            LEFT JOIN bookmarks m ON m.book_content_id = b.content_id
            GROUP BY b.content_id, b.title
            ORDER BY b.title ASC
            """
        ).fetchall()

    return templates.TemplateResponse(
        "books.html", {"request": request, "books": books}
    )


@app.get("/highlights", response_class=HTMLResponse)
def latest_highlights(request: Request, q: str | None = None) -> HTMLResponse:
    with get_db() as db:
        highlights = fetch_highlights(db, q)

    return templates.TemplateResponse(
        "highlights.html",
        {
            "request": request,
            "highlights": highlights,
            "query": q or "",
        },
    )


@app.get("/books/{book_slug}", response_class=HTMLResponse)
def book_detail(book_slug: str, request: Request) -> HTMLResponse:
    with get_db() as db:
        book = fetch_book(db, book_slug)
        chapters = fetch_chapters(db, book["content_id"])

    return templates.TemplateResponse(
        "book.html",
        {
            "request": request,
            "book": book,
            "chapters": chapters,
        },
    )


@app.get("/books/{book_slug}/chapter/{chapter_slug}", response_class=HTMLResponse)
def chapter_detail(book_slug: str, chapter_slug: str, request: Request) -> HTMLResponse:
    with get_db() as db:
        book = fetch_book(db, book_slug)
        chapter = fetch_chapter(db, book_slug, chapter_slug)
        markdown_text = build_markdown(db, book_slug, chapter_slug)
        html = markdown(markdown_text)

    return templates.TemplateResponse(
        "chapter.html",
        {
            "request": request,
            "book": book,
            "chapter": chapter,
            "html": html,
        },
    )


@app.get("/books/{book_slug}.md", response_class=PlainTextResponse)
def book_markdown(book_slug: str) -> PlainTextResponse:
    with get_db() as db:
        markdown_text = build_markdown(db, book_slug)
    return PlainTextResponse(markdown_text, media_type="text/markdown")


@app.get(
    "/books/{book_slug}/chapter/{chapter_slug}.md", response_class=PlainTextResponse
)
def chapter_markdown(book_slug: str, chapter_slug: str) -> PlainTextResponse:
    with get_db() as db:
        markdown_text = build_markdown(db, book_slug, chapter_slug)
    return PlainTextResponse(markdown_text, media_type="text/markdown")


@app.post("/upload")
async def upload(db: UploadFile = File(...)) -> dict:
    if not db.filename:
        raise HTTPException(status_code=400, detail="Missing file")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite") as temp:
        shutil.copyfileobj(db.file, temp)
        temp_path = temp.name

    try:
        count = import_kobo_db(temp_path)
    except sqlite3.Error as exc:
        raise HTTPException(status_code=400, detail=f"Import failed: {exc}") from exc
    finally:
        Path(temp_path).unlink(missing_ok=True)

    return {"imported": count}
