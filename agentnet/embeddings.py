import re
import hashlib
import numpy as np

_TOKEN = re.compile(r"[a-z0-9%₹]+")
_STOP = {"a", "an", "the", "and", "or", "to", "of", "in", "on", "for", "with", "all",
         "our", "we", "is", "are", "at", "by", "from", "this", "that", "it", "as"}

_RAW_CONCEPTS = {
    "food": "food delivery restaurant swiggy zomato instamart grocery meal dining kitchen order",
    "payments": "pay payment upi wallet card creedit debit checkout transaction bank banking cashback netbanking",
    "offers": "offer sale discount coupon off promo cashback deal reward saving promo voucher",
    "festive": "diwali holi eid christmas festive festival navratri dussehra",
    "commerce": "shopping ecommerce amazon flipkart myntra marketplace delivery order cart seller product warranty",
    "auto": "bike motorcycle vehicle scooter car auto dealership ev wheeler bajaj showroom",
    "lending": "loan emi credit rate interest mortgage financing",
    "travel": "flight hotel booking trip vacation travel ticket"
}

def _stem(word):
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("s") and len(word)>3:
        return word[:-1]
    return word

def tokens(word):
    return [_stem(t) for t in _TOKEN.findall(word.lower()) if t not in _STOP]


CONCEPTS = {}
for c, ws in _RAW_CONCEPTS.items():
    stemmed = set()
    for w in ws.split():
        stemmed.add(_stem(w))
    CONCEPTS[c] = stemmed

def concepts(word):
    word_tokens = set(tokens(word))
    return {c for c, words in CONCEPTS.items() if word_tokens & words}

DIM = 512
def _buckets(s):
    return int.from_bytes(hashlib.md5(s.encode()).digest()[:4], "little") % DIM

class LocalEmbedder: 
    name = "local-hash-concept"
    def embed(self, word):
        v = np.zeros(DIM, np.float32)
        for t in tokens(word):
            v[_buckets("w:" + t)] += 1.0
        for c in concepts(word):
            v[_buckets("c:" + c)] += 3.0
        n = np.linalg.norm(v)
        return v/n if n else v

def get_embedder():
    return LocalEmbedder()

if __name__ == "__main__":
    text = "5% off deliveries on Diwali."
    post = get_embedder().embed(text)
    phonepe = get_embedder().embed("Digital payments app: UPI payments, wallets, merchant cashback and payment offers.")
    bajaj = get_embedder().embed("Two-wheeler and motorcycle manufacturer with dealorship network.")
    print(tokens(text))
    print(concepts(text))
    print("post vs phonepe: ", round(float(post @ phonepe), 3))
    print("post vs bajaj: ", round(float(post @ bajaj), 3))