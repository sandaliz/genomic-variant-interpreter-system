"""End-to-end smoke test against the real PostgreSQL-backed API.

Run with the app's database migrated and the plans seeded:
    python scripts/smoke_auth.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from api import app  # noqa: E402


def main() -> int:
    suffix = str(int(time.time()))
    client = TestClient(app)

    print("health:", client.get("/health").json())

    reg = client.post("/api/auth/register", json={
        "username": f"smoke_{suffix}",
        "email": f"smoke_{suffix}@example.com",
        "password": "smoke-test-password",
    })
    print("register:", reg.status_code, reg.json())
    assert reg.status_code == 201, reg.text

    login = client.post("/api/auth/login", json={
        "email": f"smoke_{suffix}@example.com", "password": "smoke-test-password",
    })
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    print("login: ok, plan =", login.json()["user"]["plan_id"])

    me = client.get("/api/auth/me", headers=headers)
    print("me:", me.status_code, me.json()["plan"]["name"], me.json()["usage"])
    assert me.status_code == 200

    assert client.get("/api/auth/me").status_code == 401, "unauthenticated must be 401"

    result = client.post("/api/classify", json={
        "query": "Is BRCA1 c.68_69del pathogenic?"
    }, headers=headers)
    print("classify:", result.status_code)
    if result.status_code == 200:
        body = result.json()
        print("  variant:", body["variant_id"])
        print("  classification:", body["classification"], body["confidence"])
        print("  plan:", body["plan"], "usage:", body["usage"])
        print("  reasoning_mode:", body["reasoning_mode"])
    else:
        print("  detail:", result.text[:400])

    after = client.get("/api/auth/me", headers=headers).json()
    print("usage after classify:", after["usage"])
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())