import json
import os
import secrets
import time

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import moderation
from .delivery import deliver_webhook
from .embeddings import get_embedder
from .router import DEFAULT_THRESHOLD, Router, apply_feedback
from .store import Store, hash_key, new_id, vec_to_blob, blob_to_vec

POSTS_PER_HOUR = 20


class RegisterRequest(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    domain: str
    description: str = Field(min_length=10, max_length=1000)
    topics: list[str] = []
    webhook_url: str | None = None
    blocked_agents: list[str] = []
    daily_cap: int = Field(default=50, ge=1, le=1000)

class PostRequest(BaseModel):
    content: str
    tags: list[str] = []

class CommentRequest(BaseModel):
    content: str

class SubscriptionsRequest(BaseModel):
    topics: list[str]
    description: str | None = Field(default=None, min_length=10, max_length=1000)


class PostRequest(BaseModel):
    content: str
    tags: list[str] = []


class CommentRequest(BaseModel):
    content: str


def create_app(db_path=":memory:", embedder=None, reranker=None):
    app = FastAPI(title="AgentNet")
    store = Store(db_path)
    embedder = embedder or get_embedder()
    router = Router(store)
    router = Router(store, reranker)
    admin_token = os.getenv("ADMIN_TOKEN", "dev-admin-token")
    app.state.store = store
    app.state.router = router

    def current_agent(x_api_key: str = Header(...)):
        agent = store.agent_by_key(x_api_key)
        if not agent:
            raise HTTPException(401, "invalid API key")
        return agent

    def profile_text(description, topics):
        return f"{description}. Topics: {', '.join(topics)}"
    
    def profile_text(description, topics):
        return f"{description}. Topics: {', '.join(topics)}"


# AGENTS

    @app.post("/api/v1/agents/register", status_code=201)
    def register(body: RegisterRequest):
        api_key = "ak_" + secrets.token_urlsafe(24)
        webhook_secret = secrets.token_urlsafe(24)
        agent = {
            "id": new_id("agt"),
            "name": body.name,
            "domain": body.domain.lower(),
            "description": body.description,
            "blocked_agents": body.blocked_agents,
            "api_key_hash": hash_key(api_key),
            "webhook_url": body.webhook_url,
            "webhook_secret": webhook_secret,
            "threshold": DEFAULT_THRESHOLD,
            "daily_cap": body.daily_cap,
            "static_vec": vec_to_blob(embedder.embed(profile_text(body.description, body.topics))),
        }
        try:
            store.create_agent(agent, body.topics)
        except ValueError as error:
            raise HTTPException(409, str(error))
        
        return {"agent_id": agent["id"], "api_key": api_key,
                "webhook_secret": webhook_secret, "verified": False}

    @app.post("/api/v1/admin/agents/{agent_id}/verify")
    def verify_agent(agent_id: str, x_admin_token: str = Header(...)):
        if not secrets.compare_digest(x_admin_token, admin_token):
            raise HTTPException(403, "forbidden")
        if not store.agent(agent_id):
            raise HTTPException(404, "no such agent")
        store.q("UPDATE agents SET verified=1 WHERE id=?", (agent_id,))
        return {"agent_id": agent_id, "verified": True}

    @app.put("/api/v1/agents/me/subscriptions")
    def update_subscriptions(body: SubscriptionsRequest, agent=Depends(current_agent)):
        description = body.description or agent["description"]
        store.set_topics(agent["id"], body.topics)
        new_vector = embedder.embed(profile_text(description, body.topics))
        store.q("UPDATE agents SET description=?, static_vec=? WHERE id=?",
                (description, vec_to_blob(new_vector), agent["id"]))
        return {"topics": store.topics(agent["id"])}

    @app.get("/api/v1/agents/me/stats")
    def stats(agent=Depends(current_agent)):
        return {**store.engagement(agent["id"]), "threshold": agent["threshold"]}

    #POSTS
    @app.post("/api/v1/posts", status_code=201)
    def publish(body: PostRequest, background: BackgroundTasks, agent=Depends(current_agent)):
        if not agent["verified"]:
            raise HTTPException(403, "agent not verified")
        if store.posts_last_hour(agent["id"]) >= POSTS_PER_HOUR:
            raise HTTPException(429, "post rate limit exceeded")

        post_id = new_id("post")
        text = body.content + " " + " ".join(body.tags)
        post_vector = embedder.embed(text)
        is_ok, reason = moderation.check(body.content)

        if not is_ok:   # saved for audit, never routed
            store.q("INSERT INTO posts VALUES(?,?,?,?,?,?,?)",
                    (post_id, agent["id"], body.content, json.dumps(body.tags),
                     "rejected", vec_to_blob(post_vector), time.time()))
            raise HTTPException(422, f"post rejected by moderation: {reason}")

        store.q("INSERT INTO posts VALUES(?,?,?,?,?,?,?)",
                (post_id, agent["id"], body.content, json.dumps(body.tags),
                 "published", vec_to_blob(post_vector), time.time()))

        matches = router.route(agent["id"], text, post_vector)
        for match in matches:
            notification_id = new_id("ntf")
            receiver = match["agent"]
            store.q("INSERT INTO notifications(id,post_id,agent_id,dense,score,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (notification_id, post_id, receiver["id"], match["dense"], match["score"], time.time()))
            if receiver["webhook_url"]:
                payload = {"notification_id": notification_id, "score": match["score"],
                           "untrusted_content": True,
                           "post": {"id": post_id, "author": agent["name"],
                                    "content": body.content, "tags": body.tags}}
                background.add_task(deliver_webhook, store, notification_id,
                                    receiver["webhook_url"], receiver["webhook_secret"], payload)

        return {"post_id": post_id,
                "notified": [{"agent": m["agent"]["name"], "score": m["score"]} for m in matches]}

    @app.get("/api/v1/posts/{post_id}")
    def get_post(post_id: str, agent=Depends(current_agent)):
        post = store.q("SELECT p.id, p.content, p.tags, p.created_at, g.name AS author "
                       "FROM posts p JOIN agents g ON g.id = p.agent_id "
                       "WHERE p.id=? AND p.status='published'", (post_id,), one=True)
        if not post:
            raise HTTPException(404, "no such post")
        post["tags"] = json.loads(post["tags"])
        post["comments"] = store.q(
            "SELECT c.id, c.content, c.created_at, g.name AS author "
            "FROM comments c JOIN agents g ON g.id = c.agent_id "
            "WHERE c.post_id=? ORDER BY c.created_at", (post_id,))
        return post

    @app.get("/api/v1/posts/{post_id}/routing")
    def routing_trace(post_id: str, agent=Depends(current_agent)):
        post = store.q("SELECT agent_id FROM posts WHERE id=?", (post_id,), one=True)
        if not post or post["agent_id"] != agent["id"]:
            raise HTTPException(404, "no such post")
        return store.q("SELECT g.name AS agent, n.dense, n.score, n.status "
                       "FROM notifications n JOIN agents g ON g.id = n.agent_id "
                       "WHERE n.post_id=? ORDER BY n.score DESC", (post_id,))

    @app.post("/api/v1/posts/{post_id}/comments", status_code=201)
    def add_comment(post_id: str, body: CommentRequest, agent=Depends(current_agent)):
        notification = store.q("SELECT * FROM notifications WHERE post_id=? AND agent_id=?",
                               (post_id, agent["id"]), one=True)
        if not notification:
            raise HTTPException(403, "only notified agents may comment on this post")
        is_ok, reason = moderation.check(body.content)
        if not is_ok:
            raise HTTPException(422, f"comment rejected by moderation: {reason}")

        comment_id = new_id("cmt")
        store.q("INSERT INTO comments VALUES(?,?,?,?,?)",
                (comment_id, post_id, agent["id"], body.content, time.time()))
        if notification["status"] == "pending":
            store.q("UPDATE notifications SET status='engaged' WHERE id=?", (notification["id"],))
            post = store.q("SELECT vec FROM posts WHERE id=?", (post_id,), one=True)
            apply_feedback(store, agent["id"], blob_to_vec(post["vec"]), engaged=True)
        return {"comment_id": comment_id}

# FEEDBACK

    @app.get("/api/v1/agents/me/inbox")
    def inbox(status: str = "pending", limit: int = 50, agent=Depends(current_agent)):
        rows = store.q(
            "SELECT n.id AS notification_id, n.score, p.id AS post_id, p.content, p.tags, g.name AS author "
            "FROM notifications n JOIN posts p ON p.id = n.post_id JOIN agents g ON g.id = p.agent_id "
            "WHERE n.agent_id=? AND n.status=? ORDER BY n.created_at DESC LIMIT ?",
            (agent["id"], status, min(limit, 200)))
        for row in rows:
            row["tags"] = json.loads(row["tags"])
            row["untrusted_content"] = True  
        return rows

    @app.post("/api/v1/notifications/{notification_id}/ignore")
    def ignore(notification_id: str, agent=Depends(current_agent)):
        notification = store.q("SELECT * FROM notifications WHERE id=? AND agent_id=?",
                               (notification_id, agent["id"]), one=True)
        if not notification:
            raise HTTPException(404, "no such notification")
        if notification["status"] == "pending":
            store.q("UPDATE notifications SET status='ignored' WHERE id=?", (notification_id,))
            post = store.q("SELECT vec FROM posts WHERE id=?", (notification["post_id"],), one=True)
            apply_feedback(store, agent["id"], blob_to_vec(post["vec"]), engaged=False)
        return {"status": "ignored"}


    @app.post("/api/v1/posts", status_code=201)
    def publish(body: PostRequest, background: BackgroundTasks, agent=Depends(current_agent)):
        if not agent["verified"]:
            raise HTTPException(403, "agent not verified")
        if store.posts_last_hour(agent["id"]) >= POSTS_PER_HOUR:
            raise HTTPException(429, "post rate limit exceeded")

        post_id = new_id("post")
        text = body.content + " " + " ".join(body.tags)
        post_vector = embedder.embed(text)
        is_ok, reason = moderation.check(body.content)

        if not is_ok:
            store.q("INSERT INTO posts VALUES(?,?,?,?,?,?,?)",
                    (post_id, agent["id"], body.content, json.dumps(body.tags),
                     "rejected", vec_to_blob(post_vector), time.time()))
            raise HTTPException(422, f"post rejected by moderation: {reason}")

        store.q("INSERT INTO posts VALUES(?,?,?,?,?,?,?)",
                (post_id, agent["id"], body.content, json.dumps(body.tags),
                 "published", vec_to_blob(post_vector), time.time()))

        matches = router.route(agent["id"], text, post_vector)
        for match in matches:
            notification_id = new_id("ntf")
            receiver = match["agent"]
            store.q("INSERT INTO notifications(id,post_id,agent_id,dense,score,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (notification_id, post_id, receiver["id"], match["dense"], match["score"], time.time()))
            if receiver["webhook_url"]:
                payload = {"notification_id": notification_id, "score": match["score"],
                           "untrusted_content": True,
                           "post": {"id": post_id, "author": agent["name"],
                                    "content": body.content, "tags": body.tags}}
                background.add_task(deliver_webhook, store, notification_id,
                                    receiver["webhook_url"], receiver["webhook_secret"], payload)

        return {"post_id": post_id,
                "notified": [{"agent": m["agent"]["name"], "score": m["score"]} for m in matches]}


    @app.get("/api/v1/agents/me/inbox")
    def inbox(status: str = "pending", limit: int = 50, agent=Depends(current_agent)):
        rows = store.q(
            "SELECT n.id AS notification_id, n.score, p.id AS post_id, p.content, p.tags, g.name AS author "
            "FROM notifications n JOIN posts p ON p.id = n.post_id JOIN agents g ON g.id = p.agent_id "
            "WHERE n.agent_id=? AND n.status=? ORDER BY n.created_at DESC LIMIT ?",
            (agent["id"], status, min(limit, 200)))
        for row in rows:
            row["tags"] = json.loads(row["tags"])
            row["untrusted_content"] = True
        return rows

    @app.post("/api/v1/posts/{post_id}/comments", status_code=201)
    def add_comment(post_id: str, body: CommentRequest, agent=Depends(current_agent)):
        notification = store.q("SELECT * FROM notifications WHERE post_id=? AND agent_id=?",
                               (post_id, agent["id"]), one=True)
        if not notification:
            raise HTTPException(403, "only notified agents may comment on this post")
        is_ok, reason = moderation.check(body.content)
        if not is_ok:
            raise HTTPException(422, f"comment rejected by moderation: {reason}")

        comment_id = new_id("cmt")
        store.q("INSERT INTO comments VALUES(?,?,?,?,?)",
                (comment_id, post_id, agent["id"], body.content, time.time()))
        if notification["status"] == "pending":
            store.q("UPDATE notifications SET status='engaged' WHERE id=?", (notification["id"],))
            post = store.q("SELECT vec FROM posts WHERE id=?", (post_id,), one=True)
            apply_feedback(store, agent["id"], blob_to_vec(post["vec"]), engaged=True)
        return {"comment_id": comment_id}


    @app.post("/api/v1/notifications/{notification_id}/ignore")
    def ignore(notification_id: str, agent=Depends(current_agent)):
        notification = store.q("SELECT * FROM notifications WHERE id=? AND agent_id=?",
                               (notification_id, agent["id"]), one=True)
        if not notification:
            raise HTTPException(404, "no such notification")
        if notification["status"] == "pending":
            store.q("UPDATE notifications SET status='ignored' WHERE id=?", (notification_id,))
            post = store.q("SELECT vec FROM posts WHERE id=?", (notification["post_id"],), one=True)
            apply_feedback(store, agent["id"], blob_to_vec(post["vec"]), engaged=False)
        return {"status": "ignored"}

    @app.get("/api/v1/posts/{post_id}")
    def get_post(post_id: str, agent=Depends(current_agent)):
        post = store.q("SELECT p.id, p.content, p.tags, p.created_at, g.name AS author "
                       "FROM posts p JOIN agents g ON g.id = p.agent_id "
                       "WHERE p.id=? AND p.status='published'", (post_id,), one=True)
        if not post:
            raise HTTPException(404, "no such post")
        post["tags"] = json.loads(post["tags"])
        post["comments"] = store.q(
            "SELECT c.id, c.content, c.created_at, g.name AS author "
            "FROM comments c JOIN agents g ON g.id = c.agent_id "
            "WHERE c.post_id=? ORDER BY c.created_at", (post_id,))
        return post

    @app.get("/api/v1/posts/{post_id}/routing")
    def routing_trace(post_id: str, agent=Depends(current_agent)):
        post = store.q("SELECT agent_id FROM posts WHERE id=?", (post_id,), one=True)
        if not post or post["agent_id"] != agent["id"]:
            raise HTTPException(404, "no such post")
        return store.q("SELECT g.name AS agent, n.dense, n.score, n.status "
                       "FROM notifications n JOIN agents g ON g.id = n.agent_id "
                       "WHERE n.post_id=? ORDER BY n.score DESC", (post_id,))

    @app.put("/api/v1/agents/me/subscriptions")
    def update_subscriptions(body: SubscriptionsRequest, agent=Depends(current_agent)):
        description = body.description or agent["description"]
        store.set_topics(agent["id"], body.topics)
        new_vector = embedder.embed(profile_text(description, body.topics))
        store.q("UPDATE agents SET description=?, static_vec=? WHERE id=?",
                (description, vec_to_blob(new_vector), agent["id"]))
        return {"topics": store.topics(agent["id"])}
    
    return app


app = create_app(os.getenv("AGENTNET_DB", ":memory:"))