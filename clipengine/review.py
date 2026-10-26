"""Local review page: approve / reject / tweak drafts, then log performance once posted."""
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

from flask import Flask, abort, redirect, render_template_string, request, send_file, url_for

from . import pipeline, store
from .config import Config

PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clip review</title>
<style>
:root{--bg:#0f1115;--card:#181b22;--fg:#e8eaf0;--mute:#9aa1ad;--ok:#2fbf71;--no:#e5484d;--acc:#f5c542}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,system-ui,sans-serif}
header{display:flex;gap:16px;align-items:center;padding:14px 20px;border-bottom:1px solid #262a33;position:sticky;top:0;background:var(--bg)}
header a{color:var(--mute);text-decoration:none}header a.on{color:var(--fg);font-weight:600}
main{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:18px;padding:20px}
.card{background:var(--card);border-radius:12px;padding:14px;display:flex;flex-direction:column;gap:8px}
video{width:100%;aspect-ratio:9/16;background:#000;border-radius:8px}
input,textarea{width:100%;box-sizing:border-box;background:#0f1115;color:var(--fg);border:1px solid #2c313c;border-radius:6px;padding:6px 8px;font:inherit}
textarea{min-height:56px}.meta{color:var(--mute);font-size:13px}.score{color:var(--acc);font-weight:700}
.row{display:flex;gap:8px}.row>*{flex:1}
button{border:0;border-radius:6px;padding:8px;font-weight:600;cursor:pointer;color:#fff;background:#3a4150}
.ok{background:var(--ok)}.no{background:var(--no)}
label{font-size:12px;color:var(--mute)}
.empty{padding:40px;color:var(--mute)}
</style></head><body>
<header><strong>Clip engine</strong>
<a href="/" class="{{'on' if tab=='pending' else ''}}">To review ({{counts.pending}})</a>
<a href="/approved" class="{{'on' if tab=='approved' else ''}}">Approved / posted ({{counts.approved}})</a>
<span class="meta">Approved clips are copied to {{ready}}</span></header>
<main>
{% for c in clips %}
<div class="card">
  <video src="/video/{{c.id}}?v={{c.vstamp}}" controls preload="metadata"></video>
  <div class="meta"><span class="score">{{c.score}}</span> · {{c.creator}} · {{'%.0f'|format(c.end-c.start)}}s · {{c.why}}</div>
  {% if tab == 'pending' %}
  <form method="post" action="/decide/{{c.id}}">
    <label>Title</label><input name="title" value="{{c.title}}">
    <label>Caption</label><textarea name="caption">{{c.caption}}</textarea>
    <label>Reason (optional — teaches the engine your taste)</label><input name="reason" placeholder="e.g. weak hook, needs context, great payoff">
    <div class="row" style="margin-top:8px"><button class="ok" name="action" value="approve">Approve</button><button class="no" name="action" value="reject">Reject</button></div>
  </form>
  <form method="post" action="/rerender/{{c.id}}">
    <label>Hook text</label><input name="hook" value="{{c.hook}}">
    <div class="row"><div><label>Trim start (s)</label><input name="trim_start" value="0"></div><div><label>Trim end (s)</label><input name="trim_end" value="0"></div></div>
    <button style="margin-top:8px;width:100%">Re-render</button>
  </form>
  {% else %}
  <div><strong>{{c.title}}</strong></div><div class="meta">{{c.status}}{% if c.posted_url %} · <a href="{{c.posted_url}}" style="color:var(--acc)">link</a>{% endif %}</div>
  <form method="post" action="/stats/{{c.id}}">
    <label>Posted URL</label><input name="posted_url" value="{{c.posted_url or ''}}">
    <div class="row"><div><label>Views</label><input name="views" value="{{c.views or ''}}"></div><div><label>Earnings ($)</label><input name="earnings" value="{{c.earnings or ''}}"></div></div>
    <button style="margin-top:8px;width:100%">Save stats</button>
  </form>
  {% endif %}
</div>
{% else %}<div class="empty">Nothing here. Run <code>clip daily</code> or <code>clip run &lt;url&gt; --creator NAME</code>.</div>
{% endfor %}
</main></body></html>"""


def create_app(cfg: Config) -> Flask:
    app = Flask(__name__)

    def db():
        return store.connect(cfg.data_dir)

    def counts(con):
        q = lambda s: con.execute(f"SELECT COUNT(*) FROM clips WHERE status IN ({s})").fetchone()[0]
        return {"pending": q("'pending'"), "approved": q("'approved','posted'")}

    def rows(con, statuses: str, order: str):
        out = []
        for r in con.execute(
                f"SELECT c.*, v.creator FROM clips c JOIN videos v ON v.id = c.video_id "
                f"WHERE c.status IN ({statuses}) ORDER BY {order}"):
            d = dict(r)
            p = Path(d["path"])
            d["vstamp"] = int(p.stat().st_mtime) if p.exists() else 0
            out.append(d)
        return out

    @app.get("/")
    def pending():
        con = db()
        return render_template_string(PAGE, tab="pending", clips=rows(con, "'pending'", "c.score DESC"),
                                      counts=counts(con), ready=cfg.ready_dir)

    @app.get("/approved")
    def approved():
        con = db()
        return render_template_string(PAGE, tab="approved",
                                      clips=rows(con, "'approved','posted'", "c.decided_at DESC"),
                                      counts=counts(con), ready=cfg.ready_dir)

    @app.get("/video/<int:cid>")
    def video(cid: int):
        row = db().execute("SELECT path FROM clips WHERE id = ?", (cid,)).fetchone()
        if not row or not Path(row["path"]).exists():
            abort(404)
        return send_file(row["path"], mimetype="video/mp4", conditional=True)

    @app.post("/decide/<int:cid>")
    def decide(cid: int):
        con = db()
        action = request.form["action"]
        title, caption, reason = request.form["title"], request.form["caption"], request.form.get("reason", "")
        status = "approved" if action == "approve" else "rejected"
        con.execute("UPDATE clips SET status=?, title=?, caption=?, reason=?, decided_at=CURRENT_TIMESTAMP WHERE id=?",
                    (status, title, caption, reason, cid))
        con.commit()
        if status == "approved":
            export(cfg, con, cid)
        return redirect(url_for("pending"))

    @app.post("/rerender/<int:cid>")
    def rerender(cid: int):
        f = request.form
        pipeline.rerender(cfg, db(), cid, f["hook"], float(f.get("trim_start") or 0), float(f.get("trim_end") or 0))
        return redirect(url_for("pending"))

    @app.post("/stats/<int:cid>")
    def stats(cid: int):
        f = request.form
        con = db()
        con.execute("UPDATE clips SET posted_url=?, views=?, earnings=?, status=CASE WHEN ?<>'' THEN 'posted' ELSE status END WHERE id=?",
                    (f.get("posted_url") or None, int(f["views"]) if f.get("views") else None,
                     float(f["earnings"]) if f.get("earnings") else None, f.get("posted_url", ""), cid))
        con.commit()
        return redirect(url_for("approved"))

    return app


def export(cfg: Config, con, cid: int) -> Path:
    """Copy an approved clip + its post text into ready/<date>/ for manual upload."""
    r = con.execute("SELECT c.*, v.creator, v.url FROM clips c JOIN videos v ON v.id = c.video_id WHERE c.id = ?",
                    (cid,)).fetchone()
    creator = cfg.creator(r["creator"])
    folder = cfg.ready_dir / date.today().isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{cid:05d}-{pipeline.slug(r['title'])}"
    shutil.copy2(r["path"], folder / f"{name}.mp4")
    tags = " ".join(creator.required_tags) if creator and creator.required_tags else ""
    (folder / f"{name}.txt").write_text(
        f"TITLE:\n{r['title']}\n\n"
        f"DESCRIPTION / CAPTION:\n{r['caption']}\n{tags}\n\nCredit: {r['creator']}\n\n"
        f"SOURCE: {r['url']} @ {r['start']:.0f}s–{r['end']:.0f}s\n"
        f"PERMISSION: {creator.permission if creator else ''}\n\n"
        "CHECKLIST:\n- [ ] Upload as Short / Reel (no copyrighted music added)\n"
        "- [ ] Submit the posted link to the campaign dashboard\n"
        "- [ ] Paste the posted URL into the review page (Approved tab) to track views\n")
    return folder / f"{name}.mp4"


def serve(cfg: Config, port: int = 8765) -> None:
    print(f"Review page: http://localhost:{port}")
    create_app(cfg).run(host="127.0.0.1", port=port, debug=False)
