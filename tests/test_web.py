import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from homehunt import cli
from homehunt.store import Store
from homehunt.web import render_page

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
HOSTILE = "</script><script>alert(1)</script> & <b>x</b>"
DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


def add(store, n, title="t", price=5000, photos='["https://img/1.jpg"]'):
    cur = store.db.execute(
        """INSERT INTO listings (source, source_id, url, title, price, beds, address,
        city, photos, detected_at, fingerprint, status)
        VALUES ('zillow', ?, ?, ?, ?, 3, '1 Oak St', 'Pittsburgh', ?, ?, ?, 'analyzed')""",
        (f"fb{n}", f"https://fb/{n}", title, price, photos, NOW.isoformat(), f"fp{n}"),
    )
    store.db.execute(
        "INSERT INTO price_history (listing_id, price, seen_at) VALUES (?,?,?)",
        (cur.lastrowid, price, NOW.isoformat()),
    )
    store.db.commit()
    return cur.lastrowid


def build(tmp_path, title="t"):
    store = Store(tmp_path / "t.db")
    a = add(store, 1, title)
    b = add(store, 2)
    c = add(store, 3, price=4000)
    store.db.execute(
        "INSERT INTO price_history (listing_id, price, seen_at) VALUES (?,3500,?)",
        (c, NOW.isoformat()),
    )
    store.db.execute("UPDATE listings SET price=3500 WHERE id=?", (c,))
    store.record_analysis(a, "triage", "haiku", "worth_seeing", 80, 70, {"x": 1}, 5, "new")
    store.record_analysis(
        a, "judge", "opus", "worth_seeing", 85, 75, {"summary": "хорошо"}, 30, "new"
    )
    store.record_notification(a, 9)
    store.record_analysis(b, "triage", "haiku", "skip", 20, 10, {"summary": "no"}, 4, "new")
    store.close()
    return Store(tmp_path / "t.db")


def data_of(html):
    m = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.DOTALL)
    return json.loads(m.group(1))


def test_page_has_rental_labels(tmp_path):
    html = render_page(build(tmp_path), NOW)
    assert "Надёжность" not in html
    assert "Подходит" in html
    assert 'SINGLE_FAMILY: "дом"' in html
    assert "HOME_TYPE[l.home_type] || l.home_type" in html


def test_page_shows_facts_and_map(tmp_path):
    store = build(tmp_path)
    store.db.execute(
        """UPDATE listings SET laundry='["In Unit"]', parking='["Off Street"]',
        lat=40.44, lon=-79.99 WHERE id=1"""
    )
    store.db.commit()
    html = render_page(store, NOW)
    by_id = {x["id"]: x for x in data_of(html)["listings"]}
    one = dict(by_id[1]["facts"])
    assert one["Стирка"] == "в доме" and one["Парковка"] == "во дворе"
    assert "query=40.44,-79.99" in by_id[1]["map_url"]
    assert dict(by_id[3]["facts"])["Стирка"] == "нет данных"
    assert "l.facts" in html and "l.map_url" in html and "📍 Карта" in html
    assert "Стирка: \" +" not in html


def test_page_embeds_every_listing_with_state(tmp_path):
    store = build(tmp_path)
    data = data_of(render_page(store, NOW))
    by_id = {x["id"]: x for x in data["listings"]}
    assert len(by_id) == 3
    assert by_id[1]["verdict"] == "worth_seeing" and by_id[1]["notified"] is True
    assert by_id[1]["analyses"]["judge"]["json"]["summary"] == "хорошо"
    assert by_id[1]["analyses"]["triage"]["model"] == "haiku"
    assert by_id[2]["verdict"] == "skip" and by_id[2]["notified"] is False
    assert "judge" not in by_id[2]["analyses"]
    assert by_id[3]["verdict"] is None
    assert [p["price"] for p in by_id[3]["price_history"]] == [4000, 3500]
    assert data["meta"]["found"] == 3 and data["meta"]["notified"] == 1
    assert by_id[1]["detected_local"] == "2026-11-10 07:00"


def test_page_carries_score_and_hard_fail(tmp_path):
    store = build(tmp_path)
    store.record_score(1, 40.0, 1500, "Shadyside", {"cost": {"value": 10, "weight": 50}}, None)
    store.record_score(1, 71.5, 1676, "Squirrel Hill",
                       {"cost": {"value": 17.7, "weight": 50}, "safety": {"value": None, "weight": 12}},
                       None)
    store.record_score(2, 12.0, 3300, None, {}, "rent 3000 > max 1500")
    html = render_page(store, NOW)
    by_id = {x["id"]: x for x in data_of(html)["listings"]}
    one = by_id[1]
    assert (one["score"], one["full_cost"], one["place"]) == (71.5, 1676, "Squirrel Hill")
    assert one["breakdown"]["safety"] == {"value": None, "weight": 12}
    assert one["hard_fail"] is None
    assert by_id[2]["hard_fail"] == "rent 3000 > max 1500"
    assert by_id[3]["score"] is None and by_id[3]["breakdown"] == {}
    assert "Балл " in html and "Разбор балла" in html and "Отфильтровано: " in html


