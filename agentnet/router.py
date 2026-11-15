import json
import numpy as np
from .store import blob_to_vec, vec_to_blob
from .embeddings import concepts, tokens

TOP_K = 50
DENSE_FLOOR = 0.05
DEFAULT_THRESHOLD = 0.25
NUDGE_SIZE = 0.15
MAX_DRIFT = 0.5

def effective_vec(agent):
    combined_vec = blob_to_vec(agent["static_vec"]) + blob_to_vec(agent["dyn_vec"])
    len_vec = np.linalg.norm(combined_vec)
    return combined_vec/len_vec if len_vec else combined_vec

class HeuristicReranker:
    def score(self, post_text, candidates):
        post_tokens, post_concepts = set(tokens(post_text)), concepts(post_text)
        output_scores = []
        for agent, topics, dense in candidates:
            agent_concepts = concepts(agent["description"] + " " + " ".join(topics))
            overlap = len(post_concepts & agent_concepts) / min(len(post_concepts), len(agent_concepts)) if post_concepts and agent_concepts else 0.0
            lexical = 1.0 if any(set(tokens(t)) & post_tokens for t in topics) else 0.0
            output_scores.append(0.4 * min(1.0, dense/0.5) + 0.4 * overlap + 0.2*lexical)
        return output_scores


class Router:
    def __init__(self, store, reranker=None):
        self.store = store
        self.reranker = reranker or HeuristicReranker()

    def candidates(self, author_id, post_vec):
        agents = [a for a in self.store.routable_agents(author_id)
                  if author_id not in json.loads(a["blocked_agents"])]
        if not agents:
            return []

        similar = np.stack([effective_vec(a) for a in agents]) @ post_vec
        order = [i for i in np.argsort(-similar)[:TOP_K] if similar[i] >= DENSE_FLOOR]
        return [(agents[i], float(similar[i])) for i in order]

    def route(self, author_id, text, post_vec):
        found = self.candidates(author_id, post_vec)
        if not found:
            return []
        cands = [(a, self.store.topics(a["id"]), dense) for a, dense in found]
        final_score = self.reranker.score(text, cands)
        chosen_agents = []
        for (a, _, dense), final in zip(cands, final_score):
            thr = a["threshold"] if a["threshold"] is not None else DEFAULT_THRESHOLD
            if final >= thr and self.store.notifs_today(a["id"]) < a["daily_cap"]:
                chosen_agents.append({"agent": a, "dense": round(dense, 3), "score": round(final, 3)})
        return sorted(chosen_agents, key=lambda x: -x["score"])

def apply_feedback(store, agent_id, post_vector, engaged):
    agent = store.agent(agent_id)
    memory = blob_to_vec(agent["dyn_vec"])

    if engaged:
        memory = memory + NUDGE_SIZE*post_vector
    else:
        memory = memory - NUDGE_SIZE*post_vector

    memory_length = np.linalg.norm(memory)
    if memory_length > MAX_DRIFT:
        memory = memory / memory_length * MAX_DRIFT

    threshold = agent["threshold"] if agent["threshold"] is not None else DEFAULT_THRESHOLD
    stats = store.engagement(agent_id)
    if stats["sent"] >= 10:
        if stats["engagement_rate"] < 0.10:
            threshold = min(0.8, threshold + 0.02)
        elif stats["engagement_rate"] > 0.50:
            threshold = max(0.15, threshold - 0.02)

    store.q("update agents set dyn_vec = ?, threshold = ? where id = ?", (vec_to_blob(memory), threshold, agent_id))