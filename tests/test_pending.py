from pending import PendingReviewStore, new_token


def test_save_and_take_roundtrip():
    store = PendingReviewStore(":memory:")
    payload = {"repo": "owner/repo", "sha": "abc123", "comments": [1, 2, 3]}

    store.save("tok1", payload)
    result = store.take("tok1")

    assert result == payload


def test_take_unknown_token_returns_none():
    store = PendingReviewStore(":memory:")

    assert store.take("nope") is None


def test_take_pops_token():
    store = PendingReviewStore(":memory:")
    store.save("tok1", {"a": 1})

    first = store.take("tok1")
    second = store.take("tok1")

    assert first == {"a": 1}
    assert second is None


def test_store_is_bounded():
    store = PendingReviewStore(":memory:", max_rows=2)

    store.save("tok1", {"n": 1})
    store.save("tok2", {"n": 2})
    store.save("tok3", {"n": 3})

    assert store.take("tok1") is None
    assert store.take("tok2") == {"n": 2}
    assert store.take("tok3") == {"n": 3}


def test_survives_across_instances(tmp_path):
    db = str(tmp_path / "reviewed.db")

    first = PendingReviewStore(db)
    first.save("tok1", {"n": 1})

    second = PendingReviewStore(db)  # fresh instance = simulated restart
    assert second.take("tok1") == {"n": 1}


def test_no_db_file_created_until_used(tmp_path):
    db = tmp_path / "lazy.db"

    PendingReviewStore(str(db))
    assert not db.exists()

    PendingReviewStore(str(db)).save("tok1", {"n": 1})
    assert db.exists()


def test_new_token_returns_distinct_short_strings():
    tokens = {new_token() for _ in range(20)}

    assert len(tokens) == 20
    for token in tokens:
        assert isinstance(token, str)
        assert len(token) < 20  # well under Telegram's 64-byte callback_data limit
