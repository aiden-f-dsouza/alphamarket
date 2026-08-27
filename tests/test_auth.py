SIGNUP = {
    "email": "sam@example.com",
    "password": "correct-horse-battery",
    "display_name": "Sam",
}


def test_signup_login_me_flow(client):
    resp = client.post("/auth/signup", json=SIGNUP)
    assert resp.status_code == 201
    assert resp.json()["email"] == "sam@example.com"

    # session cookie set by signup: /me works immediately
    resp = client.get("/auth/me")
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Sam"

    resp = client.post("/auth/logout")
    assert resp.status_code == 204
    assert client.get("/auth/me").status_code == 401

    resp = client.post(
        "/auth/login", json={"email": "sam@example.com", "password": SIGNUP["password"]}
    )
    assert resp.status_code == 200
    assert client.get("/auth/me").status_code == 200


def test_duplicate_email_rejected(client):
    assert client.post("/auth/signup", json=SIGNUP).status_code == 201
    assert client.post("/auth/signup", json=SIGNUP).status_code == 409


def test_wrong_password_and_unknown_email_same_error(client):
    client.post("/auth/signup", json=SIGNUP)
    r1 = client.post("/auth/login", json={"email": "sam@example.com", "password": "wrong-password"})
    r2 = client.post("/auth/login", json={"email": "nobody@example.com", "password": "whatever123"})
    assert r1.status_code == r2.status_code == 401
    assert r1.json() == r2.json()


def test_short_password_rejected(client):
    bad = dict(SIGNUP, password="short")
    assert client.post("/auth/signup", json=bad).status_code == 422