def test_last_run_skips_unfinished_run(tmp_path):
    store = Store(tmp_path / "t.db")
    store.start_run("fb")
    assert data_of(render_page(store, NOW))["meta"]["run"] is None
    store.finish_run(1, 24, 3, 0)
    store.start_run("fb")
    run = data_of(render_page(store, NOW))["meta"]["run"]
    assert run["cards"] == 24 and run["new"] == 3


def test_hostile_text_cannot_break_out_and_round_trips(tmp_path):
    store = build(tmp_path, title=HOSTILE)
    html = render_page(store, NOW)
    assert "alert(1)" in html  # present, but only inside the JSON, escaped
    assert "</script><script>" not in html
    assert html.count("</script>") == 2  # data block + code block
    assert "<b>x</b>" not in html
    titles = [x["title"] for x in data_of(html)["listings"]]
    assert HOSTILE in titles


def test_page_is_self_contained_and_never_uses_innerhtml(tmp_path):
    html = render_page(build(tmp_path), NOW)
    assert "<script src=" not in html
    assert not re.search(r'<link[^>]+rel=["\']?stylesheet', html)
    assert "innerHTML" not in html
    assert "prefers-color-scheme" in html


def test_page_reads_only_current_verdict_fields(tmp_path):
    html = render_page(build(tmp_path), NOW)
    for current in ("questions_for_landlord", "market_rent", "monthly_cost_note"):
        assert current in html
    for removed in ("questions_for_seller", "market_price", "expected_costs", "negotiation_target"):
        assert removed not in html


def test_cli_web_writes_file_atomically(tmp_path, capsys):
    build(tmp_path).close()
    out = tmp_path / "x" / "index.html"
    code = cli.main(["--db", str(tmp_path / "t.db"), "web", "--out", str(out)], now=NOW)
    assert code == 0
    assert len(data_of(out.read_text(encoding="utf-8"))["listings"]) == 3
    assert [p.name for p in out.parent.iterdir()] == ["index.html"]  # no temp left


def publish(tmp_path, ssh_exit, extra_env=None, dotenv=None):
    root = tmp_path / "repo"
    (root / "deploy").mkdir(parents=True)
    shutil.copy(DEPLOY / "publish-web.sh", root / "deploy" / "publish-web.sh")
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    homehunt = venv_bin / "homehunt"
    homehunt.write_text(
        '#!/bin/sh\n[ "$1" = web ] && [ "$2" = --out ] || exit 2\n'
        'mkdir -p "$(dirname "$3")"; printf "<html>PAGE</html>" > "$3"\n'
    )
    homehunt.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(
        f'#!/bin/sh\necho "$@" > "{tmp_path}/argv"\ncat > "{tmp_path}/stdin"\nexit {ssh_exit}\n'
    )
    ssh.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    env.pop("HOMEHUNT_WEB_SSH", None)
    env.pop("HOMEHUNT_WEB_DIR", None)
    env.update(extra_env or {})
    if dotenv is not None:
        (root / ".env").write_text(dotenv)
    return subprocess.run(
        ["bash", str(root / "deploy" / "publish-web.sh")],
        check=False, env=env, capture_output=True, text=True,
    )


def test_publish_sends_page_over_ssh(tmp_path):
    proc = publish(
        tmp_path, 0, {"HOMEHUNT_WEB_SSH": "web1", "HOMEHUNT_WEB_DIR": "/srv/hh"}
    )
    assert proc.returncode == 0, proc.stderr
    argv = (tmp_path / "argv").read_text()
    assert "BatchMode=yes" in argv and "web1" in argv
    assert "/srv/hh" in argv
    assert (tmp_path / "stdin").read_text() == "<html>PAGE</html>"


def test_publish_without_host_skips_and_never_calls_ssh(tmp_path):
    proc = publish(tmp_path, 0)
    assert proc.returncode == 0
    assert "publish skipped: HOMEHUNT_WEB_SSH not set" in proc.stderr
    assert not (tmp_path / "argv").exists()
    assert (tmp_path / "repo" / "data" / "web" / "index.html").exists()


def test_publish_reads_host_and_dir_from_dotenv(tmp_path):
    proc = publish(
        tmp_path, 0, dotenv="# c\nHOMEHUNT_WEB_SSH=envhost\nHOMEHUNT_WEB_DIR='/srv/x'\n"
    )
    assert proc.returncode == 0, proc.stderr
    argv = (tmp_path / "argv").read_text()
    assert "envhost" in argv and "/srv/x" in argv


def test_publish_failure_never_fails_the_poll(tmp_path):
    proc = publish(
        tmp_path, 255, {"HOMEHUNT_WEB_SSH": "web1", "HOMEHUNT_WEB_DIR": "/srv/hh"}
    )
    assert proc.returncode == 0
    assert "failed" in proc.stderr
