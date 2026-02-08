#!/usr/bin/env python3
"""
export_static_site.py

Generate a static site with one HTML page per lemma, using lexdb_sql1.html
as the UI template. Each page embeds the lemma data so it works offline.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Dict, List, Tuple

import psycopg
from psycopg.rows import dict_row

DSN = "dbname=lexdb_gemini user=David.He host=localhost"
TEMPLATE_PATH = Path("lexdb_sql1.html")

SLUG_SAFE_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    if not text:
        return "untitled"
    s = unicodedata.normalize("NFKC", text).strip().lower()
    s = SLUG_SAFE_RE.sub("-", s).strip("-")
    return s or "untitled"


def load_template() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def inject_data(template: str, data: Dict) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    title = (data.get("lemma") or "Logodaedaly").strip()
    inject = (
        f"<script>window.__LEMMA_DATA__ = {payload};"
        f"document.title = {json.dumps(title + ' — Logodaedaly')};"
        f"</script>\n"
    )
    marker = '<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>'
    if marker not in template:
        raise ValueError("Template missing supabase script tag.")
    return template.replace(marker, inject + marker, 1)

def inject_index(template: str) -> str:
    inject = (
        "<script>"
        f"window.__STATIC_INDEX__ = true;"
        f"window.__API_BASE__ = '';"
        "window.__MOBILE__ = window.matchMedia('(max-width: 760px)').matches;"
        "document.title = 'Logodaedaly — English Dictionary for Serious Learners';"
        "</script>\n"
    )
    marker = '<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>'
    if marker not in template:
        raise ValueError("Template missing supabase script tag.")
    return template.replace(marker, inject + marker, 1)


def fetch_entries() -> List[Dict]:
    query = """
        SELECT e.id AS entry_id, e.lemma, e.pos, e.ipa, e.freq, e.morphology,
               e.etymology, e.related,
               s.id AS sense_id, s.attr, s.def, s.ex
        FROM test.entries e
        JOIN test.senses s ON s.entry_id = e.id
        ORDER BY e.lemma, s.id
    """
    entries: Dict[str, Dict] = {}
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            for row in cur:
                lemma = row["lemma"]
                if lemma not in entries:
                    entries[lemma] = {
                        "lemma": row["lemma"],
                        "ipa": row["ipa"],
                        "pos": row["pos"],
                        "freq": row["freq"],
                        "morphology": row["morphology"],
                        "etymology": row["etymology"],
                        "related": row["related"],
                        "senses": [],
                    }
                entries[lemma]["senses"].append(
                    {
                        "id": row["sense_id"],
                        "attr": row["attr"],
                        "def": row["def"],
                        "ex": row["ex"],
                    }
                )
    return list(entries.values())


def ensure_unique_slug(slug: str, used: Dict[str, int]) -> str:
    if slug not in used:
        used[slug] = 1
        return slug
    used[slug] += 1
    return f"{slug}-{used[slug]}"


def chunked(items: List[Dict], size: int) -> List[List[Dict]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def write_sitemap(out_dir: Path, manifest: List[Dict[str, str]], base_url: str) -> int:
    base = base_url.rstrip("/")
    urls = [f"{base}/"] + [f"{base}/lemma/{m['slug']}" for m in manifest]

    def render_urlset(url_list: List[str]) -> str:
        lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for u in url_list:
            lines.append(f"  <url><loc>{u}</loc></url>")
        lines.append("</urlset>")
        return "\n".join(lines) + "\n"

    # Split into multiple sitemaps if over 50k URLs.
    max_urls = 50000
    if len(urls) <= max_urls:
        (out_dir / "sitemap.xml").write_text(render_urlset(urls), encoding="utf-8")
        return 1

    chunks = [urls[i:i + max_urls] for i in range(0, len(urls), max_urls)]
    sitemap_files = []
    for i, chunk in enumerate(chunks, 1):
        name = f"sitemap-{i}.xml"
        (out_dir / name).write_text(render_urlset(chunk), encoding="utf-8")
        sitemap_files.append(name)

    index_lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                   '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for name in sitemap_files:
        index_lines.append(f"  <sitemap><loc>{base}/{name}</loc></sitemap>")
    index_lines.append("</sitemapindex>")
    (out_dir / "sitemap.xml").write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    return len(sitemap_files)


def write_site(out_dir: Path, chunk_size: int, base_url: str) -> Tuple[int, int, int]:
    template = load_template()
    entries = fetch_entries()
    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    used_slugs: Dict[str, int] = {}
    manifest: List[Dict[str, str]] = []

    enriched: List[Dict] = []
    for entry in entries:
        slug = ensure_unique_slug(slugify(entry["lemma"]), used_slugs)
        enriched.append({**entry, "slug": slug})

    chunks = chunked(enriched, chunk_size)
    for i, chunk in enumerate(chunks):
        chunk_name = f"chunk-{i:04d}.json"
        for item in chunk:
            manifest.append({"lemma": item["lemma"], "slug": item["slug"], "chunk": chunk_name})
        (data_dir / chunk_name).write_text(json.dumps(chunk, ensure_ascii=False), encoding="utf-8")

    # Write a static manifest for landing animation/search + routing.
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    # Write index page (auto-adapts to mobile).
    index_path = out_dir / "index.html"
    index_html = inject_index(template)
    index_path.write_text(index_html, encoding="utf-8")

    # SPA fallback for Cloudflare Pages
    redirects_path = out_dir / "_redirects"
    redirects_path.write_text(
        "/index.html /index.html 200\n"
        "/lemma/* /index.html 200\n"
        "/* /index.html 200\n",
        encoding="utf-8"
    )

    sitemap_count = write_sitemap(out_dir, manifest, base_url)
    return len(entries), len(chunks), sitemap_count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site", help="Output folder for static site")
    ap.add_argument("--chunk-size", type=int, default=1000)
    ap.add_argument("--base-url", default=os.environ.get("LOGODAEDALY_BASE_URL", "https://logodaedaly.pages.dev"),
                    help="Base URL for sitemap (default: https://logodaedaly.pages.dev)")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    count, chunks, sitemaps = write_site(out_dir, args.chunk_size, args.base_url)
    print(f"Wrote {count} entries into {chunks} data chunks in {out_dir}")
    print(f"Wrote {sitemaps} sitemap file(s) in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
