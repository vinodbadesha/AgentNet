import hashlib
import sqlite3
import uuid
import threading
import json
import time
import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents(
  id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, domain TEXT, description TEXT,
  blocked_agents TEXT DEFAULT '[]', api_key_hash TEXT UNIQUE, webhook_url TEXT,
  webhook_secret TEXT, verified INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
  threshold REAL, daily_cap INTEGER DEFAULT 50, static_vec BLOB, dyn_vec BLOB, created_at REAL);
CREATE TABLE IF NOT EXISTS subscriptions(
  agent_id TEXT, topic TEXT, PRIMARY KEY(agent_id, topic));
CREATE TABLE IF NOT EXISTS posts(
  id TEXT PRIMARY KEY, agent_id TEXT, content TEXT, tags TEXT, status TEXT, vec BLOB, created_at REAL);
CREATE TABLE IF NOT EXISTS notifications(
  id TEXT PRIMARY KEY, post_id TEXT, agent_id TEXT, dense REAL, score REAL,
  status TEXT DEFAULT 'pending', delivery TEXT DEFAULT 'inbox', created_at REAL,
  UNIQUE(post_id, agent_id));
CREATE TABLE IF NOT EXISTS comments(
  id TEXT PRIMARY KEY, post_id TEXT, agent_id TEXT, content TEXT, created_at REAL);
"""

def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"

def hash_key(key):
    return hashlib.sha256(key.encode()).hexdigest()

def vec_to_blob(vec):
    return np.asarray(vec, np.float32).tobytes()

def blob_to_vec(blob):
    return np.frombuffer(blob, dtype=np.float32).copy()

class Store:
    def __init__(self, path = ":memory:"):
        self.db = sqlite3.connect(path,  check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(SCHEMA)

    def q(self, sql, args=(), one=False):
        with self.lock:
            rows = self.db.execute(sql, args).fetchall()
            self.db.commit()
        out = [dict(r) for r in rows]
        return (out[0] if out else None) if one else out

    def create_agent(self, a, topics):
        try:
            self.q("INSERT INTO agents(id, name, domain, description, blocked_agents, api_key_hash, webhook_url,"
            "webhook_secret, threshold, daily_cap, static_vec, dyn_vec, created_at)"
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (a["id"], a["name"], a["domain"], a["description"], json.dumps(a["blocked_agents"]), a["api_key_hash"], a["webhook_url"],
             a["webhook_secret"], a["threshold"], a["daily_cap"], a["static_vec"], vec_to_blob(np.zeros_like(blob_to_vec(a["static_vec"]))), time.time()))
        except sqlite3.IntegrityError as e:
            raise ValueError("Agent name already registered") from e
        self.set_topics(a["id"], topics)

    def set_topics(self, agent_id, topics):
        self.q("DELETE FROM subscriptions WHERE agent_id = ?", (agent_id,))
        seen = set()
        for topic in topics:
            t = topic.strip().lower()
            if t and t not in seen:
                seen.add(t)
                self.q("INSERT INTO subscriptions VALUES(?,?)", (agent_id, t))

    def topics(self, agent_id):
        return [r["topic"] for r in self.q("SELECT topic FROM subscriptions WHERE agent_id = ?", (agent_id,))]

    def agent_by_key(self, key):
        return self.q("select * from agents where api_key_hash = ? and active = 1", (hash_key(key),), one = True)

    def agent(self, agent_id):
        return self.q("select * from agents where id = ?", (agent_id,), one = True)

    def routable_agents(self, author_id):
        return self.q("select * from agents where active = 1 and verified = 1 and id != ?", (author_id,))

    def posts_last_hour(self, agent_id):
        return self.q("select count(*) n from posts where agent_id = ? and created_at > ?", (agent_id, time.time() - 3600), one = True)["n"]

    def notifs_today(self, agent_id):
        return self.q("select count(*) n from notifications where agent_id = ? and created_at > ?", (agent_id, time.time() - 86400), one = True)["n"]

    def engagement(self, agent_id):
        r = self.q("select count(*) sent, sum(status = 'engaged') engaged, sum(status = 'ignored') ignored "
                "from notifications where agent_id = ?", (agent_id,), one = True)
        sent = r["sent"] or 0
        engaged = r["engaged"] or 0
        ignored = r["ignored"] or 0
        return {"sent": sent, "engaged": engaged, "ignored": ignored,
                "engagement_rate": round(engaged/sent, 3) if sent else 0.0}


if __name__ == "__main__":
    store = Store()
    store.q('INSERT INTO agents(id, name) VALUES(?, ?)', (new_id("agt"), "swiggy"))
    print(store.q("SELECT id, name FROM agents"))