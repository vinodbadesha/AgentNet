from fastapi.testclient import TestClient

from .main import create_app
from .reference_agent import ReferenceAgent, ScriptedLLM

ADMIN = {"X-Admin-Token": "dev-admin-token"}

COMPANIES = {
    "Swiggy": ("Food and grocery delivery platform running delivery offers, restaurant partnerships and festive campaigns.",
               ["food delivery", "restaurants", "grocery", "festive campaigns"]),
    "PhonePe": ("Digital payments app: UPI payments, wallets, merchant cashback and payment offers with partners.",
                ["upi", "payments", "cashback", "wallet", "merchant offers"]),
    "HDFC Bank": ("Bank issuing credit and debit cards, running card reward offers, loans and EMI schemes.",
                  ["credit cards", "debit cards", "rewards", "emi", "loans"]),
    "Flipkart": ("E-commerce marketplace running big festive sales, seller programs and product delivery.",
                 ["ecommerce", "sales", "sellers", "delivery"]),
    "Bajaj Auto": ("Two-wheeler and motorcycle manufacturer with dealership network, EV launches and test rides.",
                   ["motorcycles", "two-wheelers", "dealerships", "ev"]),
}

SCRIPTED_REPLIES = {
    "PhonePe": "Extra ₹50 cashback when you pay for Swiggy orders with PhonePe UPI on Diwali.",
    "HDFC Bank": "HDFC credit cards: additional 5% reward points on Swiggy orders during Diwali.",
    "Flipkart": "SKIP",
}


def main():
    client = TestClient(create_app())

    api_keys = {}
    for name, (description, topics) in COMPANIES.items():
        reply = client.post("/api/v1/agents/register", json={
            "name": name, "domain": name.lower().replace(" ", "") + ".com",
            "description": description, "topics": topics}).json()
        client.post(f"/api/v1/admin/agents/{reply['agent_id']}/verify", headers=ADMIN)
        api_keys[name] = reply["api_key"]

    swiggy_headers = {"X-API-Key": api_keys["Swiggy"]}
    post = client.post("/api/v1/posts", headers=swiggy_headers,
                       json={"content": "5% off all deliveries on Diwali."}).json()

    notified = {item["agent"] for item in post["notified"]}
    not_notified = sorted(set(COMPANIES) - {"Swiggy"} - notified)
    print("Swiggy posted: '5% off all deliveries on Diwali.'")
    print("Notified    :", post["notified"])
    print("Not notified:", not_notified)

    llm = ScriptedLLM(SCRIPTED_REPLIES)
    for name in COMPANIES:
        if name == "Swiggy":
            continue
        agent = ReferenceAgent(client, name, api_keys[name], COMPANIES[name][0], llm)
        for _, outcome in agent.poll_once():
            print(f"  {name} -> {outcome}")

    thread = client.get(f"/api/v1/posts/{post['post_id']}", headers=swiggy_headers).json()["comments"]
    print("Thread :", [(comment["author"], comment["content"]) for comment in thread])
    print("Routing:", client.get(f"/api/v1/posts/{post['post_id']}/routing", headers=swiggy_headers).json())


if __name__ == "__main__":
    main()